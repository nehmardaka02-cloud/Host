"""
=============================================================
  Telegram Bot Hosting Platform
  Requirements: python-telegram-bot>=20.0, flask, psutil
  Run: pip install python-telegram-bot flask psutil
       python main.py
=============================================================
"""

import os
import sys
import json
import time
import shutil
import signal
import logging
import zipfile
import asyncio
import threading
import subprocess
from pathlib import Path
from datetime import datetime, timedelta

import psutil
from flask import Flask, send_file, abort, jsonify
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# ─────────────────────────────────────────────
#  CONFIGURATION  (edit before running)
# ─────────────────────────────────────────────
BOT_TOKEN      = "8791101593:AAEbW3M77kiiL5NghFICpl0iDInpVKh0VwY"          # BotFather token
OWNER_ID       = 8487397448                       # Your Telegram user ID (int)
SERVER_HOST    = "http://your-server.com"        # Public URL of this machine
FLASK_PORT     = 5000
FLASK_HOST     = "0.0.0.0"

MAX_STORAGE_MB = 500        # Per-user storage limit in MB
MAX_BOTS_USER  = 10         # Max bots per user
PORT_RANGE_START = 6000     # Dynamic port allocation start
PORT_RANGE_END   = 7000

# Dangerous shell patterns to block
BLOCKED_PATTERNS = [
    "rm -rf", "rm -r /", "mkfs", ":(){:|:&}", "dd if=/dev/zero",
    "chmod -R 777 /", "> /dev/sda", "wget | sh", "curl | sh",
    "sudo rm", "sudo dd", "format c:",
]

# ─────────────────────────────────────────────
#  DIRECTORY STRUCTURE
# ─────────────────────────────────────────────
BASE_DIR  = Path(__file__).parent
BOTS_DIR  = BASE_DIR / "bots"
LOGS_DIR  = BASE_DIR / "logs"
DATA_DIR  = BASE_DIR / "data"

for d in (BOTS_DIR, LOGS_DIR, DATA_DIR):
    d.mkdir(parents=True, exist_ok=True)

USERS_FILE = DATA_DIR / "users.json"
BOTS_FILE  = DATA_DIR / "bots.json"

# ─────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(BASE_DIR / "platform.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("BotHost")

# ─────────────────────────────────────────────
#  CONVERSATION STATES
# ─────────────────────────────────────────────
WAIT_FILE, WAIT_NAME, WAIT_CONFIRM = range(3)

# ─────────────────────────────────────────────
#  DATA HELPERS
# ─────────────────────────────────────────────

def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return default


def save_json(path: Path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def get_users() -> dict:
    return load_json(USERS_FILE, {})


def save_users(users: dict):
    save_json(USERS_FILE, users)


def get_bots() -> dict:
    return load_json(BOTS_FILE, {})


def save_bots(bots: dict):
    save_json(BOTS_FILE, bots)


def register_user(user_id: int, username: str, full_name: str):
    users = get_users()
    uid = str(user_id)
    if uid not in users:
        users[uid] = {
            "id": user_id,
            "username": username,
            "full_name": full_name,
            "joined": datetime.utcnow().isoformat(),
            "bot_count": 0,
        }
        save_users(users)
    return users[uid]


def generate_bot_id() -> str:
    """Generate a short unique ID."""
    import random, string
    bots = get_bots()
    while True:
        bid = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
        if bid not in bots:
            return bid

# ─────────────────────────────────────────────
#  PROCESS MANAGER
# ─────────────────────────────────────────────

class ProcessManager:
    """Manages subprocess lifecycles for hosted bots."""

    _processes: dict = {}   # bot_id -> subprocess.Popen

    @classmethod
    def find_free_port(cls) -> int:
        import socket
        for port in range(PORT_RANGE_START, PORT_RANGE_END):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                try:
                    s.bind(("0.0.0.0", port))
                    return port
                except OSError:
                    continue
        raise RuntimeError("No free ports available in configured range.")

    @classmethod
    def detect_bot_type(cls, bot_dir: Path) -> tuple:
        """
        Returns (bot_type, start_command, entry_file).
        bot_type: 'python_flask' | 'python' | 'nodejs' | 'php' | 'html' | 'unknown'
        """
        files = list(bot_dir.rglob("*"))
        names = [f.name.lower() for f in files if f.is_file()]

        # Flask detection: app.py containing 'Flask'
        for f in files:
            if f.name.lower() == "app.py" and f.is_file():
                content = f.read_text(errors="ignore")
                if "Flask" in content or "flask" in content:
                    return "python_flask", f"python3 {f.name}", f.name

        # Node.js
        for entry in ("index.js", "bot.js", "main.js", "app.js", "server.js"):
            if entry in names:
                return "nodejs", f"node {entry}", entry
            
        if "package.json" in names:
            pkg = json.loads((bot_dir / "package.json").read_text(errors="ignore"))
            main_file = pkg.get("main", "index.js")
            return "nodejs", f"node {main_file}", main_file

        # PHP
        for entry in ("bot.php", "index.php", "main.php"):
            if entry in names:
                return "php", f"php {entry}", entry

        # HTML
        for entry in ("index.html", "app.html"):
            if entry in names:
                return "html", None, entry

        # Python generic
        for entry in ("main.py", "bot.py", "run.py", "start.py"):
            if entry in names:
                return "python", f"python3 {entry}", entry

        # Fallback: any .py
        py_files = [f for f in files if f.suffix == ".py" and f.is_file()]
        if py_files:
            entry = py_files[0].name
            return "python", f"python3 {entry}", entry

        return "unknown", None, None

    @classmethod
    def is_running(cls, bot_id: str) -> bool:
        proc = cls._processes.get(bot_id)
        if proc is None:
            return False
        return proc.poll() is None

    @classmethod
    def start(cls, bot_id: str, bot_dir: Path, command: str, log_file: Path) -> bool:
        """Start a bot subprocess."""
        if cls.is_running(bot_id):
            return True  # Already running

        parts = command.split()
        # Security: block dangerous commands
        for pattern in BLOCKED_PATTERNS:
            if pattern in command:
                raise ValueError(f"Blocked command pattern detected: {pattern}")

        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_fd = open(log_file, "a", encoding="utf-8")

        try:
            proc = subprocess.Popen(
                parts,
                cwd=str(bot_dir),
                stdout=log_fd,
                stderr=log_fd,
                start_new_session=True,
                env={**os.environ, "BOT_ID": bot_id},
            )
            cls._processes[bot_id] = proc
            logger.info(f"Started bot {bot_id} with PID {proc.pid}")
            return True
        except Exception as e:
            logger.error(f"Failed to start bot {bot_id}: {e}")
            raise

    @classmethod
    def stop(cls, bot_id: str) -> bool:
        proc = cls._processes.get(bot_id)
        if proc is None or proc.poll() is not None:
            cls._processes.pop(bot_id, None)
            return True
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        cls._processes.pop(bot_id, None)
        logger.info(f"Stopped bot {bot_id}")
        return True

    @classmethod
    def get_resource_usage(cls, bot_id: str) -> dict:
        proc = cls._processes.get(bot_id)
        if proc is None or proc.poll() is not None:
            return {"running": False}
        try:
            p = psutil.Process(proc.pid)
            cpu = p.cpu_percent(interval=0.5)
            mem = p.memory_info().rss / (1024 * 1024)
            create_time = p.create_time()
            uptime = int(time.time() - create_time)
            return {
                "running": True,
                "pid": proc.pid,
                "cpu_percent": round(cpu, 2),
                "ram_mb": round(mem, 2),
                "uptime_seconds": uptime,
                "uptime_human": str(timedelta(seconds=uptime)),
            }
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return {"running": False}

# ─────────────────────────────────────────────
#  STORAGE HELPER
# ─────────────────────────────────────────────

def get_user_storage_mb(user_id: int) -> float:
    user_dir = BOTS_DIR / str(user_id)
    if not user_dir.exists():
        return 0.0
    total = sum(f.stat().st_size for f in user_dir.rglob("*") if f.is_file())
    return total / (1024 * 1024)


def get_log_tail(log_file: Path, lines: int = 20) -> str:
    if not log_file.exists():
        return "No logs found."
    try:
        content = log_file.read_text(errors="replace").splitlines()
        tail = content[-lines:] if len(content) > lines else content
        return "\n".join(tail) if tail else "Log file is empty."
    except Exception as e:
        return f"Error reading log: {e}"

# ─────────────────────────────────────────────
#  FLASK APPLICATION (file server)
# ─────────────────────────────────────────────

flask_app = Flask(__name__)


@flask_app.route("/")
def flask_index():
    bots = get_bots()
    return jsonify({
        "service": "Bot Hosting Platform",
        "total_bots": len(bots),
        "running": sum(1 for b in bots.values() if ProcessManager.is_running(b["id"])),
    })


@flask_app.route("/<int:user_id>/<bot_name>/<path:filename>")
def serve_bot_file(user_id: int, bot_name: str, filename: str):
    """Serve a file from a hosted bot directory."""
    target = BOTS_DIR / str(user_id) / bot_name / filename
    if not target.exists() or not target.is_file():
        abort(404)
    # Security: ensure path is within bots directory
    try:
        target.resolve().relative_to(BOTS_DIR.resolve())
    except ValueError:
        abort(403)
    return send_file(str(target))


@flask_app.route("/status/<bot_id>")
def bot_status_api(bot_id: str):
    bots = get_bots()
    if bot_id not in bots:
        return jsonify({"error": "Bot not found"}), 404
    usage = ProcessManager.get_resource_usage(bot_id)
    return jsonify({**bots[bot_id], **usage})


def run_flask():
    flask_app.run(host=FLASK_HOST, port=FLASK_PORT, debug=False, use_reloader=False)

# ─────────────────────────────────────────────
#  KEYBOARD BUILDERS
# ─────────────────────────────────────────────

def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("New Bot", callback_data="menu_new"),
            InlineKeyboardButton("My Bots", callback_data="menu_list"),
        ],
        [
            InlineKeyboardButton("Help", callback_data="menu_help"),
        ],
    ])


def bot_actions_keyboard(bot_id: str, running: bool):
    row1 = []
    if running:
        row1.append(InlineKeyboardButton("Stop", callback_data=f"action_stop_{bot_id}"))
        row1.append(InlineKeyboardButton("Restart", callback_data=f"action_restart_{bot_id}"))
    else:
        row1.append(InlineKeyboardButton("Start", callback_data=f"action_start_{bot_id}"))

    row2 = [
        InlineKeyboardButton("Logs", callback_data=f"action_logs_{bot_id}"),
        InlineKeyboardButton("Status", callback_data=f"action_status_{bot_id}"),
    ]
    row3 = [
        InlineKeyboardButton("URL", callback_data=f"action_url_{bot_id}"),
        InlineKeyboardButton("Delete", callback_data=f"action_delete_{bot_id}"),
    ]
    row4 = [InlineKeyboardButton("Back to list", callback_data="menu_list")]
    return InlineKeyboardMarkup([row1, row2, row3, row4])


def confirm_delete_keyboard(bot_id: str):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Yes, delete", callback_data=f"confirm_delete_{bot_id}"),
            InlineKeyboardButton("Cancel", callback_data=f"action_view_{bot_id}"),
        ]
    ])

# ─────────────────────────────────────────────
#  COMMAND HANDLERS
# ─────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    register_user(user.id, user.username or "", user.full_name or "")
    text = (
        f"Welcome to the Bot Hosting Platform, {user.first_name}.\n\n"
        "You can upload, run, and manage your bots from here.\n"
        "Use the buttons below or type /help to see all commands."
    )
    await update.message.reply_text(text, reply_markup=main_menu_keyboard())


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Available Commands\n"
        "──────────────────\n"
        "/start       - Welcome screen\n"
        "/new         - Upload and deploy a new bot (zip file)\n"
        "/list        - List all your bots with their IDs\n"
        "/stop ID     - Stop a running bot\n"
        "/startbot ID - Start a stopped bot\n"
        "/restart ID  - Restart a bot\n"
        "/delete ID   - Permanently delete a bot\n"
        "/logs ID     - View last 20 log lines\n"
        "/status ID   - View CPU, RAM, uptime\n"
        "/url ID      - Get direct file URL (Flask bots)\n"
        "/help        - Show this message\n\n"
        "How to deploy a bot:\n"
        "1. Compress your bot files into a ZIP archive.\n"
        "2. Send /new and upload the ZIP file.\n"
        "3. The platform detects the bot type automatically.\n"
        "4. Confirm and the bot starts immediately.\n\n"
        f"Storage limit: {MAX_STORAGE_MB} MB per user.\n"
        f"Bot limit: {MAX_BOTS_USER} bots per user."
    )
    await update.message.reply_text(text)


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    register_user(user.id, user.username or "", user.full_name or "")

    # Check bot count
    bots = get_bots()
    user_bots = [b for b in bots.values() if b["user_id"] == user.id]
    if len(user_bots) >= MAX_BOTS_USER:
        await update.message.reply_text(
            f"You have reached the maximum of {MAX_BOTS_USER} bots.\n"
            "Delete an existing bot before adding a new one."
        )
        return ConversationHandler.END

    # Check storage
    used_mb = get_user_storage_mb(user.id)
    if used_mb >= MAX_STORAGE_MB:
        await update.message.reply_text(
            f"Storage limit reached ({MAX_STORAGE_MB} MB).\n"
            "Delete some bots to free space."
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "Send me your bot as a ZIP file.\n"
        "The archive should contain your entry file at the root level.\n\n"
        "Supported: Python, Node.js, PHP, HTML, Flask.\n"
        "Send /cancel to abort."
    )
    return WAIT_FILE


async def handle_zip_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    doc = update.message.document

    if not doc or not doc.file_name.lower().endswith(".zip"):
        await update.message.reply_text(
            "Please send a valid ZIP file.\n"
            "Send /cancel to abort."
        )
        return WAIT_FILE

    # Check storage
    used_mb = get_user_storage_mb(user.id)
    file_mb = (doc.file_size or 0) / (1024 * 1024)
    if used_mb + file_mb > MAX_STORAGE_MB:
        await update.message.reply_text(
            f"This file would exceed your storage limit of {MAX_STORAGE_MB} MB.\n"
            f"Currently used: {used_mb:.1f} MB."
        )
        return ConversationHandler.END

    await update.message.reply_text("Downloading and extracting your file...")

    bot_id = generate_bot_id()
    user_dir = BOTS_DIR / str(user.id)
    zip_path = user_dir / f"{bot_id}.zip"
    user_dir.mkdir(parents=True, exist_ok=True)

    try:
        tg_file = await context.bot.get_file(doc.file_id)
        await tg_file.download_to_drive(str(zip_path))

        # Extract
        extract_dir = user_dir / bot_id
        extract_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path, "r") as zf:
            # Security: prevent path traversal in zip
            for member in zf.namelist():
                target = (extract_dir / member).resolve()
                if not str(target).startswith(str(extract_dir.resolve())):
                    raise ValueError(f"Path traversal detected in zip: {member}")
            zf.extractall(str(extract_dir))

        zip_path.unlink(missing_ok=True)

        # Flatten single top-level directory
        items = list(extract_dir.iterdir())
        if len(items) == 1 and items[0].is_dir():
            inner = items[0]
            for f in inner.iterdir():
                shutil.move(str(f), str(extract_dir / f.name))
            inner.rmdir()

        bot_type, start_cmd, entry_file = ProcessManager.detect_bot_type(extract_dir)

        context.user_data["pending_bot"] = {
            "id": bot_id,
            "bot_type": bot_type,
            "start_command": start_cmd,
            "entry_file": entry_file,
            "extract_dir": str(extract_dir),
            "user_id": user.id,
        }

        detected_info = (
            f"Extraction complete.\n\n"
            f"Detected type: {bot_type.replace('_', ' ').title()}\n"
            f"Entry file:    {entry_file or 'N/A'}\n"
            f"Start command: {start_cmd or 'N/A (static file)'}\n\n"
            "What name should this bot have? (letters, digits, underscores only)\n"
            "Example: my_bot\n\n"
            "Send /cancel to abort."
        )
        await update.message.reply_text(detected_info)
        return WAIT_NAME

    except Exception as e:
        logger.error(f"Upload error for user {user.id}: {e}")
        shutil.rmtree(str(user_dir / bot_id), ignore_errors=True)
        zip_path.unlink(missing_ok=True)
        await update.message.reply_text(f"Error processing file: {e}")
        return ConversationHandler.END


async def handle_bot_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    import re
    if not re.match(r"^[a-zA-Z0-9_]{1,32}$", name):
        await update.message.reply_text(
            "Invalid name. Use only letters, digits, and underscores (max 32 chars).\n"
            "Try again or send /cancel."
        )
        return WAIT_NAME

    pending = context.user_data.get("pending_bot", {})
    pending["name"] = name
    context.user_data["pending_bot"] = pending

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Confirm and Deploy", callback_data="deploy_confirm"),
            InlineKeyboardButton("Cancel", callback_data="deploy_cancel"),
        ]
    ])
    summary = (
        f"Ready to deploy:\n\n"
        f"Name:    {name}\n"
        f"Type:    {pending['bot_type'].replace('_', ' ').title()}\n"
        f"Command: {pending['start_command'] or 'Static (no process)'}\n\n"
        "Confirm deployment?"
    )
    await update.message.reply_text(summary, reply_markup=keyboard)
    return WAIT_CONFIRM


async def handle_deploy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "deploy_cancel":
        pending = context.user_data.pop("pending_bot", {})
        if pending.get("extract_dir"):
            shutil.rmtree(pending["extract_dir"], ignore_errors=True)
        await query.edit_message_text("Deployment cancelled.")
        return ConversationHandler.END

    # Confirm
    pending = context.user_data.pop("pending_bot", {})
    user = update.effective_user
    bot_id    = pending["id"]
    name      = pending["name"]
    bot_type  = pending["bot_type"]
    start_cmd = pending["start_command"]
    entry_file = pending["entry_file"]
    extract_dir = Path(pending["extract_dir"])

    # Rename directory to bot name
    final_dir = BOTS_DIR / str(user.id) / name
    if final_dir.exists():
        shutil.rmtree(str(final_dir))
    shutil.move(str(extract_dir), str(final_dir))

    log_file = LOGS_DIR / str(user.id) / f"{bot_id}.log"

    bot_record = {
        "id": bot_id,
        "name": name,
        "user_id": user.id,
        "bot_type": bot_type,
        "start_command": start_cmd,
        "entry_file": entry_file,
        "directory": str(final_dir),
        "log_file": str(log_file),
        "created": datetime.utcnow().isoformat(),
        "status": "stopped",
        "port": None,
        "url": None,
    }

    # Build URL for Flask bots
    if bot_type == "python_flask" and entry_file:
        bot_record["url"] = f"{SERVER_HOST}/{user.id}/{name}/{entry_file}"

    bots = get_bots()
    bots[bot_id] = bot_record
    save_bots(bots)

    # Start the process
    result_text = ""
    if start_cmd:
        try:
            ProcessManager.start(bot_id, final_dir, start_cmd, log_file)
            bots[bot_id]["status"] = "running"
            save_bots(bots)
            result_text = (
                f"Bot deployed and started successfully.\n\n"
                f"ID:      {bot_id}\n"
                f"Name:    {name}\n"
                f"Type:    {bot_type.replace('_', ' ').title()}\n"
                f"Status:  Running\n"
            )
            if bot_record["url"]:
                result_text += f"URL:     {bot_record['url']}\n"
            result_text += f"\nUse /logs {bot_id} to see output."
        except Exception as e:
            result_text = (
                f"Bot saved but failed to start: {e}\n"
                f"ID: {bot_id}\n"
                f"Use /startbot {bot_id} to retry."
            )
    else:
        result_text = (
            f"Static bot deployed (no process).\n\n"
            f"ID:   {bot_id}\n"
            f"Name: {name}\n"
        )
        if bot_record["url"]:
            result_text += f"URL:  {bot_record['url']}\n"

    await query.edit_message_text(result_text)
    return ConversationHandler.END


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending = context.user_data.pop("pending_bot", {})
    if pending.get("extract_dir"):
        shutil.rmtree(pending["extract_dir"], ignore_errors=True)
    await update.message.reply_text("Operation cancelled.")
    return ConversationHandler.END


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    bots = get_bots()
    user_bots = [b for b in bots.values() if b["user_id"] == user.id]

    if not user_bots:
        await update.message.reply_text(
            "You have no deployed bots yet.\n"
            "Use /new to deploy your first bot."
        )
        return

    lines = [f"Your bots ({len(user_bots)}/{MAX_BOTS_USER}):\n"]
    buttons = []
    for b in sorted(user_bots, key=lambda x: x["created"]):
        running = ProcessManager.is_running(b["id"])
        status = "Running" if running else "Stopped"
        lines.append(f"- {b['name']} ({b['id']}) | {status} | {b['bot_type'].replace('_',' ').title()}")
        buttons.append([InlineKeyboardButton(f"Manage: {b['name']}", callback_data=f"action_view_{b['id']}")])

    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(buttons) if buttons else None,
    )


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /stop <bot_id>")
        return
    await _stop_bot(update, context, args[0])


async def cmd_startbot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /startbot <bot_id>")
        return
    await _start_bot(update, context, args[0])


async def cmd_restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /restart <bot_id>")
        return
    bot_id = args[0]
    bots = get_bots()
    if not _check_bot_owner(update, bots, bot_id):
        await update.message.reply_text("Bot not found or access denied.")
        return
    ProcessManager.stop(bot_id)
    await asyncio.sleep(1)
    await _start_bot(update, context, bot_id)


async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /delete <bot_id>")
        return
    bot_id = args[0]
    bots = get_bots()
    if not _check_bot_owner(update, bots, bot_id):
        await update.message.reply_text("Bot not found or access denied.")
        return
    keyboard = confirm_delete_keyboard(bot_id)
    await update.message.reply_text(
        f"Are you sure you want to permanently delete bot '{bots[bot_id]['name']}' ({bot_id})?\n"
        "This cannot be undone.",
        reply_markup=keyboard,
    )


async def cmd_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /logs <bot_id>")
        return
    bot_id = args[0]
    bots = get_bots()
    if not _check_bot_owner(update, bots, bot_id):
        await update.message.reply_text("Bot not found or access denied.")
        return
    log_file = Path(bots[bot_id]["log_file"])
    tail = get_log_tail(log_file, 20)
    await update.message.reply_text(
        f"Last 20 lines of logs for {bots[bot_id]['name']} ({bot_id}):\n\n"
        f"```\n{tail}\n```",
        parse_mode="Markdown",
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /status <bot_id>")
        return
    bot_id = args[0]
    bots = get_bots()
    if not _check_bot_owner(update, bots, bot_id):
        await update.message.reply_text("Bot not found or access denied.")
        return
    await _send_status(update, bots[bot_id])


async def cmd_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /url <bot_id>")
        return
    bot_id = args[0]
    bots = get_bots()
    if not _check_bot_owner(update, bots, bot_id):
        await update.message.reply_text("Bot not found or access denied.")
        return
    bot = bots[bot_id]
    if bot.get("url"):
        await update.message.reply_text(
            f"Direct URL for {bot['name']}:\n{bot['url']}"
        )
    else:
        await update.message.reply_text(
            f"No URL available for {bot['name']}.\n"
            "URLs are only generated for Flask bots (app.py containing Flask)."
        )

# ─────────────────────────────────────────────
#  OWNER COMMANDS
# ─────────────────────────────────────────────

def owner_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != OWNER_ID:
            await update.message.reply_text("This command is restricted to the bot owner.")
            return
        return await func(update, context)
    wrapper.__name__ = func.__name__
    return wrapper


@owner_only
async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /broadcast <message>")
        return
    message = " ".join(context.args)
    users = get_users()
    sent, failed = 0, 0
    for uid_str, u in users.items():
        try:
            await context.bot.send_message(
                chat_id=int(uid_str),
                text=f"[Broadcast]\n\n{message}",
            )
            sent += 1
        except Exception:
            failed += 1
    await update.message.reply_text(f"Broadcast complete.\nSent: {sent} | Failed: {failed}")


@owner_only
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users = get_users()
    bots = get_bots()
    running = sum(1 for b in bots.values() if ProcessManager.is_running(b["id"]))

    sys_cpu = psutil.cpu_percent(interval=1)
    sys_mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")

    text = (
        "System Statistics\n"
        f"─────────────────\n"
        f"Total users:   {len(users)}\n"
        f"Total bots:    {len(bots)}\n"
        f"Running bots:  {running}\n\n"
        f"CPU usage:     {sys_cpu}%\n"
        f"RAM total:     {sys_mem.total / 1024**3:.1f} GB\n"
        f"RAM used:      {sys_mem.used / 1024**3:.1f} GB ({sys_mem.percent}%)\n"
        f"Disk total:    {disk.total / 1024**3:.1f} GB\n"
        f"Disk used:     {disk.used / 1024**3:.1f} GB ({disk.percent}%)\n"
    )
    await update.message.reply_text(text)


@owner_only
async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users = get_users()
    if not users:
        await update.message.reply_text("No registered users.")
        return
    lines = ["Registered users:\n"]
    for u in list(users.values())[:50]:  # Cap display at 50
        lines.append(
            f"- {u.get('full_name','?')} (@{u.get('username','?')}) "
            f"| ID: {u['id']} | Joined: {u['joined'][:10]}"
        )
    if len(users) > 50:
        lines.append(f"\n...and {len(users) - 50} more.")
    await update.message.reply_text("\n".join(lines))

# ─────────────────────────────────────────────
#  INLINE BUTTON CALLBACKS
# ─────────────────────────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "menu_new":
        await query.message.reply_text(
            "Send me your bot as a ZIP file.\n"
            "The archive should contain your entry file at the root level.\n\n"
            "Supported: Python, Node.js, PHP, HTML, Flask.\n"
            "Send /cancel to abort."
        )
        context.user_data["_conv_state"] = WAIT_FILE
        return

    if data == "menu_list":
        user = update.effective_user
        bots = get_bots()
        user_bots = [b for b in bots.values() if b["user_id"] == user.id]
        if not user_bots:
            await query.edit_message_text(
                "You have no deployed bots yet.\n"
                "Use /new to deploy your first bot.",
                reply_markup=main_menu_keyboard(),
            )
            return
        lines = [f"Your bots ({len(user_bots)}/{MAX_BOTS_USER}):\n"]
        buttons = []
        for b in sorted(user_bots, key=lambda x: x["created"]):
            running = ProcessManager.is_running(b["id"])
            status = "Running" if running else "Stopped"
            lines.append(f"- {b['name']} ({b['id']}) | {status}")
            buttons.append([InlineKeyboardButton(f"Manage: {b['name']}", callback_data=f"action_view_{b['id']}")])
        buttons.append([InlineKeyboardButton("Back", callback_data="menu_main")])
        await query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons))
        return

    if data == "menu_help":
        await query.edit_message_text(
            "Use /help for the full command list.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="menu_main")]]),
        )
        return

    if data == "menu_main":
        await query.edit_message_text(
            "Main menu:",
            reply_markup=main_menu_keyboard(),
        )
        return

    # Bot-specific actions
    if data.startswith("action_view_"):
        bot_id = data[len("action_view_"):]
        bots = get_bots()
        if bot_id not in bots:
            await query.edit_message_text("Bot not found.")
            return
        bot = bots[bot_id]
        running = ProcessManager.is_running(bot_id)
        text = (
            f"Bot: {bot['name']}\n"
            f"ID:  {bot_id}\n"
            f"Type: {bot['bot_type'].replace('_',' ').title()}\n"
            f"Status: {'Running' if running else 'Stopped'}\n"
            f"Created: {bot['created'][:10]}\n"
        )
        await query.edit_message_text(text, reply_markup=bot_actions_keyboard(bot_id, running))
        return

    if data.startswith("action_stop_"):
        bot_id = data[len("action_stop_"):]
        await _stop_bot_inline(query, bot_id)
        return

    if data.startswith("action_start_"):
        bot_id = data[len("action_start_"):]
        await _start_bot_inline(query, context, bot_id)
        return

    if data.startswith("action_restart_"):
        bot_id = data[len("action_restart_"):]
        bots = get_bots()
        if bot_id not in bots:
            await query.edit_message_text("Bot not found.")
            return
        ProcessManager.stop(bot_id)
        await asyncio.sleep(1)
        await _start_bot_inline(query, context, bot_id)
        return

    if data.startswith("action_logs_"):
        bot_id = data[len("action_logs_"):]
        bots = get_bots()
        if bot_id not in bots:
            await query.edit_message_text("Bot not found.")
            return
        log_file = Path(bots[bot_id]["log_file"])
        tail = get_log_tail(log_file, 20)
        await query.edit_message_text(
            f"Logs for {bots[bot_id]['name']}:\n\n```\n{tail}\n```",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Back", callback_data=f"action_view_{bot_id}")]
            ]),
        )
        return

    if data.startswith("action_status_"):
        bot_id = data[len("action_status_"):]
        bots = get_bots()
        if bot_id not in bots:
            await query.edit_message_text("Bot not found.")
            return
        usage = ProcessManager.get_resource_usage(bot_id)
        if usage.get("running"):
            text = (
                f"Status for {bots[bot_id]['name']}:\n\n"
                f"PID:     {usage['pid']}\n"
                f"CPU:     {usage['cpu_percent']}%\n"
                f"RAM:     {usage['ram_mb']} MB\n"
                f"Uptime:  {usage['uptime_human']}\n"
            )
        else:
            text = f"Bot {bots[bot_id]['name']} is not running."
        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Back", callback_data=f"action_view_{bot_id}")]
            ]),
        )
        return

    if data.startswith("action_url_"):
        bot_id = data[len("action_url_"):]
        bots = get_bots()
        if bot_id not in bots:
            await query.edit_message_text("Bot not found.")
            return
        bot = bots[bot_id]
        if bot.get("url"):
            text = f"URL for {bot['name']}:\n{bot['url']}"
        else:
            text = "No URL available. URLs are only generated for Flask bots."
        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Back", callback_data=f"action_view_{bot_id}")]
            ]),
        )
        return

    if data.startswith("action_delete_"):
        bot_id = data[len("action_delete_"):]
        bots = get_bots()
        if bot_id not in bots:
            await query.edit_message_text("Bot not found.")
            return
        await query.edit_message_text(
            f"Are you sure you want to permanently delete '{bots[bot_id]['name']}'?\n"
            "This cannot be undone.",
            reply_markup=confirm_delete_keyboard(bot_id),
        )
        return

    if data.startswith("confirm_delete_"):
        bot_id = data[len("confirm_delete_"):]
        bots = get_bots()
        if bot_id not in bots:
            await query.edit_message_text("Bot not found.")
            return
        bot = bots[bot_id]
        ProcessManager.stop(bot_id)
        shutil.rmtree(bot["directory"], ignore_errors=True)
        log_path = Path(bot["log_file"])
        log_path.unlink(missing_ok=True)
        del bots[bot_id]
        save_bots(bots)
        await query.edit_message_text(
            f"Bot '{bot['name']}' has been deleted.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Back to list", callback_data="menu_list")]
            ]),
        )
        return

# ─────────────────────────────────────────────
#  PRIVATE HELPERS
# ─────────────────────────────────────────────

def _check_bot_owner(update: Update, bots: dict, bot_id: str) -> bool:
    if bot_id not in bots:
        return False
    if bots[bot_id]["user_id"] != update.effective_user.id:
        return update.effective_user.id == OWNER_ID
    return True


async def _stop_bot(update, context, bot_id: str):
    bots = get_bots()
    if not _check_bot_owner(update, bots, bot_id):
        await update.message.reply_text("Bot not found or access denied.")
        return
    ProcessManager.stop(bot_id)
    bots[bot_id]["status"] = "stopped"
    save_bots(bots)
    await update.message.reply_text(f"Bot '{bots[bot_id]['name']}' stopped.")


async def _start_bot(update, context, bot_id: str):
    bots = get_bots()
    if not _check_bot_owner(update, bots, bot_id):
        await update.message.reply_text("Bot not found or access denied.")
        return
    bot = bots[bot_id]
    if not bot.get("start_command"):
        await update.message.reply_text("This bot has no start command (static file).")
        return
    if ProcessManager.is_running(bot_id):
        await update.message.reply_text("Bot is already running.")
        return
    try:
        ProcessManager.start(bot_id, Path(bot["directory"]), bot["start_command"], Path(bot["log_file"]))
        bots[bot_id]["status"] = "running"
        save_bots(bots)
        await update.message.reply_text(f"Bot '{bot['name']}' started.")
    except Exception as e:
        await update.message.reply_text(f"Failed to start bot: {e}")


async def _stop_bot_inline(query, bot_id: str):
    bots = get_bots()
    if bot_id not in bots:
        await query.edit_message_text("Bot not found.")
        return
    ProcessManager.stop(bot_id)
    bots[bot_id]["status"] = "stopped"
    save_bots(bots)
    await query.edit_message_text(
        f"Bot '{bots[bot_id]['name']}' stopped.",
        reply_markup=bot_actions_keyboard(bot_id, False),
    )


async def _start_bot_inline(query, context, bot_id: str):
    bots = get_bots()
    if bot_id not in bots:
        await query.edit_message_text("Bot not found.")
        return
    bot = bots[bot_id]
    if not bot.get("start_command"):
        await query.edit_message_text("This bot has no start command.")
        return
    try:
        ProcessManager.start(bot_id, Path(bot["directory"]), bot["start_command"], Path(bot["log_file"]))
        bots[bot_id]["status"] = "running"
        save_bots(bots)
        await query.edit_message_text(
            f"Bot '{bot['name']}' started.",
            reply_markup=bot_actions_keyboard(bot_id, True),
        )
    except Exception as e:
        await query.edit_message_text(f"Failed to start: {e}")


async def _send_status(update, bot: dict):
    bot_id = bot["id"]
    usage = ProcessManager.get_resource_usage(bot_id)
    if usage.get("running"):
        text = (
            f"Status for {bot['name']}:\n\n"
            f"PID:     {usage['pid']}\n"
            f"CPU:     {usage['cpu_percent']}%\n"
            f"RAM:     {usage['ram_mb']} MB\n"
            f"Uptime:  {usage['uptime_human']}\n"
        )
    else:
        text = f"Bot {bot['name']} is not currently running."
    await update.message.reply_text(text)

# ─────────────────────────────────────────────
#  WATCHDOG (auto-restart crashed bots)
# ─────────────────────────────────────────────

def watchdog_loop():
    """Runs in background thread, restarts bots that crashed."""
    while True:
        try:
            bots = get_bots()
            for bot_id, bot in bots.items():
                if bot.get("status") == "running" and not ProcessManager.is_running(bot_id):
                    logger.warning(f"Bot {bot_id} ({bot['name']}) crashed. Attempting restart.")
                    try:
                        ProcessManager.start(
                            bot_id,
                            Path(bot["directory"]),
                            bot["start_command"],
                            Path(bot["log_file"]),
                        )
                    except Exception as e:
                        logger.error(f"Watchdog failed to restart {bot_id}: {e}")
                        bots[bot_id]["status"] = "crashed"
                        save_bots(bots)
        except Exception as e:
            logger.error(f"Watchdog error: {e}")
        time.sleep(30)

# ─────────────────────────────────────────────
#  APPLICATION SETUP
# ─────────────────────────────────────────────

def build_application() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()

    # Conversation handler for /new
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("new", cmd_new),
        ],
        states={
            WAIT_FILE: [
                MessageHandler(filters.Document.ALL, handle_zip_upload),
                CommandHandler("cancel", cmd_cancel),
            ],
            WAIT_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_bot_name),
                CommandHandler("cancel", cmd_cancel),
            ],
            WAIT_CONFIRM: [
                CallbackQueryHandler(handle_deploy_callback, pattern="^deploy_"),
                CommandHandler("cancel", cmd_cancel),
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
    )

    app.add_handler(conv_handler)

    # General commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("startbot", cmd_startbot))
    app.add_handler(CommandHandler("restart", cmd_restart))
    app.add_handler(CommandHandler("delete", cmd_delete))
    app.add_handler(CommandHandler("logs", cmd_logs))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("url", cmd_url))

    # Owner commands
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("users", cmd_users))

    # Inline buttons (general)
    app.add_handler(CallbackQueryHandler(handle_callback))

    return app


async def set_bot_commands(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start", "Welcome screen"),
        BotCommand("new", "Deploy a new bot"),
        BotCommand("list", "List your bots"),
        BotCommand("stop", "Stop a bot"),
        BotCommand("startbot", "Start a stopped bot"),
        BotCommand("restart", "Restart a bot"),
        BotCommand("delete", "Delete a bot"),
        BotCommand("logs", "View bot logs"),
        BotCommand("status", "View bot resource usage"),
        BotCommand("url", "Get bot URL"),
        BotCommand("help", "Help"),
    ])


# ─────────────────────────────────────────────
#  MAIN ENTRY POINT
# ─────────────────────────────────────────────

def main():
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("ERROR: Please set your BOT_TOKEN in the configuration section at the top of main.py")
        sys.exit(1)

    if OWNER_ID == 123456789:
        print("WARNING: OWNER_ID is still the default placeholder. Set your actual Telegram user ID.")

    logger.info("Starting Bot Hosting Platform")

    # Start Flask in background thread
    flask_thread = threading.Thread(target=run_flask, daemon=True, name="FlaskServer")
    flask_thread.start()
    logger.info(f"Flask server started on port {FLASK_PORT}")

    # Start watchdog
    watchdog_thread = threading.Thread(target=watchdog_loop, daemon=True, name="Watchdog")
    watchdog_thread.start()
    logger.info("Watchdog started")

    # Restore previously running bots
    bots = get_bots()
    restored = 0
    for bot_id, bot in bots.items():
        if bot.get("status") == "running" and bot.get("start_command"):
            try:
                ProcessManager.start(
                    bot_id,
                    Path(bot["directory"]),
                    bot["start_command"],
                    Path(bot["log_file"]),
                )
                restored += 1
            except Exception as e:
                logger.error(f"Could not restore bot {bot_id}: {e}")
    if restored:
        logger.info(f"Restored {restored} previously running bot(s)")

    # Build and run Telegram bot
    application = build_application()

    async def post_init(app):
        await set_bot_commands(app)

    application.post_init = post_init

    logger.info("Telegram bot starting (polling mode)")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
