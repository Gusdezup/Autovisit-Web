#!/usr/bin/env python3
"""
Autovisit Web UI — Flask backend
"""

import json
import logging
import os
import subprocess
import threading
import queue
import signal
import datetime
import secrets
import bcrypt
import requests
from datetime import datetime as dt
from pathlib import Path
from flask import Flask, render_template, request, jsonify, Response, stream_with_context, redirect, url_for, session
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)

# Logging : silence le logger HTTP de Werkzeug (requêtes GET/POST)
logging.basicConfig(level=logging.WARNING)
logging.getLogger("werkzeug").setLevel(logging.ERROR)
log = logging.getLogger(__name__)

DATA_DIR    = Path(os.environ.get("AUTOVISIT_DIR", "/data"))
SITES_JSON  = DATA_DIR / "sites.json"
STATUS_JSON = DATA_DIR / "status.json"
LOGS_DIR    = DATA_DIR / "logs"
SCRIPT      = Path(os.environ.get("AUTOVISIT_SCRIPT", "/app/autovisit.py"))
AUTH_FILE   = DATA_DIR / "auth.json"

# ─── Auth ──────────────────────────────────────────────────────────────────────

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login_page"

DEFAULT_USERNAME = "admin"
DEFAULT_PASSWORD = "autovisit"

class User(UserMixin):
    def __init__(self, username):
        self.id = username

@login_manager.user_loader
def load_user(user_id):
    return User(user_id)

def load_auth():
    if AUTH_FILE.exists():
        with open(AUTH_FILE, encoding="utf-8") as f:
            return json.load(f)
    # Premier lancement : créer auth.json avec credentials par défaut
    hashed = bcrypt.hashpw(DEFAULT_PASSWORD.encode(), bcrypt.gensalt()).decode()
    auth = {"username": DEFAULT_USERNAME, "password_hash": hashed, "is_default": True}
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(AUTH_FILE, "w", encoding="utf-8") as f:
        json.dump(auth, f, indent=2)
    log.warning(
        "=== PREMIER LANCEMENT — login: %s / mot de passe: %s ===",
        DEFAULT_USERNAME, DEFAULT_PASSWORD
    )
    return auth

def save_auth(username, password_hash):
    auth = {"username": username, "password_hash": password_hash, "is_default": False}
    with open(AUTH_FILE, "w", encoding="utf-8") as f:
        json.dump(auth, f, indent=2)

def check_password(plain, hashed):
    return bcrypt.checkpw(plain.encode(), hashed.encode())

# Initialiser auth au démarrage
load_auth()

# ─── Routes auth ───────────────────────────────────────────────────────────────

@app.route("/login", methods=["GET"])
def login_page():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    return render_template("login.html")

@app.route("/api/auth/login", methods=["POST"])
def api_login():
    data     = request.json or {}
    username = data.get("username", "").strip()
    password = data.get("password", "")
    auth     = load_auth()
    if username == auth["username"] and check_password(password, auth["password_hash"]):
        login_user(User(username), remember=True)
        return jsonify({"ok": True, "is_default": auth.get("is_default", False)})
    return jsonify({"ok": False, "error": "Identifiants incorrects"}), 401

@app.route("/api/auth/logout", methods=["POST"])
@login_required
def api_logout():
    logout_user()
    return jsonify({"ok": True})

@app.route("/api/auth/change-password", methods=["POST"])
@login_required
def api_change_password():
    data         = request.json or {}
    current_pw   = data.get("current_password", "")
    new_pw       = data.get("new_password", "")
    new_username = data.get("username", "").strip()
    auth         = load_auth()
    if not check_password(current_pw, auth["password_hash"]):
        return jsonify({"ok": False, "error": "Mot de passe actuel incorrect"}), 401
    if len(new_pw) < 6:
        return jsonify({"ok": False, "error": "Le mot de passe doit faire au moins 6 caractères"}), 400
    if not new_username:
        return jsonify({"ok": False, "error": "Le nom d'utilisateur ne peut pas être vide"}), 400
    hashed = bcrypt.hashpw(new_pw.encode(), bcrypt.gensalt()).decode()
    save_auth(new_username, hashed)
    return jsonify({"ok": True})

@app.route("/api/auth/status", methods=["GET"])
@login_required
def api_auth_status():
    auth = load_auth()
    return jsonify({"username": auth["username"], "is_default": auth.get("is_default", False)})

# ─── Scheduler ─────────────────────────────────────────────────────────────────

run_log_queue    = queue.Queue()
run_lock         = threading.Lock()
current_run_proc = None

scheduler = BackgroundScheduler()
scheduler.start()

SCHED_FILE = DATA_DIR / "schedule.json"

def load_schedule():
    if SCHED_FILE.exists():
        with open(SCHED_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"enabled": False, "mode": "days", "interval_d": 1, "hour_d": 3, "minute_d": 0,
            "interval_h": 6, "hour_w": 3, "minute_w": 0, "weekdays": [],
            "args": "--mp --error --json-output"}

def save_schedule(data):
    with open(SCHED_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def run_autovisit_job(extra_args="--mp --error --json-output"):
    args = ["python3", str(SCRIPT)] + extra_args.split()
    subprocess.run(args, cwd=str(DATA_DIR))

def apply_schedule(sched):
    scheduler.remove_all_jobs()
    if not sched.get("enabled"):
        return
    mode = sched.get("mode", "days")
    args = sched.get("args", "--mp --error --json-output")

    if mode == "hours":
        scheduler.add_job(
            run_autovisit_job,
            IntervalTrigger(hours=sched.get("interval_h", 6)),
            args=[args], id="autovisit_cron"
        )
    elif mode == "days":
        now   = dt.now()
        start = now.replace(hour=sched.get("hour_d", 3), minute=sched.get("minute_d", 0),
                            second=0, microsecond=0)
        if start <= now:
            start += datetime.timedelta(days=1)
        scheduler.add_job(
            run_autovisit_job,
            IntervalTrigger(days=sched.get("interval_d", 1), start_date=start),
            args=[args], id="autovisit_cron"
        )
    elif mode == "weekdays":
        days = sched.get("weekdays", [])
        if days:
            dow = ",".join(str(d) for d in days)
            scheduler.add_job(
                run_autovisit_job,
                CronTrigger(day_of_week=dow, hour=sched.get("hour_w", 3), minute=sched.get("minute_w", 0)),
                args=[args], id="autovisit_cron"
            )

apply_schedule(load_schedule())

# ─── Config ────────────────────────────────────────────────────────────────────

def load_config():
    if not SITES_JSON.exists():
        return {"pushover": {}, "sites": []}
    with open(SITES_JSON, encoding="utf-8") as f:
        return json.load(f)

def save_config(cfg):
    with open(SITES_JSON, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

# ─── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
@login_required
def index():
    return render_template("index.html")

@app.route("/<path:path>")
@login_required
def catch_all(path):
    if path.startswith("api/"):
        return jsonify({"error": "Not found"}), 404
    return render_template("index.html")

@app.route("/api/status")
@login_required
def api_status():
    if STATUS_JSON.exists():
        with open(STATUS_JSON, encoding="utf-8") as f:
            return jsonify(json.load(f))
    return jsonify({"updated": None, "sites": []})

@app.route("/api/sites", methods=["GET"])
@login_required
def api_sites_get():
    return jsonify(load_config())

@app.route("/api/sites", methods=["POST"])
@login_required
def api_sites_post():
    cfg  = load_config()
    site = request.json
    for bool_field in ["enabled", "api_json", "use_curl_cffi", "use_playwright",
                        "playwright_fetch_verify", "playwright_wait_url_change", "extract_hidden_fields"]:
        if bool_field in site:
            site[bool_field] = bool(site[bool_field])
    for list_field in ["success_keywords", "alert_keywords", "aliases", "pre_visit_urls"]:
        if list_field in site and isinstance(site[list_field], str):
            site[list_field] = [x.strip() for x in site[list_field].split(",") if x.strip()]
    for dict_field in ["extra_fields", "extra_headers", "stats", "stats_json"]:
        if dict_field in site and isinstance(site[dict_field], str):
            try:
                site[dict_field] = json.loads(site[dict_field]) if site[dict_field].strip() else {}
            except Exception:
                site[dict_field] = {}
    site = {k: v for k, v in site.items() if v != "" and v != [] and v != {}}
    cfg["sites"].append(site)
    save_config(cfg)
    return jsonify({"ok": True})

@app.route("/api/sites/<int:idx>", methods=["PUT"])
@login_required
def api_sites_put(idx):
    cfg = load_config()
    if idx < 0 or idx >= len(cfg["sites"]):
        return jsonify({"ok": False, "error": "Index invalide"}), 404
    site = request.json
    for bool_field in ["enabled", "api_json", "use_curl_cffi", "use_playwright",
                        "playwright_fetch_verify", "extract_hidden_fields"]:
        if bool_field in site:
            site[bool_field] = bool(site[bool_field])
    for list_field in ["success_keywords", "alert_keywords", "aliases", "pre_visit_urls"]:
        if list_field in site and isinstance(site[list_field], str):
            site[list_field] = [x.strip() for x in site[list_field].split(",") if x.strip()]
    for dict_field in ["extra_fields", "extra_headers", "stats", "stats_json"]:
        if dict_field in site and isinstance(site[dict_field], str):
            try:
                site[dict_field] = json.loads(site[dict_field]) if site[dict_field].strip() else {}
            except Exception:
                site[dict_field] = {}
    site = {k: v for k, v in site.items() if v != "" and v != [] and v != {}}
    preserved_fields = ['stats', 'stats_json', 'playwright_submit', 'playwright_fetch_verify',
                        'stats_base', 'mp_url', 'mp_json_field', 'playwright_stats_url']
    existing = cfg["sites"][idx]
    for field in preserved_fields:
        if field in existing and field not in site:
            site[field] = existing[field]
    cfg["sites"][idx] = site
    save_config(cfg)
    return jsonify({"ok": True})

@app.route("/api/sites/<int:idx>", methods=["DELETE"])
@login_required
def api_sites_delete(idx):
    cfg = load_config()
    if idx < 0 or idx >= len(cfg["sites"]):
        return jsonify({"ok": False, "error": "Index invalide"}), 404
    cfg["sites"].pop(idx)
    save_config(cfg)
    return jsonify({"ok": True})

@app.route("/api/sites/<int:idx>/toggle", methods=["POST"])
@login_required
def api_sites_toggle(idx):
    cfg = load_config()
    if idx < 0 or idx >= len(cfg["sites"]):
        return jsonify({"ok": False, "error": "Index invalide"}), 404
    site = cfg["sites"][idx]
    site["enabled"] = not site.get("enabled", True)
    save_config(cfg)
    return jsonify({"ok": True, "enabled": site["enabled"]})

@app.route("/api/pushover", methods=["GET"])
@login_required
def api_pushover_get():
    return jsonify(load_config().get("pushover", {}))

@app.route("/api/pushover", methods=["POST"])
@login_required
def api_pushover_post():
    cfg = load_config()
    cfg["pushover"] = request.json
    save_config(cfg)
    return jsonify({"ok": True})

@app.route("/api/notifications", methods=["GET"])
@login_required
def api_notifications_get():
    cfg   = load_config()
    notif = cfg.get("notifications", {})
    return jsonify({
        "apprise_url":                  notif.get("apprise_url", ""),
        "urls":                         notif.get("urls", []),
        "notify_error":                 notif.get("notify_error", True),
        "notify_success":               notif.get("notify_success", False),
        "notify_success_after_failure": notif.get("notify_success_after_failure", True),
        "notify_stats":                 notif.get("notify_stats", False),
        "notify_mp":                    notif.get("notify_mp", True),
    })

@app.route("/api/notifications", methods=["POST"])
@login_required
def api_notifications_post():
    cfg  = load_config()
    data = request.json
    cfg["notifications"] = {
        "apprise_url":                  data.get("apprise_url", "").rstrip("/"),
        "urls":                         data.get("urls", []),
        "notify_error":                 bool(data.get("notify_error", True)),
        "notify_success":               bool(data.get("notify_success", False)),
        "notify_success_after_failure": bool(data.get("notify_success_after_failure", True)),
        "notify_stats":                 bool(data.get("notify_stats", False)),
        "notify_mp":                    bool(data.get("notify_mp", True)),
    }
    save_config(cfg)
    return jsonify({"ok": True})

@app.route("/api/notifications/test", methods=["POST"])
@login_required
def api_notifications_test():
    cfg    = load_config()
    notif  = cfg.get("notifications", {})
    server = notif.get("apprise_url", "").rstrip("/")
    urls   = notif.get("urls", [])
    if not server:
        return jsonify({"ok": False, "error": "Serveur Apprise non configuré"})
    if not urls:
        return jsonify({"ok": False, "error": "Aucune URL de notification configurée"})
    payload = {
        "title": "Autovisit — Test",
        "body":  "Notification de test depuis Autovisit Web",
        "urls":  "\n".join(urls),
    }
    try:
        r = requests.post(server + "/notify/", json=payload, timeout=10)
        if r.status_code == 200:
            return jsonify({"ok": True})
        return jsonify({"ok": False, "error": "HTTP " + str(r.status_code)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/api/schedule", methods=["GET"])
@login_required
def api_schedule_get():
    return jsonify(load_schedule())

@app.route("/api/schedule", methods=["POST"])
@login_required
def api_schedule_post():
    data = request.json
    save_schedule(data)
    apply_schedule(data)
    return jsonify({"ok": True})

@app.route("/api/run", methods=["POST"])
@login_required
def api_run():
    global current_run_proc
    data     = request.json or {}
    site_arg = data.get("site")
    flags    = data.get("flags", "--mp --error --json-output")
    if "--json-output" not in flags:
        flags += " --json-output"
    if not run_lock.acquire(blocking=False):
        return jsonify({"ok": False, "error": "Un run est déjà en cours"}), 409

    def do_run():
        global current_run_proc
        try:
            args = ["python3", str(SCRIPT)] + flags.split()
            if site_arg:
                sites = site_arg.split() if isinstance(site_arg, str) else site_arg
                args += ["--site"] + sites
            current_run_proc = subprocess.Popen(
                args, cwd=str(DATA_DIR),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )
            for line in current_run_proc.stdout:
                run_log_queue.put(line.rstrip())
            current_run_proc.wait()
            run_log_queue.put("__DONE__")
        finally:
            current_run_proc = None
            run_lock.release()

    threading.Thread(target=do_run, daemon=True).start()
    return jsonify({"ok": True})

@app.route("/api/run/stop", methods=["POST"])
@login_required
def api_run_stop():
    global current_run_proc
    if current_run_proc and current_run_proc.poll() is None:
        current_run_proc.send_signal(signal.SIGTERM)
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "Aucun run en cours"})

@app.route("/api/run/stream")
@login_required
def api_run_stream():
    def generate():
        while True:
            try:
                line = run_log_queue.get(timeout=60)
                if line == "__DONE__":
                    yield "data: __DONE__\n\n"
                    break
                yield f"data: {line}\n\n"
            except queue.Empty:
                yield "data: __PING__\n\n"
    return Response(stream_with_context(generate()),
                    mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.route("/api/logs")
@login_required
def api_logs_list():
    if not LOGS_DIR.exists():
        return jsonify([])
    files = sorted(LOGS_DIR.glob("visit_*.log"), reverse=True)
    return jsonify([f.name for f in files])

@app.route("/api/logs/<filename>")
@login_required
def api_logs_file(filename):
    path = LOGS_DIR / filename
    if not path.exists() or not filename.startswith("visit_"):
        return jsonify({"error": "Introuvable"}), 404
    lines  = path.read_text(encoding="utf-8").splitlines()
    offset = int(request.args.get("offset", 0))
    limit  = int(request.args.get("limit", 500))
    total  = len(lines)
    chunk  = lines[max(0, total - limit - offset): total - offset] if offset == 0 else lines[offset:offset+limit]
    return jsonify({"lines": chunk, "total": total})

@app.route("/api/running")
@login_required
def api_running():
    return jsonify({"running": current_run_proc is not None and current_run_proc.poll() is None})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=4567, debug=False)
