"""
بوت تليجرام لاستضافة APIs وملفات HTML
======================================
المتطلبات:
    pip install python-telegram-bot[webhooks]>=20.0 flask psutil aiofiles aiohttp

الإعداد:
    1. غيّر BOT_TOKEN بتوكن البوت من BotFather
    2. غيّر OWNER_ID بمعرفك الرقمي
    3. غيّر BASE_URL برابط سيرفرك (مثل http://your-domain.com)
    4. غيّر HOST_PORT إذا أردت منفذاً مختلفاً لخادم الملفات
"""

import asyncio
import hashlib
import json
import logging
import os
import random
import re
import shutil
import signal
import sqlite3
import string
import subprocess
import sys
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import psutil
from flask import Flask, Response, abort, request, send_from_directory
from telegram import (
    Bot,
    CallbackQuery,
    Chat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ═══════════════════════════════════════════
#   الإعدادات الرئيسية — غيّرها قبل التشغيل
# ═══════════════════════════════════════════
BOT_TOKEN = "8791101593:AAEbW3M77kiiL5NghFICpl0iDInpVKh0VwY"
OWNER_ID = 8487397448          # معرفك الرقمي
BASE_URL = "https://host-masjon.onrender.com"  # رابط السيرفر بدون / في النهاية
HOST = "0.0.0.0"
HOST_PORT = 8080               # منفذ خادم الملفات
API_PORT_START = 9000          # أول منفذ للـ APIs الديناميكية
API_PORT_END = 9999            # آخر منفذ للـ APIs الديناميكية
MAX_PROJECTS_FREE = 2          # أقصى مشاريع للمستخدم العادي
MAX_PROJECTS_VIP = 10          # أقصى مشاريع لـ VIP
RATE_LIMIT_PER_MINUTE = 30     # أقصى طلبات في الدقيقة
WATCHDOG_INTERVAL = 30         # ثانية بين كل فحص للـ watchdog
MANDATORY_CHANNELS = []        # أضف قنوات إجبارية: ["@channel1", "@channel2"]

# ═══════════════════════════════════════════
#   المسارات
# ═══════════════════════════════════════════
BASE_DIR = Path(__file__).parent
PROJECTS_DIR = BASE_DIR / "projects"
DB_PATH = BASE_DIR / "bot_data.db"
LOGS_DIR = BASE_DIR / "logs"

PROJECTS_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)

# ═══════════════════════════════════════════
#   Logging
# ═══════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOGS_DIR / "bot.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("TelegramHostBot")

# ═══════════════════════════════════════════
#   الكلمات الخطيرة الممنوعة في الملفات
# ═══════════════════════════════════════════
DANGEROUS_PATTERNS = [
    r"rm\s+-rf\s+/",
    r"curl\s+.*\|\s*sh",
    r"wget\s+.*\|\s*sh",
    r"bash\s+-c\s+.*eval",
    r"exec\s*\(\s*['\"].*rm",
    r"os\.system\s*\(['\"].*rm\s+-rf",
    r"subprocess.*rm\s+-rf",
    r"__import__\s*\(\s*['\"]os['\"]",
    r"open\s*\(['\"]\/etc\/(passwd|shadow)",
    r"\.\.\s*/\s*\.\.\s*/",
    r"chmod\s+777\s+/",
    r"mkfs\.",
    r"dd\s+if=.+of=\/dev",
]

# ═══════════════════════════════════════════
#   قاعدة البيانات
# ═══════════════════════════════════════════
class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _get_conn(self):
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._lock:
            conn = self._get_conn()
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id     INTEGER PRIMARY KEY,
                    username    TEXT,
                    full_name   TEXT,
                    user_type   TEXT DEFAULT 'free',
                    joined_at   TEXT DEFAULT (datetime('now')),
                    is_banned   INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS projects (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id      INTEGER,
                    project_name TEXT,
                    project_type TEXT,
                    status       TEXT DEFAULT 'stopped',
                    port         INTEGER,
                    path         TEXT,
                    url          TEXT,
                    created_at   TEXT DEFAULT (datetime('now')),
                    pid          INTEGER,
                    UNIQUE(user_id, project_name)
                );

                CREATE TABLE IF NOT EXISTS activation_codes (
                    code         TEXT PRIMARY KEY,
                    user_type    TEXT DEFAULT 'vip',
                    duration_hrs INTEGER DEFAULT 720,
                    max_projects INTEGER DEFAULT 10,
                    used_by      INTEGER,
                    used_at      TEXT,
                    created_at   TEXT DEFAULT (datetime('now')),
                    expires_at   TEXT
                );

                CREATE TABLE IF NOT EXISTS rate_limits (
                    user_id    INTEGER PRIMARY KEY,
                    requests   INTEGER DEFAULT 0,
                    window_start TEXT DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS project_logs (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id    INTEGER,
                    project_id INTEGER,
                    event      TEXT,
                    message    TEXT,
                    created_at TEXT DEFAULT (datetime('now'))
                );
            """)
            conn.commit()
            conn.close()

    def execute(self, query: str, params=(), fetchone=False, fetchall=False):
        with self._lock:
            conn = self._get_conn()
            try:
                cur = conn.execute(query, params)
                conn.commit()
                if fetchone:
                    return cur.fetchone()
                if fetchall:
                    return cur.fetchall()
                return cur
            finally:
                conn.close()

    # ─── Users ───
    def get_user(self, user_id: int):
        return self.execute(
            "SELECT * FROM users WHERE user_id=?", (user_id,), fetchone=True
        )

    def upsert_user(self, user_id: int, username: str, full_name: str):
        self.execute(
            """INSERT INTO users (user_id, username, full_name)
               VALUES (?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET
                   username=excluded.username,
                   full_name=excluded.full_name""",
            (user_id, username, full_name),
        )

    def get_all_users(self):
        return self.execute("SELECT * FROM users ORDER BY joined_at DESC", fetchall=True)

    def set_user_type(self, user_id: int, user_type: str):
        self.execute("UPDATE users SET user_type=? WHERE user_id=?", (user_type, user_id))

    # ─── Projects ───
    def get_projects(self, user_id: int):
        return self.execute(
            "SELECT * FROM projects WHERE user_id=? ORDER BY created_at DESC",
            (user_id,), fetchall=True,
        )

    def get_project(self, project_id: int):
        return self.execute(
            "SELECT * FROM projects WHERE id=?", (project_id,), fetchone=True
        )

    def get_project_by_name(self, user_id: int, name: str):
        return self.execute(
            "SELECT * FROM projects WHERE user_id=? AND project_name=?",
            (user_id, name), fetchone=True,
        )

    def create_project(self, user_id, name, ptype, path, url, port=None):
        self.execute(
            """INSERT INTO projects (user_id, project_name, project_type, path, url, port)
               VALUES (?,?,?,?,?,?)""",
            (user_id, name, ptype, path, url, port),
        )
        return self.execute(
            "SELECT * FROM projects WHERE user_id=? AND project_name=?",
            (user_id, name), fetchone=True,
        )

    def update_project_status(self, project_id: int, status: str, pid: int = None):
        self.execute(
            "UPDATE projects SET status=?, pid=? WHERE id=?",
            (status, pid, project_id),
        )

    def update_project_port(self, project_id: int, port: int):
        self.execute("UPDATE projects SET port=? WHERE id=?", (port, project_id))

    def delete_project(self, project_id: int):
        self.execute("DELETE FROM projects WHERE id=?", (project_id,))

    def count_projects(self, user_id: int):
        row = self.execute(
            "SELECT COUNT(*) as c FROM projects WHERE user_id=?", (user_id,), fetchone=True
        )
        return row["c"] if row else 0

    # ─── Activation Codes ───
    def create_code(self, code: str, user_type: str, duration_hrs: int, max_projects: int):
        expires = (datetime.now() + timedelta(hours=duration_hrs)).isoformat()
        self.execute(
            """INSERT INTO activation_codes
               (code, user_type, duration_hrs, max_projects, expires_at)
               VALUES (?,?,?,?,?)""",
            (code, user_type, duration_hrs, max_projects, expires),
        )

    def get_code(self, code: str):
        return self.execute(
            "SELECT * FROM activation_codes WHERE code=?", (code,), fetchone=True
        )

    def use_code(self, code: str, user_id: int):
        self.execute(
            "UPDATE activation_codes SET used_by=?, used_at=datetime('now') WHERE code=?",
            (user_id, code),
        )

    # ─── Rate Limiting ───
    def check_rate_limit(self, user_id: int) -> bool:
        """يرجع True إذا كان مسموحاً، False إذا تجاوز الحد"""
        row = self.execute(
            "SELECT * FROM rate_limits WHERE user_id=?", (user_id,), fetchone=True
        )
        now = datetime.now()
        if row is None:
            self.execute(
                "INSERT INTO rate_limits (user_id, requests, window_start) VALUES (?,1,?)",
                (user_id, now.isoformat()),
            )
            return True
        window_start = datetime.fromisoformat(row["window_start"])
        if (now - window_start).total_seconds() > 60:
            self.execute(
                "UPDATE rate_limits SET requests=1, window_start=? WHERE user_id=?",
                (now.isoformat(), user_id),
            )
            return True
        if row["requests"] >= RATE_LIMIT_PER_MINUTE:
            return False
        self.execute(
            "UPDATE rate_limits SET requests=requests+1 WHERE user_id=?", (user_id,)
        )
        return True

    # ─── Logs ───
    def add_log(self, user_id: int, project_id: int, event: str, message: str):
        self.execute(
            "INSERT INTO project_logs (user_id, project_id, event, message) VALUES (?,?,?,?)",
            (user_id, project_id, event, message),
        )

    def get_logs(self, user_id: int = None, project_id: int = None, limit: int = 50):
        if user_id and project_id:
            return self.execute(
                "SELECT * FROM project_logs WHERE user_id=? AND project_id=? ORDER BY created_at DESC LIMIT ?",
                (user_id, project_id, limit), fetchall=True,
            )
        elif user_id:
            return self.execute(
                "SELECT * FROM project_logs WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
                (user_id, limit), fetchall=True,
            )
        else:
            return self.execute(
                "SELECT * FROM project_logs ORDER BY created_at DESC LIMIT ?",
                (limit,), fetchall=True,
            )


db = Database(str(DB_PATH))

# ═══════════════════════════════════════════
#   إدارة المنافذ
# ═══════════════════════════════════════════
_used_ports: set = set()
_ports_lock = threading.Lock()


def get_free_port() -> int:
    with _ports_lock:
        used_by_system = {conn.laddr.port for conn in psutil.net_connections()}
        for _ in range(200):
            port = random.randint(API_PORT_START, API_PORT_END)
            if port not in _used_ports and port not in used_by_system:
                _used_ports.add(port)
                return port
    raise RuntimeError("لا يوجد منفذ متاح")


def release_port(port: int):
    with _ports_lock:
        _used_ports.discard(port)


# ═══════════════════════════════════════════
#   فحص الملفات الضارة
# ═══════════════════════════════════════════
def scan_file_content(file_path: Path) -> tuple[bool, str]:
    """يرجع (آمن, سبب_الرفض)"""
    try:
        text = file_path.read_text(encoding="utf-8", errors="ignore")
        for pattern in DANGEROUS_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                return False, f"نمط خطير مكتشف: {pattern}"
        return True, ""
    except Exception:
        return True, ""  # ملفات غير نصية (صور، إلخ) تعتبر آمنة


def scan_project_dir(project_path: Path) -> tuple[bool, str]:
    dangerous_exts = {".exe", ".sh", ".bat", ".cmd", ".ps1"}
    for f in project_path.rglob("*"):
        if f.is_file():
            if f.suffix.lower() in dangerous_exts:
                return False, f"امتداد خطير: {f.name}"
            # فحص مسار نسبي (Path Traversal)
            try:
                f.resolve().relative_to(project_path.resolve())
            except ValueError:
                return False, "محاولة Path Traversal مكتشفة"
            safe, reason = scan_file_content(f)
            if not safe:
                return False, f"{f.name}: {reason}"
    return True, ""


# ═══════════════════════════════════════════
#   اكتشاف نوع المشروع
# ═══════════════════════════════════════════
def detect_project_type(project_path: Path) -> str:
    files = list(project_path.rglob("*"))
    names = {f.name.lower() for f in files if f.is_file()}
    contents = {}

    def read(name):
        p = project_path / name
        if p.exists():
            return p.read_text(encoding="utf-8", errors="ignore")
        return ""

    if "package.json" in names:
        return "nodejs"
    if any(n.endswith(".php") for n in names):
        return "php"
    if "app.py" in names:
        txt = read("app.py")
        if "fastapi" in txt.lower():
            return "fastapi"
        if "flask" in txt.lower():
            return "flask"
        return "python"
    if "main.py" in names:
        txt = read("main.py")
        if "fastapi" in txt.lower():
            return "fastapi"
        if "flask" in txt.lower():
            return "flask"
        return "python"
    if any(n.endswith(".html") for n in names):
        return "html"
    return "unknown"


# ═══════════════════════════════════════════
#   تشغيل وإيقاف المشاريع
# ═══════════════════════════════════════════
_running_processes: dict[int, subprocess.Popen] = {}  # project_id -> process
_processes_lock = threading.Lock()


def _get_entry_file(project_path: Path, ptype: str) -> Optional[Path]:
    candidates = {
        "flask": ["app.py", "main.py", "run.py", "server.py"],
        "fastapi": ["app.py", "main.py", "run.py"],
        "python": ["main.py", "app.py", "run.py"],
        "nodejs": ["index.js", "app.js", "server.js", "main.js"],
        "php": ["index.php", "app.php"],
    }
    for name in candidates.get(ptype, []):
        p = project_path / name
        if p.exists():
            return p
    return None


def start_project(project_id: int) -> tuple[bool, str]:
    project = db.get_project(project_id)
    if not project:
        return False, "المشروع غير موجود"

    ptype = project["project_type"]
    path = Path(project["path"])

    if ptype == "html":
        db.update_project_status(project_id, "running")
        db.add_log(project["user_id"], project_id, "START", "مشروع HTML يعمل عبر الخادم الثابت")
        return True, "مشروع HTML يعمل تلقائياً عبر الخادم الثابت"

    port = project["port"]
    if not port:
        port = get_free_port()
        db.update_project_port(project_id, port)

    entry = _get_entry_file(path, ptype)
    if not entry:
        return False, f"لم يُعثر على ملف التشغيل لنوع {ptype}"

    log_file = LOGS_DIR / f"project_{project_id}.log"

    if ptype in ("flask", "python"):
        cmd = [sys.executable, str(entry)]
        env = {**os.environ, "PORT": str(port), "FLASK_RUN_PORT": str(port), "HOST": "0.0.0.0"}
    elif ptype == "fastapi":
        cmd = [sys.executable, "-m", "uvicorn",
               entry.stem + ":app", "--host", "0.0.0.0", "--port", str(port)]
        env = {**os.environ}
    elif ptype == "nodejs":
        cmd = ["node", str(entry)]
        env = {**os.environ, "PORT": str(port)}
    elif ptype == "php":
        cmd = ["php", "-S", f"0.0.0.0:{port}", "-t", str(path)]
        env = {**os.environ}
    else:
        return False, f"نوع غير مدعوم: {ptype}"

    try:
        with open(log_file, "a", encoding="utf-8") as lf:
            proc = subprocess.Popen(
                cmd,
                cwd=str(path),
                env=env,
                stdout=lf,
                stderr=lf,
                preexec_fn=os.setsid if sys.platform != "win32" else None,
            )
        with _processes_lock:
            _running_processes[project_id] = proc
        db.update_project_status(project_id, "running", proc.pid)
        db.add_log(project["user_id"], project_id, "START",
                   f"تم تشغيل المشروع على المنفذ {port} — PID: {proc.pid}")
        logger.info(f"تشغيل مشروع #{project_id} على المنفذ {port} — PID {proc.pid}")
        return True, f"تم التشغيل على المنفذ {port}"
    except Exception as e:
        logger.error(f"خطأ في تشغيل المشروع {project_id}: {e}")
        db.add_log(project["user_id"], project_id, "ERROR", str(e))
        return False, str(e)


def stop_project(project_id: int) -> tuple[bool, str]:
    project = db.get_project(project_id)
    if not project:
        return False, "المشروع غير موجود"

    with _processes_lock:
        proc = _running_processes.pop(project_id, None)

    if proc:
        try:
            if sys.platform != "win32":
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            else:
                proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    if project["port"]:
        release_port(project["port"])

    db.update_project_status(project_id, "stopped", None)
    db.add_log(project["user_id"], project_id, "STOP", "تم إيقاف المشروع")
    logger.info(f"إيقاف مشروع #{project_id}")
    return True, "تم الإيقاف"


# ═══════════════════════════════════════════
#   Watchdog — مراقبة العمليات وإعادة تشغيلها
# ═══════════════════════════════════════════
def watchdog_loop():
    logger.info("Watchdog بدأ")
    while True:
        time.sleep(WATCHDOG_INTERVAL)
        with _processes_lock:
            ids = list(_running_processes.keys())
        for pid in ids:
            with _processes_lock:
                proc = _running_processes.get(pid)
            if proc and proc.poll() is not None:
                logger.warning(f"Watchdog: مشروع #{pid} توقف، جارٍ إعادة التشغيل...")
                project = db.get_project(pid)
                if project:
                    db.add_log(project["user_id"], pid, "WATCHDOG",
                               "أُعيد تشغيل المشروع تلقائياً بواسطة Watchdog")
                with _processes_lock:
                    _running_processes.pop(pid, None)
                db.update_project_status(pid, "stopped")
                start_project(pid)


watchdog_thread = threading.Thread(target=watchdog_loop, daemon=True)
watchdog_thread.start()

# ═══════════════════════════════════════════
#   خادم Flask للملفات الثابتة والـ APIs
# ═══════════════════════════════════════════
flask_app = Flask(__name__)
ALLOWED_EXTENSIONS = {".html", ".css", ".js", ".png", ".jpg", ".jpeg",
                      ".ico", ".json", ".svg", ".gif", ".woff", ".woff2", ".ttf"}


@flask_app.after_request
def add_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    return response


@flask_app.route("/static/<int:user_id>/<project_name>/", defaults={"filename": "index.html"})
@flask_app.route("/static/<int:user_id>/<project_name>/<path:filename>")
def serve_static(user_id, project_name, filename):
    # منع Path Traversal
    if ".." in filename or filename.startswith("/"):
        abort(403)

    project = db.get_project_by_name(user_id, project_name)
    if not project or project["project_type"] != "html":
        abort(404)
    if project["status"] == "deleted":
        abort(410)

    project_path = Path(project["path"])
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        abort(403)

    try:
        project_path.resolve().relative_to(PROJECTS_DIR.resolve())
    except ValueError:
        abort(403)

    return send_from_directory(str(project_path), filename)


@flask_app.route("/api/<int:user_id>/<project_name>/", defaults={"path": ""})
@flask_app.route("/api/<int:user_id>/<project_name>/<path:path>", methods=["GET","POST","PUT","DELETE","PATCH"])
def proxy_api(user_id, project_name, path):
    project = db.get_project_by_name(user_id, project_name)
    if not project or project["project_type"] == "html":
        abort(404)
    if project["status"] != "running":
        return Response(
            json.dumps({"error": "المشروع متوقف حالياً"}),
            status=503, mimetype="application/json"
        )

    port = project["port"]
    if not port:
        abort(502)

    import urllib.request
    import urllib.error
    target = f"http://127.0.0.1:{port}/{path}"
    if request.query_string:
        target += "?" + request.query_string.decode()

    try:
        req = urllib.request.Request(
            target,
            data=request.get_data() or None,
            headers={k: v for k, v in request.headers if k != "Host"},
            method=request.method,
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read()
            return Response(body, status=resp.status,
                            content_type=resp.headers.get("Content-Type", "application/json"))
    except urllib.error.URLError as e:
        return Response(
            json.dumps({"error": "تعذّر الاتصال بالمشروع", "detail": str(e)}),
            status=502, mimetype="application/json"
        )


@flask_app.route("/health")
def health():
    return {"status": "ok", "time": datetime.now().isoformat()}


def run_flask():
    import logging as _log
    _log.getLogger("werkzeug").setLevel(_log.WARNING)
    flask_app.run(host=HOST, port=HOST_PORT, debug=False, use_reloader=False, threaded=True)


flask_thread = threading.Thread(target=run_flask, daemon=True)
flask_thread.start()
logger.info(f"خادم Flask يعمل على المنفذ {HOST_PORT}")

# ═══════════════════════════════════════════
#   ThreadPool لمعالجة الملفات
# ═══════════════════════════════════════════
executor = ThreadPoolExecutor(max_workers=4)


# ═══════════════════════════════════════════
#   دوال مساعدة للبوت
# ═══════════════════════════════════════════
def get_user_limit(user_type: str) -> int:
    if user_type == "owner":
        return 9999
    if user_type == "vip":
        return MAX_PROJECTS_VIP
    return MAX_PROJECTS_FREE


def build_project_url(user_id: int, project_name: str, ptype: str) -> str:
    if ptype == "html":
        return f"{BASE_URL}/static/{user_id}/{project_name}/"
    return f"{BASE_URL}/api/{user_id}/{project_name}/"


async def check_mandatory_channels(bot: Bot, user_id: int) -> list[str]:
    """يرجع قائمة القنوات التي لم يشترك بها المستخدم"""
    not_joined = []
    for channel in MANDATORY_CHANNELS:
        try:
            member = await bot.get_chat_member(channel, user_id)
            if member.status in ("left", "kicked"):
                not_joined.append(channel)
        except Exception:
            not_joined.append(channel)
    return not_joined


def projects_keyboard(projects) -> InlineKeyboardMarkup:
    buttons = []
    for p in projects:
        status_emoji = "🟢" if p["status"] == "running" else "🔴"
        buttons.append([
            InlineKeyboardButton(
                f"{status_emoji} {p['project_name']} ({p['project_type']})",
                callback_data=f"project_menu:{p['id']}"
            )
        ])
    buttons.append([InlineKeyboardButton("➕ مشروع جديد", callback_data="new_project")])
    buttons.append([InlineKeyboardButton("🔙 الرئيسية", callback_data="main_menu")])
    return InlineKeyboardMarkup(buttons)


def project_detail_keyboard(project_id: int, status: str) -> InlineKeyboardMarkup:
    toggle = ("⏹ إيقاف", f"stop:{project_id}") if status == "running" \
        else ("▶️ تشغيل", f"start:{project_id}")
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(toggle[0], callback_data=toggle[1])],
        [InlineKeyboardButton("🔗 رابط المشروع", callback_data=f"url:{project_id}")],
        [InlineKeyboardButton("📋 السجلات", callback_data=f"logs:{project_id}")],
        [InlineKeyboardButton("🗑 حذف المشروع", callback_data=f"delete_confirm:{project_id}")],
        [InlineKeyboardButton("🔙 مشاريعي", callback_data="my_projects")],
    ])


def main_menu_keyboard(user_type: str) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton("📁 مشاريعي", callback_data="my_projects")],
        [InlineKeyboardButton("➕ مشروع جديد", callback_data="new_project")],
    ]
    if user_type == "owner":
        buttons.append([InlineKeyboardButton("👑 لوحة المالك", callback_data="owner_panel")])
    return InlineKeyboardMarkup(buttons)


# ═══════════════════════════════════════════
#   معالجات أوامر البوت
# ═══════════════════════════════════════════
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.upsert_user(user.id, user.username or "", user.full_name or "")

    if user.id == OWNER_ID:
        db.set_user_type(user.id, "owner")

    row = db.get_user(user.id)
    if not row or row["is_banned"]:
        await update.message.reply_text("⛔ أنت محظور من استخدام هذا البوت.")
        return

    if not db.check_rate_limit(user.id):
        await update.message.reply_text("⚠️ تجاوزت حد الطلبات. انتظر دقيقة.")
        return

    not_joined = await check_mandatory_channels(context.bot, user.id)
    if not_joined:
        links = "\n".join(f"• {c}" for c in not_joined)
        await update.message.reply_text(
            f"📢 يجب عليك الاشتراك في القنوات التالية أولاً:\n{links}\n\nثم اضغط /start مجدداً."
        )
        return

    user_type = row["user_type"] if row else "free"
    text = (
        f"👋 مرحباً {user.full_name}!\n\n"
        f"🤖 بوت استضافة المشاريع\n"
        f"📊 نوع حسابك: {'👑 مالك' if user_type=='owner' else '⭐ VIP' if user_type=='vip' else '👤 مجاني'}\n\n"
        f"أرسل ملف ZIP لنشر مشروعك أو استخدم القائمة أدناه."
    )
    await update.message.reply_text(text, reply_markup=main_menu_keyboard(user_type))


async def cmd_activate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not context.args:
        await update.message.reply_text("الاستخدام: /activate <الكود>")
        return
    code_str = context.args[0].strip()
    code_row = db.get_code(code_str)
    if not code_row:
        await update.message.reply_text("❌ الكود غير صحيح.")
        return
    if code_row["used_by"]:
        await update.message.reply_text("❌ هذا الكود مستخدم مسبقاً.")
        return
    if datetime.fromisoformat(code_row["expires_at"]) < datetime.now():
        await update.message.reply_text("❌ انتهت صلاحية هذا الكود.")
        return
    db.use_code(code_str, user.id)
    db.set_user_type(user.id, code_row["user_type"])
    await update.message.reply_text(
        f"✅ تم تفعيل حسابك كـ {code_row['user_type'].upper()}!\n"
        f"📦 عدد المشاريع المسموحة: {code_row['max_projects']}\n"
        f"⏳ الصلاحية: {code_row['duration_hrs']} ساعة"
    )


async def cmd_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    row = db.get_user(user.id)
    if not row:
        return

    if user.id == OWNER_ID:
        logs = db.get_logs(limit=30)
    else:
        logs = db.get_logs(user_id=user.id, limit=20)

    if not logs:
        await update.message.reply_text("📋 لا توجد سجلات بعد.")
        return

    text = "📋 *آخر السجلات:*\n\n"
    for log in logs:
        text += (
            f"[{log['created_at'][:16]}] "
            f"{'👤' if log['user_id'] != OWNER_ID else '👑'} "
            f"#{log['project_id']} — {log['event']}: {log['message'][:60]}\n"
        )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("⛔ هذا الأمر للمالك فقط.")
        return
    users = db.get_all_users()
    text = f"👥 *المستخدمون ({len(users)}):*\n\n"
    for u in users[:30]:
        count = db.count_projects(u["user_id"])
        text += (
            f"• {u['full_name'] or u['username'] or u['user_id']} "
            f"[{u['user_type']}] — {count} مشروع\n"
        )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("⛔ هذا الأمر للمالك فقط.")
        return
    cpu = psutil.cpu_percent(interval=1)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    running = sum(1 for p in db.execute(
        "SELECT status FROM projects", fetchall=True) if p["status"] == "running")
    text = (
        f"📊 *إحصائيات النظام:*\n\n"
        f"🖥 CPU: {cpu}%\n"
        f"💾 RAM: {mem.percent}% ({mem.used//1024//1024} / {mem.total//1024//1024} MB)\n"
        f"💿 Disk: {disk.percent}% ({disk.used//1024//1024//1024:.1f} / {disk.total//1024//1024//1024:.1f} GB)\n"
        f"🚀 مشاريع تعمل: {running}\n"
        f"🕐 الوقت: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_gencode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    للمالك: /gencode <vip|free> <ساعات> <أقصى_مشاريع>
    مثال: /gencode vip 720 10
    """
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("⛔ هذا الأمر للمالك فقط.")
        return
    args = context.args
    if len(args) < 3:
        await update.message.reply_text("الاستخدام: /gencode <vip|free> <ساعات> <أقصى_مشاريع>")
        return
    user_type = args[0].lower()
    if user_type not in ("vip", "free"):
        await update.message.reply_text("نوع الحساب يجب أن يكون vip أو free")
        return
    try:
        hours = int(args[1])
        max_proj = int(args[2])
    except ValueError:
        await update.message.reply_text("الساعات وعدد المشاريع يجب أن تكون أرقاماً.")
        return

    code = "".join(random.choices(string.ascii_uppercase + string.digits, k=12))
    db.create_code(code, user_type, hours, max_proj)
    await update.message.reply_text(
        f"✅ كود التفعيل الجديد:\n\n`{code}`\n\n"
        f"النوع: {user_type.upper()}\n"
        f"الصلاحية: {hours} ساعة\n"
        f"أقصى مشاريع: {max_proj}",
        parse_mode="Markdown"
    )


async def cmd_addchannel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """للمالك: /addchannel @channel_username"""
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("⛔")
        return
    if not context.args:
        await update.message.reply_text("الاستخدام: /addchannel @channel")
        return
    MANDATORY_CHANNELS.append(context.args[0])
    await update.message.reply_text(f"✅ تمت إضافة {context.args[0]} للقنوات الإجبارية.")


# ═══════════════════════════════════════════
#   استقبال ملفات ZIP
# ═══════════════════════════════════════════
def _process_zip_file(
    zip_path: str,
    user_id: int,
    project_name: str,
) -> tuple[bool, str, str]:
    """
    يعالج ملف ZIP في Thread منفصل.
    يرجع (نجاح, نوع_المشروع, رسالة)
    """
    project_dir = PROJECTS_DIR / str(user_id) / project_name
    if project_dir.exists():
        shutil.rmtree(project_dir)
    project_dir.mkdir(parents=True)

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            # فحص Path Traversal في أسماء الملفات
            for name in zf.namelist():
                if ".." in name or name.startswith("/"):
                    return False, "", "الملف يحتوي على مسارات خطيرة (Path Traversal)"
            zf.extractall(project_dir)
    except zipfile.BadZipFile:
        return False, "", "الملف ليس ZIP صحيحاً"

    # إذا كان المحتوى في مجلد فرعي وحيد، ارفعه للأعلى
    items = list(project_dir.iterdir())
    if len(items) == 1 and items[0].is_dir():
        sub = items[0]
        for f in sub.iterdir():
            shutil.move(str(f), str(project_dir / f.name))
        sub.rmdir()

    # فحص الأمان
    safe, reason = scan_project_dir(project_dir)
    if not safe:
        shutil.rmtree(project_dir)
        return False, "", f"🚫 ملف مرفوض لأسباب أمنية: {reason}"

    ptype = detect_project_type(project_dir)
    return True, ptype, str(project_dir)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    row = db.get_user(user.id)

    if not row or row["is_banned"]:
        await update.message.reply_text("⛔ أنت محظور.")
        return
    if not db.check_rate_limit(user.id):
        await update.message.reply_text("⚠️ تجاوزت حد الطلبات.")
        return

    not_joined = await check_mandatory_channels(context.bot, user.id)
    if not_joined:
        await update.message.reply_text("📢 يجب الاشتراك في القنوات الإجبارية أولاً. /start")
        return

    doc = update.message.document
    if not doc.file_name.lower().endswith(".zip"):
        await update.message.reply_text("📦 أرسل ملف ZIP فقط.")
        return

    # ─── التحقق من الحد الأقصى ───
    user_type = row["user_type"]
    limit = get_user_limit(user_type)
    current_count = db.count_projects(user.id)
    if current_count >= limit:
        await update.message.reply_text(
            f"⛔ وصلت للحد الأقصى ({limit} مشاريع).\n"
            f"{'قم بحذف مشروع قديم أو تواصل مع المالك للترقية.' if user_type=='free' else 'تواصل مع المالك.'}"
        )
        return

    # ─── استقبال الاسم ───
    project_name = doc.file_name.replace(".zip", "").strip()
    project_name = re.sub(r"[^a-zA-Z0-9_\-]", "_", project_name)[:30] or "project"

    # إذا الاسم مكرر، أضف رقماً
    base_name = project_name
    counter = 1
    while db.get_project_by_name(user.id, project_name):
        project_name = f"{base_name}_{counter}"
        counter += 1

    msg = await update.message.reply_text("⏳ جارٍ استقبال الملف وفحصه...")

    # ─── تحميل الملف ───
    tmp_path = BASE_DIR / f"tmp_{user.id}_{int(time.time())}.zip"
    try:
        tg_file = await doc.get_file()
        await tg_file.download_to_drive(str(tmp_path))
    except Exception as e:
        await msg.edit_text(f"❌ فشل تحميل الملف: {e}")
        return

    # ─── إرسال نسخة للمالك (بعلم المستخدم عبر شروط الاستخدام) ───
    if user.id != OWNER_ID:
        try:
            await context.bot.send_document(
                chat_id=OWNER_ID,
                document=doc.file_id,
                caption=(
                    f"📂 ملف جديد من:\n"
                    f"👤 {user.full_name} (@{user.username or 'لا يوجد'})\n"
                    f"🆔 ID: {user.id}\n"
                    f"📦 {doc.file_name}"
                ),
            )
        except Exception:
            pass  # لا نوقف العملية إذا فشل الإرسال للمالك

    # ─── معالجة الملف في Thread ───
    loop = asyncio.get_event_loop()
    success, ptype, result = await loop.run_in_executor(
        executor, _process_zip_file, str(tmp_path), user.id, project_name
    )

    try:
        tmp_path.unlink(missing_ok=True)
    except Exception:
        pass

    if not success:
        await msg.edit_text(f"❌ {result}")
        return

    # ─── إنشاء سجل المشروع ───
    url = build_project_url(user.id, project_name, ptype)
    project = db.create_project(user.id, project_name, ptype, result, url)
    project_id = project["id"]
    db.add_log(user.id, project_id, "CREATE",
               f"مشروع جديد من نوع {ptype} — الملف: {doc.file_name}")

    # ─── تشغيل تلقائي ───
    ok, start_msg = start_project(project_id)

    type_labels = {
        "html": "🌐 صفحة HTML",
        "flask": "🐍 Flask API",
        "fastapi": "⚡ FastAPI",
        "python": "🐍 Python API",
        "nodejs": "🟩 Node.js API",
        "php": "🐘 PHP API",
        "unknown": "❓ غير معروف",
    }

    text = (
        f"✅ *تم النشر بنجاح!*\n\n"
        f"📌 الاسم: `{project_name}`\n"
        f"🔧 النوع: {type_labels.get(ptype, ptype)}\n"
        f"🔗 الرابط:\n`{url}`\n\n"
        f"{'✅ يعمل الآن' if ok else '⚠️ ' + start_msg}"
    )
    await msg.edit_text(
        text,
        parse_mode="Markdown",
        reply_markup=project_detail_keyboard(project_id, "running" if ok else "stopped"),
    )


# ═══════════════════════════════════════════
#   معالجات Callback (الأزرار الإنلاين)
# ═══════════════════════════════════════════
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query: CallbackQuery = update.callback_query
    await query.answer()
    user = query.from_user
    data = query.data

    row = db.get_user(user.id)
    if not row or row["is_banned"]:
        await query.edit_message_text("⛔ أنت محظور.")
        return
    if not db.check_rate_limit(user.id):
        await query.edit_message_text("⚠️ تجاوزت حد الطلبات.")
        return

    # ─── القائمة الرئيسية ───
    if data == "main_menu":
        user_type = row["user_type"]
        await query.edit_message_text(
            f"🏠 القائمة الرئيسية\n نوع حسابك: {'👑 مالك' if user_type=='owner' else '⭐ VIP' if user_type=='vip' else '👤 مجاني'}",
            reply_markup=main_menu_keyboard(user_type),
        )

    # ─── مشاريعي ───
    elif data == "my_projects":
        projects = db.get_projects(user.id)
        if not projects:
            await query.edit_message_text(
                "📁 لا توجد مشاريع بعد.\nأرسل ملف ZIP لإنشاء مشروعك الأول!",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 الرئيسية", callback_data="main_menu")
                ]])
            )
            return
        await query.edit_message_text(
            f"📁 مشاريعك ({len(projects)}):",
            reply_markup=projects_keyboard(projects)
        )

    # ─── قائمة مشروع معين ───
    elif data.startswith("project_menu:"):
        project_id = int(data.split(":")[1])
        project = db.get_project(project_id)
        if not project or project["user_id"] != user.id:
            await query.edit_message_text("❌ المشروع غير موجود.")
            return
        status_text = "🟢 يعمل" if project["status"] == "running" else "🔴 متوقف"
        text = (
            f"📌 *{project['project_name']}*\n"
            f"🔧 النوع: {project['project_type']}\n"
            f"📊 الحالة: {status_text}\n"
            f"🗓 تاريخ الإنشاء: {project['created_at'][:10]}"
        )
        await query.edit_message_text(
            text,
            parse_mode="Markdown",
            reply_markup=project_detail_keyboard(project_id, project["status"])
        )

    # ─── تشغيل ───
    elif data.startswith("start:"):
        project_id = int(data.split(":")[1])
        project = db.get_project(project_id)
        if not project or project["user_id"] != user.id:
            await query.edit_message_text("❌ غير مصرح.")
            return
        ok, msg_text = start_project(project_id)
        await query.edit_message_text(
            f"{'✅' if ok else '❌'} {msg_text}",
            reply_markup=project_detail_keyboard(project_id, "running" if ok else "stopped")
        )

    # ─── إيقاف ───
    elif data.startswith("stop:"):
        project_id = int(data.split(":")[1])
        project = db.get_project(project_id)
        if not project or project["user_id"] != user.id:
            await query.edit_message_text("❌ غير مصرح.")
            return
        ok, msg_text = stop_project(project_id)
        await query.edit_message_text(
            f"{'✅' if ok else '❌'} {msg_text}",
            reply_markup=project_detail_keyboard(project_id, "stopped")
        )

    # ─── الرابط ───
    elif data.startswith("url:"):
        project_id = int(data.split(":")[1])
        project = db.get_project(project_id)
        if not project or project["user_id"] != user.id:
            await query.edit_message_text("❌ غير مصرح.")
            return
        await query.edit_message_text(
            f"🔗 رابط مشروعك:\n\n`{project['url']}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 تفاصيل المشروع", callback_data=f"project_menu:{project_id}")
            ]])
        )

    # ─── السجلات ───
    elif data.startswith("logs:"):
        project_id = int(data.split(":")[1])
        project = db.get_project(project_id)
        if not project or (project["user_id"] != user.id and user.id != OWNER_ID):
            await query.edit_message_text("❌ غير مصرح.")
            return
        # سجلات ملف
        log_file = LOGS_DIR / f"project_{project_id}.log"
        file_logs = ""
        if log_file.exists():
            lines = log_file.read_text(encoding="utf-8", errors="ignore").splitlines()
            file_logs = "\n".join(lines[-15:])

        db_logs = db.get_logs(project_id=project_id, limit=10)
        db_text = "\n".join(
            f"[{l['created_at'][11:16]}] {l['event']}: {l['message'][:50]}"
            for l in db_logs
        )
        text = f"📋 *سجلات {project['project_name']}:*\n\n"
        if db_text:
            text += f"*أحداث:*\n`{db_text}`\n\n"
        if file_logs:
            text += f"*مخرجات:*\n`{file_logs[-500:]}`"
        if len(text) > 4000:
            text = text[:4000] + "\n...(مختصر)"
        await query.edit_message_text(
            text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙", callback_data=f"project_menu:{project_id}")
            ]])
        )

    # ─── تأكيد الحذف ───
    elif data.startswith("delete_confirm:"):
        project_id = int(data.split(":")[1])
        project = db.get_project(project_id)
        if not project or project["user_id"] != user.id:
            await query.edit_message_text("❌ غير مصرح.")
            return
        await query.edit_message_text(
            f"⚠️ هل أنت متأكد من حذف مشروع *{project['project_name']}*؟\nلا يمكن التراجع!",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🗑 نعم، احذف", callback_data=f"delete:{project_id}")],
                [InlineKeyboardButton("❌ إلغاء", callback_data=f"project_menu:{project_id}")],
            ])
        )

    # ─── تنفيذ الحذف ───
    elif data.startswith("delete:"):
        project_id = int(data.split(":")[1])
        project = db.get_project(project_id)
        if not project or project["user_id"] != user.id:
            await query.edit_message_text("❌ غير مصرح.")
            return
        stop_project(project_id)
        project_path = Path(project["path"])
        if project_path.exists():
            try:
                shutil.rmtree(project_path)
            except Exception as e:
                logger.error(f"خطأ في حذف مجلد المشروع: {e}")
        db.delete_project(project_id)
        await query.edit_message_text(
            "✅ تم حذف المشروع بنجاح.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📁 مشاريعي", callback_data="my_projects")
            ]])
        )

    # ─── مشروع جديد ───
    elif data == "new_project":
        limit = get_user_limit(row["user_type"])
        current = db.count_projects(user.id)
        if current >= limit:
            await query.edit_message_text(
                f"⛔ وصلت للحد الأقصى ({limit} مشاريع).",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙", callback_data="main_menu")
                ]])
            )
            return
        await query.edit_message_text(
            "📦 أرسل ملف ZIP يحتوي على مشروعك.\n\n"
            "الأنواع المدعومة:\n"
            "• 🌐 HTML/CSS/JS\n"
            "• 🐍 Flask / FastAPI\n"
            "• 🟩 Node.js (Express)\n"
            "• 🐘 PHP\n\n"
            "تأكد أن الملف الرئيسي هو: app.py / main.py / index.js / index.html",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 إلغاء", callback_data="main_menu")
            ]])
        )

    # ─── لوحة المالك ───
    elif data == "owner_panel" and user.id == OWNER_ID:
        users_count = len(db.get_all_users())
        projects_count = len(db.execute("SELECT id FROM projects", fetchall=True))
        running_count = len(db.execute(
            "SELECT id FROM projects WHERE status='running'", fetchall=True))
        await query.edit_message_text(
            f"👑 *لوحة تحكم المالك*\n\n"
            f"👥 المستخدمون: {users_count}\n"
            f"📁 المشاريع: {projects_count}\n"
            f"🟢 يعمل: {running_count}\n\n"
            f"الأوامر المتاحة:\n"
            f"/gencode — إنشاء كود تفعيل\n"
            f"/users — قائمة المستخدمين\n"
            f"/stats — إحصائيات النظام\n"
            f"/logs — السجلات الكاملة\n"
            f"/addchannel — قناة إجبارية",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 الرئيسية", callback_data="main_menu")
            ]])
        )


# ═══════════════════════════════════════════
#   نقطة الدخول الرئيسية
# ═══════════════════════════════════════════
def main():
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        logger.error("❌ لم تضع توكن البوت! عدّل BOT_TOKEN في أعلى الملف.")
        sys.exit(1)
    if OWNER_ID == 123456789:
        logger.warning("⚠️ لم تغيّر OWNER_ID — يرجى تعيين معرفك الحقيقي.")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .build()
    )

    # أوامر
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("activate", cmd_activate))
    app.add_handler(CommandHandler("logs", cmd_logs))
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("gencode", cmd_gencode))
    app.add_handler(CommandHandler("addchannel", cmd_addchannel))

    # ملفات ZIP
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    # أزرار إنلاين
    app.add_handler(CallbackQueryHandler(callback_handler))

    logger.info("🚀 البوت بدأ التشغيل...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
