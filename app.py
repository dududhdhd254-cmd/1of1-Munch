"""
app.py
Flask routes that expose bot_manager's functions to your website.

Drop this next to bot_manager.py, `pip install flask`, and run it
(or import `bot_bp` / `manager` into your existing Flask app).

Auth here is a minimal API-key-per-user scheme so this is safe to
run multi-tenant. Swap _get_current_user() for your real login system.
"""

import os
import tempfile
from flask import Flask, request, jsonify, g

from bot_manager import BotManager

app = Flask(__name__)
manager = BotManager()

# --- extremely minimal "auth": header -> user_id -------------------
# Replace this with your real session/login lookup.
API_KEYS = {
    "demo-key-123": "user_1",
}


def _get_current_user():
    key = request.headers.get("X-Api-Key")
    user_id = API_KEYS.get(key)
    if not user_id:
        return None
    return user_id


def _require_user():
    user_id = _get_current_user()
    if not user_id:
        return None, (jsonify({"error": "invalid or missing X-Api-Key"}), 401)
    return user_id, None


def _require_ownership(bot_id, user_id):
    bot = manager.get_status(bot_id)
    if bot["user_id"] != user_id:
        return False
    return True


# --- routes ----------------------------------------------------------

@app.route("/api/bots", methods=["GET"])
def list_bots():
    user_id, err = _require_user()
    if err:
        return err
    return jsonify(manager.list_bots(user_id=user_id))


@app.route("/api/bots", methods=["POST"])
def upload_bot():
    """
    multipart/form-data:
      name: string
      file: the bot's .py file
      requirements: optional requirements.txt
    """
    user_id, err = _require_user()
    if err:
        return err

    name = request.form.get("name", "my-bot")
    if "file" not in request.files:
        return jsonify({"error": "missing 'file' (the bot's .py script)"}), 400

    file = request.files["file"]
    with tempfile.TemporaryDirectory() as tmp:
        file_path = os.path.join(tmp, "main.py")
        file.save(file_path)

        req_path = None
        if "requirements" in request.files:
            req_path = os.path.join(tmp, "requirements.txt")
            request.files["requirements"].save(req_path)

        bot_id = manager.register_bot(user_id, name, file_path, req_path)

    return jsonify({"bot_id": bot_id}), 201


@app.route("/api/bots/<bot_id>/upload", methods=["POST"])
def update_bot_code(bot_id):
    user_id, err = _require_user()
    if err:
        return err
    if not _require_ownership(bot_id, user_id):
        return jsonify({"error": "not found"}), 404

    if "file" not in request.files:
        return jsonify({"error": "missing 'file'"}), 400

    with tempfile.TemporaryDirectory() as tmp:
        file_path = os.path.join(tmp, "main.py")
        request.files["file"].save(file_path)
        req_path = None
        if "requirements" in request.files:
            req_path = os.path.join(tmp, "requirements.txt")
            request.files["requirements"].save(req_path)
        manager.upload_new_version(bot_id, file_path, req_path)

    return jsonify({"status": "updated"})


@app.route("/api/bots/<bot_id>/start", methods=["POST"])
def start_bot(bot_id):
    user_id, err = _require_user()
    if err:
        return err
    if not _require_ownership(bot_id, user_id):
        return jsonify({"error": "not found"}), 404
    manager.start_bot(bot_id)
    return jsonify({"status": "running"})


@app.route("/api/bots/<bot_id>/stop", methods=["POST"])
def stop_bot(bot_id):
    user_id, err = _require_user()
    if err:
        return err
    if not _require_ownership(bot_id, user_id):
        return jsonify({"error": "not found"}), 404
    manager.stop_bot(bot_id)
    return jsonify({"status": "stopped"})


@app.route("/api/bots/<bot_id>/restart", methods=["POST"])
def restart_bot(bot_id):
    user_id, err = _require_user()
    if err:
        return err
    if not _require_ownership(bot_id, user_id):
        return jsonify({"error": "not found"}), 404
    manager.restart_bot(bot_id)
    return jsonify({"status": "restarted"})


@app.route("/api/bots/<bot_id>", methods=["DELETE"])
def delete_bot(bot_id):
    user_id, err = _require_user()
    if err:
        return err
    if not _require_ownership(bot_id, user_id):
        return jsonify({"error": "not found"}), 404
    manager.delete_bot(bot_id)
    return jsonify({"status": "deleted"})


@app.route("/api/bots/<bot_id>/status", methods=["GET"])
def bot_status(bot_id):
    user_id, err = _require_user()
    if err:
        return err
    if not _require_ownership(bot_id, user_id):
        return jsonify({"error": "not found"}), 404
    return jsonify(manager.get_status(bot_id))


@app.route("/api/bots/<bot_id>/logs", methods=["GET"])
def bot_logs(bot_id):
    user_id, err = _require_user()
    if err:
        return err
    if not _require_ownership(bot_id, user_id):
        return jsonify({"error": "not found"}), 404
    lines = int(request.args.get("lines", 200))
    return jsonify({"logs": manager.get_logs(bot_id, max_lines=lines)})


if __name__ == "__main__":
    # Start the 24/7 watchdog once, when the server boots.
    manager.start_watchdog()
    app.run(host="0.0.0.0", port=5000, debug=False)
