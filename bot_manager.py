"""
bot_manager.py
Core functions for a "run my Discord bot 24/7" hosting site (fps.ms-style).

Handles, per bot:
- uploading the bot's .py file (+ optional requirements.txt)
- creating an isolated virtualenv and installing dependencies
- starting / stopping / restarting the bot as a subprocess
- capturing stdout/stderr to a log file
- a background watchdog thread that keeps bots alive 24/7 and
  auto-restarts them if they crash (with crash-loop protection)

Persistence is a simple JSON file (bots.json). Swap for a real DB later
if you want — the function signatures won't need to change.

Wire this into your existing website by importing BotManager and calling
its methods from your routes (Flask/FastAPI/whatever you use).
"""

import os
import sys
import json
import time
import signal
import shutil
import sqlite3
import subprocess
import threading
import uuid
from datetime import datetime, timezone

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "data"))
BOTS_DIR = os.path.join(BASE_DIR, "bots")          # one folder per bot
DB_PATH = os.path.join(BASE_DIR, "bots.db")

os.makedirs(BOTS_DIR, exist_ok=True)

# Restart-loop protection: if a bot crashes this many times within
# CRASH_WINDOW_SECONDS, the watchdog stops trying and marks it "crashed"
# instead of hammering it forever.
MAX_CRASHES = 5
CRASH_WINDOW_SECONDS = 120
WATCHDOG_INTERVAL = 5


def _now():
    return datetime.now(timezone.utc).isoformat()


class BotManager:
    def __init__(self, base_dir=BASE_DIR, db_path=DB_PATH):
        self.base_dir = base_dir
        self.bots_dir = os.path.join(base_dir, "bots")
        self.db_path = db_path
        os.makedirs(self.bots_dir, exist_ok=True)

        # bot_id -> subprocess.Popen (in-memory only; not persisted)
        self._processes = {}
        # bot_id -> list of recent crash timestamps
        self._crash_times = {}
        self._lock = threading.RLock()

        self._init_db()
        self._watchdog_thread = None

    # ---------------------------------------------------------------
    # storage
    # ---------------------------------------------------------------
    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bots (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                name TEXT NOT NULL,
                entry_file TEXT NOT NULL,
                folder TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'stopped',   -- stopped/running/crashed/installing
                should_run INTEGER NOT NULL DEFAULT 0,    -- 1 = watchdog should keep this alive
                pid INTEGER,
                created_at TEXT,
                last_started_at TEXT,
                restart_count INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.commit()
        conn.close()

    def _get_conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _get_bot(self, bot_id):
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM bots WHERE id = ?", (bot_id,)).fetchone()
        conn.close()
        if not row:
            raise ValueError(f"No bot with id {bot_id}")
        return dict(row)

    def _update_bot(self, bot_id, **fields):
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        vals = list(fields.values()) + [bot_id]
        conn = self._get_conn()
        conn.execute(f"UPDATE bots SET {cols} WHERE id = ?", vals)
        conn.commit()
        conn.close()

    def list_bots(self, user_id=None):
        conn = self._get_conn()
        if user_id:
            rows = conn.execute("SELECT * FROM bots WHERE user_id = ?", (user_id,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM bots").fetchall()
        conn.close()
        return [dict(r) for r in rows]

    # ---------------------------------------------------------------
    # upload / setup
    # ---------------------------------------------------------------
    def register_bot(self, user_id, name, uploaded_file_path, requirements_path=None):
        """
        uploaded_file_path: path to the bot's main .py file the user uploaded
        requirements_path: optional path to a requirements.txt they uploaded
        Returns the new bot's id.
        """
        bot_id = uuid.uuid4().hex[:12]
        folder = os.path.join(self.bots_dir, bot_id)
        os.makedirs(folder, exist_ok=True)

        entry_file = "main.py"
        shutil.copy(uploaded_file_path, os.path.join(folder, entry_file))

        if requirements_path and os.path.exists(requirements_path):
            shutil.copy(requirements_path, os.path.join(folder, "requirements.txt"))

        conn = self._get_conn()
        conn.execute(
            "INSERT INTO bots (id, user_id, name, entry_file, folder, status, should_run, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'stopped', 0, ?)",
            (bot_id, user_id, name, entry_file, folder, _now()),
        )
        conn.commit()
        conn.close()

        self._install_requirements(bot_id)
        return bot_id

    def upload_new_version(self, bot_id, uploaded_file_path, requirements_path=None):
        """Replace a bot's code (e.g. user re-uploads an updated script)."""
        bot = self._get_bot(bot_id)
        was_running = bot["status"] == "running"
        if was_running:
            self.stop_bot(bot_id)

        shutil.copy(uploaded_file_path, os.path.join(bot["folder"], bot["entry_file"]))
        if requirements_path and os.path.exists(requirements_path):
            shutil.copy(requirements_path, os.path.join(bot["folder"], "requirements.txt"))
            self._install_requirements(bot_id)

        if was_running:
            self.start_bot(bot_id)

    def _venv_python(self, folder):
        venv_dir = os.path.join(folder, "venv")
        if os.name == "nt":
            return os.path.join(venv_dir, "Scripts", "python.exe")
        return os.path.join(venv_dir, "bin", "python")

    def _install_requirements(self, bot_id):
        bot = self._get_bot(bot_id)
        folder = bot["folder"]
        venv_dir = os.path.join(folder, "venv")

        self._update_bot(bot_id, status="installing")

        if not os.path.exists(venv_dir):
            subprocess.run([sys.executable, "-m", "venv", venv_dir], check=True)

        py = self._venv_python(folder)
        req_file = os.path.join(folder, "requirements.txt")
        if os.path.exists(req_file):
            subprocess.run([py, "-m", "pip", "install", "-r", req_file], check=False)
        else:
            # discord bots need this at minimum
            subprocess.run([py, "-m", "pip", "install", "discord.py"], check=False)

        self._update_bot(bot_id, status="stopped")

    # ---------------------------------------------------------------
    # process control
    # ---------------------------------------------------------------
    def start_bot(self, bot_id):
        with self._lock:
            bot = self._get_bot(bot_id)
            if bot_id in self._processes and self._processes[bot_id].poll() is None:
                return  # already running

            folder = bot["folder"]
            py = self._venv_python(folder)
            script = os.path.join(folder, bot["entry_file"])
            log_path = os.path.join(folder, "output.log")

            log_file = open(log_path, "a", buffering=1)
            log_file.write(f"\n----- started {_now()} -----\n")

            proc = subprocess.Popen(
                [py, "-u", script],
                cwd=folder,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
            )
            self._processes[bot_id] = proc
            self._update_bot(
                bot_id,
                status="running",
                should_run=1,
                pid=proc.pid,
                last_started_at=_now(),
            )

    def stop_bot(self, bot_id):
        with self._lock:
            self._update_bot(bot_id, should_run=0)  # tell watchdog to leave it alone
            proc = self._processes.get(bot_id)
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
            self._processes.pop(bot_id, None)
            self._update_bot(bot_id, status="stopped", pid=None)

    def restart_bot(self, bot_id):
        self.stop_bot(bot_id)
        time.sleep(1)
        self.start_bot(bot_id)

    def delete_bot(self, bot_id):
        try:
            self.stop_bot(bot_id)
        except Exception:
            pass
        bot = self._get_bot(bot_id)
        shutil.rmtree(bot["folder"], ignore_errors=True)
        conn = self._get_conn()
        conn.execute("DELETE FROM bots WHERE id = ?", (bot_id,))
        conn.commit()
        conn.close()

    def get_status(self, bot_id):
        bot = self._get_bot(bot_id)
        proc = self._processes.get(bot_id)
        alive = bool(proc and proc.poll() is None)
        if bot["status"] == "running" and not alive:
            # process died since we last checked
            self._update_bot(bot_id, status="crashed", pid=None)
            bot["status"] = "crashed"
        return bot

    def get_logs(self, bot_id, max_lines=200):
        bot = self._get_bot(bot_id)
        log_path = os.path.join(bot["folder"], "output.log")
        if not os.path.exists(log_path):
            return ""
        with open(log_path, "r", errors="replace") as f:
            lines = f.readlines()
        return "".join(lines[-max_lines:])

    # ---------------------------------------------------------------
    # 24/7 watchdog
    # ---------------------------------------------------------------
    def start_watchdog(self):
        """Call this once when your website's server process boots."""
        if self._watchdog_thread and self._watchdog_thread.is_alive():
            return
        self._watchdog_thread = threading.Thread(target=self._watchdog_loop, daemon=True)
        self._watchdog_thread.start()

    def _watchdog_loop(self):
        while True:
            try:
                self._watchdog_tick()
            except Exception as e:
                print(f"[watchdog] error: {e}")
            time.sleep(WATCHDOG_INTERVAL)

    def _watchdog_tick(self):
        conn = self._get_conn()
        rows = conn.execute("SELECT * FROM bots WHERE should_run = 1").fetchall()
        conn.close()

        for row in rows:
            bot_id = row["id"]
            proc = self._processes.get(bot_id)
            alive = bool(proc and proc.poll() is None)
            if alive:
                continue

            # it died - check crash-loop protection before restarting
            now = time.time()
            times = self._crash_times.setdefault(bot_id, [])
            times.append(now)
            times[:] = [t for t in times if now - t < CRASH_WINDOW_SECONDS]

            if len(times) > MAX_CRASHES:
                self._update_bot(bot_id, status="crashed", should_run=0, pid=None)
                print(f"[watchdog] bot {bot_id} crash-looping, disabled auto-restart")
                continue

            print(f"[watchdog] restarting bot {bot_id} ({len(times)} crash(es) recently)")
            self._processes.pop(bot_id, None)
            self.start_bot(bot_id)
            conn = self._get_conn()
            conn.execute(
                "UPDATE bots SET restart_count = restart_count + 1 WHERE id = ?", (bot_id,)
            )
            conn.commit()
            conn.close()
