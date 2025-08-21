import os
from typing import Optional, List

from fastapi import FastAPI, Request, Form, UploadFile, File, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from passlib.hash import pbkdf2_sha256
import asyncio
import json
import pty
import termios
import fcntl
import struct
import signal
import shutil
import stat as statmod
from datetime import datetime
from pathlib import Path

from .utils.auth import is_authenticated
from .core.system_ops import (
    start_vpn,
    stop_vpn,
    check_gensyn_api,
    get_gensyn_log_status,
    start_gensyn_session,
    kill_gensyn,
    get_public_ip,
    check_gensyn_screen_running,
    start_periodic_sync_backup,
    backup_user_data_timestamped,
    fetch_peer_info,
    is_tmate_running,
    start_tmate,
    stop_tmate,
    get_tmate_ssh,
)


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES_DIR = os.path.join(BASE_DIR, "app", "templates")

app = FastAPI()

# Session secret from env
SESSION_SECRET = os.getenv("SESSION_SECRET", "change_me_please")
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET)

templates = Jinja2Templates(directory=TEMPLATES_DIR)


def flash(request: Request, message: Optional[str] = None) -> Optional[str]:
    if message is not None:
        request.session["flash"] = message
        return None
    msg = request.session.pop("flash", None)
    return msg


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": flash(request)},
    )


@app.post("/login")
def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    env_username = os.getenv("ADMIN_USERNAME", "")
    env_password_hash = os.getenv("ADMIN_PASSWORD_HASH", "")

    if not env_username or not env_password_hash:
        flash(request, "Server not initialized. Run setup.sh.")
        return RedirectResponse("/login", status_code=303)

    if username == env_username and pbkdf2_sha256.verify(password, env_password_hash):
        request.session["user"] = username
        return RedirectResponse("/", status_code=303)
    flash(request, "Invalid credentials")
    return RedirectResponse("/login", status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    if not is_authenticated(request):
        return RedirectResponse("/login", status_code=303)

    # Status data
    api_alive = check_gensyn_api()
    log_data = get_gensyn_log_status()
    public_ip = get_public_ip()
    gensyn_running = check_gensyn_screen_running()

    # Prepare simple status strings
    api_status = "Online" if api_alive else "Offline"
    last_activity = None
    joining = None
    starting = None
    if log_data:
        last_activity = log_data.get("timestamp")
        joining = log_data.get("joining")
        starting = log_data.get("starting")

    # Peer info
    peer = fetch_peer_info() or {}
    reward = peer.get("reward")
    score = peer.get("score")
    peer_name = peer.get("peer_name")
    online = peer.get("online")

    # Terminal status
    terminal_running = is_tmate_running()
    terminal_ssh = get_tmate_ssh() if terminal_running else None

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "flash": flash(request),
            "api_status": api_status,
            "last_activity": last_activity,
            "joining": joining,
            "starting": starting,
            "public_ip": public_ip,
            "gensyn_running": gensyn_running,
            "peer_name": peer_name,
            "reward": reward,
            "score": score,
            "peer_online": online,
            "terminal_running": terminal_running,
            "terminal_ssh": terminal_ssh,
        },
    )


@app.post("/vpn/on")
def vpn_on(request: Request):
    if not is_authenticated(request):
        return RedirectResponse("/login", status_code=303)
    success, message = start_vpn()
    flash(request, message)
    return RedirectResponse("/", status_code=303)


@app.post("/vpn/off")
def vpn_off(request: Request):
    if not is_authenticated(request):
        return RedirectResponse("/login", status_code=303)
    success, message = stop_vpn()
    flash(request, message)
    return RedirectResponse("/", status_code=303)


@app.post("/gensyn/start")
def gensyn_start(request: Request):
    if not is_authenticated(request):
        return RedirectResponse("/login", status_code=303)
    try:
        start_gensyn_session(use_sync_backup=True, fresh_start=False)
        flash(request, "Gensyn started in screen session 'gensyn'")
    except Exception as e:
        flash(request, f"Error starting Gensyn: {str(e)}")
    return RedirectResponse("/", status_code=303)


@app.post("/gensyn/kill")
def gensyn_kill(request: Request):
    if not is_authenticated(request):
        return RedirectResponse("/login", status_code=303)
    try:
        kill_gensyn()
        flash(request, "Gensyn screen killed")
    except Exception as e:
        flash(request, f"Failed to kill Gensyn: {str(e)}")
    return RedirectResponse("/", status_code=303)


@app.post("/backup/run")
def run_backup(request: Request):
    if not is_authenticated(request):
        return RedirectResponse("/login", status_code=303)
    ok = backup_user_data_timestamped()
    flash(request, "Backup created" if ok else "Backup failed")
    return RedirectResponse("/", status_code=303)


@app.post("/tmate/toggle")
def toggle_tmate(request: Request):
    if not is_authenticated(request):
        return RedirectResponse("/login", status_code=303)
    if is_tmate_running():
        ok = stop_tmate()
        flash(request, "Terminal stopped" if ok else "Failed to stop terminal")
    else:
        ssh = start_tmate()
        if ssh:
            flash(request, f"Terminal started: {ssh}")
        else:
            flash(request, "Failed to start terminal")
    return RedirectResponse("/", status_code=303)


# ---------- File Manager (key files) ----------

from .core.system_ops import SWARM_PEM_PATH, USER_DATA_PATH, USER_APIKEY_PATH, BACKUP_USERDATA_DIR


@app.get("/files", response_class=HTMLResponse)
def files_page(request: Request):
    if not is_authenticated(request):
        return RedirectResponse("/login", status_code=303)
    # Collect backups list
    backups = []
    if os.path.exists(BACKUP_USERDATA_DIR):
        for name in sorted(os.listdir(BACKUP_USERDATA_DIR)):
            backups.append(name)
    return templates.TemplateResponse(
        "files.html",
        {
            "request": request,
            "flash": flash(request),
            "has_swarm": os.path.exists(SWARM_PEM_PATH),
            "has_userdata": os.path.exists(USER_DATA_PATH),
            "has_apikey": os.path.exists(USER_APIKEY_PATH),
            "backups": backups,
        },
    )


@app.get("/files/download")
def files_download(path: str):
    # very limited allowlist
    allow = {SWARM_PEM_PATH, USER_DATA_PATH, USER_APIKEY_PATH}
    if path in allow:
        filename = os.path.basename(path)
        return FileResponse(path=path, filename=filename)
    # backup files
    backup_file = os.path.join(BACKUP_USERDATA_DIR, os.path.basename(path))
    if os.path.commonpath([backup_file, BACKUP_USERDATA_DIR]) == BACKUP_USERDATA_DIR and os.path.exists(backup_file):
        return FileResponse(path=backup_file, filename=os.path.basename(backup_file))
    return RedirectResponse("/files", status_code=303)


@app.post("/files/upload")
async def files_upload(request: Request, kind: str = Form(...), file: UploadFile = File(...)):
    if not is_authenticated(request):
        return RedirectResponse("/login", status_code=303)
    mapping = {
        "swarm": SWARM_PEM_PATH,
        "userdata": USER_DATA_PATH,
        "apikey": USER_APIKEY_PATH,
    }
    if kind not in mapping:
        flash(request, "Invalid upload type")
        return RedirectResponse("/files", status_code=303)
    dest = mapping[kind]
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "wb") as f:
        f.write(await file.read())
    flash(request, f"Uploaded {kind}")
    return RedirectResponse("/files", status_code=303)


# ---------- Gensyn Login Assistant ----------

from .core import login_assistant as LA
import time


@app.get("/gensyn-login", response_class=HTMLResponse)
def gensyn_login_page(request: Request):
    if not is_authenticated(request):
        return RedirectResponse("/login", status_code=303)
    # Auto-start login flow when coming from main site link
    status = LA.get_status()
    if not status.get("running") and status.get("status") == "idle":
        LA.start_login()
        status = LA.get_status()
    return templates.TemplateResponse(
        "login_assistant.html",
        {"request": request, "flash": flash(request), "running": status.get("running"), "status": status, "ts": int(time.time())},
    )


@app.post("/gensyn-login/start")
def gensyn_login_start(request: Request):
    if not is_authenticated(request):
        return RedirectResponse("/login", status_code=303)
    if LA.start_login():
        flash(request, "Login started. Submit email and OTP when ready.")
    else:
        flash(request, "Login already running")
    return RedirectResponse("/gensyn-login", status_code=303)


@app.post("/gensyn-login/email")
def gensyn_login_email(request: Request, email: str = Form(...)):
    if not is_authenticated(request):
        return RedirectResponse("/login", status_code=303)
    LA.submit_email(email)
    flash(request, "Email submitted")
    return RedirectResponse("/gensyn-login", status_code=303)


@app.post("/gensyn-login/otp")
def gensyn_login_otp(request: Request, otp: str = Form(...)):
    if not is_authenticated(request):
        return RedirectResponse("/login", status_code=303)
    LA.submit_otp(otp)
    flash(request, "OTP submitted")
    return RedirectResponse("/gensyn-login", status_code=303)


@app.get("/gensyn-login/screenshot")
def gensyn_login_screenshot(which: str = "final"):
    path_map = {
        "final": "/root/final_login_success_web.png",
        "failed": "/root/login_failed_web.png",
        "error": "/root/login_error_web.png",
        "after_continue": "/root/login_after_continue_web.png",
        "magic": "/root/login_magic_link_web.png",
    }
    path = path_map.get(which, path_map["final"])
    if os.path.exists(path):
        return FileResponse(path=path, media_type="image/png", filename=os.path.basename(path))
    return RedirectResponse("/gensyn-login", status_code=303)


@app.get("/gensyn-login/current-screenshot")
def gensyn_login_current_screenshot():
    status = LA.get_status()
    path = status.get("screenshot")
    if path and os.path.exists(path):
        return FileResponse(path=path, media_type="image/png", filename=os.path.basename(path))
    return RedirectResponse("/gensyn-login", status_code=303)


@app.get("/gensyn-login/status.json")
def gensyn_login_status_json():
    return JSONResponse(LA.get_status())


# ---------- Web Terminal ----------

@app.get("/terminal", response_class=HTMLResponse)
def terminal_page(request: Request):
    if not is_authenticated(request):
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(
        "terminal.html",
        {"request": request, "flash": flash(request)},
    )


def _set_pty_size(fd: int, rows: int, cols: int) -> None:
    try:
        size = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, size)
    except Exception:
        pass


@app.websocket("/ws/terminal")
async def websocket_terminal(websocket: WebSocket):
    # Simple auth gate: require logged-in session
    session = websocket.scope.get("session") or {}
    if not session.get("user"):
        await websocket.close(code=1008)
        return

    await websocket.accept()

    # Spawn a login shell in a PTY
    pid, master_fd = pty.fork()
    if pid == 0:
        # Child: replace with shell
        try:
            os.environ.setdefault("TERM", "xterm-256color")
            # Prefer bash, fallback to sh
            shell = os.environ.get("SHELL", "/bin/bash")
            os.execvp(shell, [shell, "-l"])  # login shell
        except Exception:
            os._exit(1)

    loop = asyncio.get_running_loop()

    # Optional: set an initial size
    _set_pty_size(master_fd, 24, 80)

    # Reader: forward PTY -> WebSocket
    def on_pty_readable():
        try:
            data = os.read(master_fd, 1024)
            if not data:
                # EOF
                asyncio.ensure_future(websocket.close())
                return
            # Send as text to xterm
            asyncio.ensure_future(websocket.send_text(data.decode("utf-8", "ignore")))
        except Exception:
            pass

    loop.add_reader(master_fd, on_pty_readable)

    try:
        while True:
            # Receive user input or resize events
            message = await websocket.receive_text()
            try:
                obj = json.loads(message)
                if isinstance(obj, dict) and obj.get("type") == "resize":
                    cols = int(obj.get("cols") or 80)
                    rows = int(obj.get("rows") or 24)
                    _set_pty_size(master_fd, rows, cols)
                    continue
            except Exception:
                pass
            if message:
                try:
                    os.write(master_fd, message.encode("utf-8", "ignore"))
                except Exception:
                    break
    except WebSocketDisconnect:
        pass
    finally:
        try:
            loop.remove_reader(master_fd)
        except Exception:
            pass
        try:
            os.close(master_fd)
        except Exception:
            pass
        try:
            os.kill(pid, signal.SIGHUP)
        except Exception:
            pass


# ---------- File Manager (SFTP-like) ----------

# Restrict to these roots only
FM_ROOTS = [os.path.abspath(p) for p in ["/root", "/home/ubuntu"]]


def _containing_root(path: str) -> Optional[str]:
    for base in FM_ROOTS:
        try:
            if os.path.commonpath([path, base]) == base:
                return base
        except Exception:
            continue
    return None


def _sanitize_path(user_path: Optional[str], default_root: Optional[str] = None) -> Optional[str]:
    base_default = default_root or (FM_ROOTS[0] if FM_ROOTS else "/root")
    candidate = (user_path or base_default).strip()
    # Expand ~
    if candidate.startswith("~"):
        candidate = os.path.expanduser(candidate)
    # If not absolute, resolve relative to default base
    if not os.path.isabs(candidate):
        candidate = os.path.join(base_default, candidate)
    # Resolve symlinks and normalize
    norm = os.path.realpath(candidate)
    # Ensure within one of the allowed bases
    if _containing_root(norm):
        return norm
    return None


def _breadcrumb(path: str) -> List[dict]:
    crumbs = []
    cur = path
    while True:
        # stop if outside allowed roots
        if not _containing_root(cur):
            break
        name = os.path.basename(cur) or "/"
        crumbs.append({"name": name, "path": cur})
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return list(reversed(crumbs))


def _entry_info(p: Path) -> dict:
    try:
        st = p.lstat()
        is_dir = p.is_dir()
        size = st.st_size if not is_dir else None
        mtime = datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds")
        mode = statmod.filemode(st.st_mode)
        return {
            "name": p.name or "/",
            "path": str(p),
            "is_dir": is_dir,
            "size": size,
            "mtime": mtime,
            "mode": mode,
        }
    except Exception as e:
        return {
            "name": p.name or "/",
            "path": str(p),
            "is_dir": p.is_dir() if p.exists() else False,
            "error": str(e),
        }


@app.get("/file-manager", response_class=HTMLResponse)
def file_manager_page(request: Request, path: Optional[str] = None):
    if not is_authenticated(request):
        return RedirectResponse("/login", status_code=303)
    start_path = _sanitize_path(path) or FM_ROOTS[0]
    return templates.TemplateResponse(
        "file_manager.html",
        {"request": request, "flash": flash(request), "start_path": start_path, "fm_roots": FM_ROOTS},
    )


@app.get("/fm/list")
def fm_list(request: Request, path: Optional[str] = None):
    if not is_authenticated(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    sanitized = _sanitize_path(path)
    if not sanitized:
        return JSONResponse({"error": "path_out_of_root"}, status_code=400)
    cur = Path(sanitized)
    if not cur.exists():
        return JSONResponse({"error": "not_found"}, status_code=404)
    if not cur.is_dir():
        # If file, return its info
        return JSONResponse({"path": str(cur), "entries": [], "info": _entry_info(cur)})
    entries: List[dict] = []
    try:
        for child in cur.iterdir():
            entries.append(_entry_info(child))
    except PermissionError:
        return JSONResponse({"error": "permission_denied"}, status_code=403)
    # Sort: dirs first, then files, by name
    entries.sort(key=lambda e: (not e.get("is_dir", False), e.get("name", "").lower()))
    # compute parent but clamp to root boundary
    parent = None
    root = _containing_root(str(cur))
    if root and str(cur) != root:
        parent = str(cur.parent)
    return JSONResponse({
        "path": str(cur),
        "parent": parent,
        "breadcrumb": _breadcrumb(str(cur)),
        "entries": entries,
    })


@app.get("/fm/download")
def fm_download(request: Request, path: str):
    if not is_authenticated(request):
        return RedirectResponse("/login", status_code=303)
    target = _sanitize_path(path)
    if not target:
        return RedirectResponse("/file-manager", status_code=303)
    if not os.path.isfile(target):
        return RedirectResponse("/file-manager?path=" + os.path.dirname(target), status_code=303)
    return FileResponse(path=target, filename=os.path.basename(target))


@app.post("/fm/mkdir")
async def fm_mkdir(request: Request):
    if not is_authenticated(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    data = await request.json()
    dirpath = _sanitize_path(data.get("path"))
    name = data.get("name")
    if not name:
        return JSONResponse({"error": "missing_name"}, status_code=400)
    if not dirpath:
        return JSONResponse({"error": "path_out_of_root"}, status_code=400)
    target = _sanitize_path(os.path.join(dirpath, name), default_root=dirpath)
    try:
        os.makedirs(target, exist_ok=False)
        return JSONResponse({"ok": True})
    except FileExistsError:
        return JSONResponse({"error": "exists"}, status_code=409)
    except PermissionError:
        return JSONResponse({"error": "permission_denied"}, status_code=403)


@app.post("/fm/delete")
async def fm_delete(request: Request):
    if not is_authenticated(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    data = await request.json()
    path = _sanitize_path(data.get("path"))
    if not path:
        return JSONResponse({"error": "path_out_of_root"}, status_code=400)
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            os.remove(path)
        return JSONResponse({"ok": True})
    except FileNotFoundError:
        return JSONResponse({"error": "not_found"}, status_code=404)
    except PermissionError:
        return JSONResponse({"error": "permission_denied"}, status_code=403)


@app.post("/fm/rename")
async def fm_rename(request: Request):
    if not is_authenticated(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    data = await request.json()
    path = _sanitize_path(data.get("path"))
    new_name = data.get("new_name")
    if not new_name:
        return JSONResponse({"error": "missing_new_name"}, status_code=400)
    if not path:
        return JSONResponse({"error": "path_out_of_root"}, status_code=400)
    dest = _sanitize_path(os.path.join(os.path.dirname(path), new_name), default_root=os.path.dirname(path))
    try:
        os.rename(path, dest)
        return JSONResponse({"ok": True})
    except FileNotFoundError:
        return JSONResponse({"error": "not_found"}, status_code=404)
    except FileExistsError:
        return JSONResponse({"error": "exists"}, status_code=409)
    except PermissionError:
        return JSONResponse({"error": "permission_denied"}, status_code=403)


@app.post("/fm/move")
async def fm_move(request: Request):
    if not is_authenticated(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    data = await request.json()
    srcs = data.get("srcs") or []
    dest_dir = _sanitize_path(data.get("dest_dir"))
    if not dest_dir:
        return JSONResponse({"error": "path_out_of_root"}, status_code=400)
    if not os.path.isdir(dest_dir):
        return JSONResponse({"error": "dest_not_dir"}, status_code=400)
    moved = []
    for s in srcs:
        sp = _sanitize_path(s)
        if not sp:
            return JSONResponse({"error": "path_out_of_root"}, status_code=400)
        target = _sanitize_path(os.path.join(dest_dir, os.path.basename(sp)), default_root=dest_dir)
        try:
            shutil.move(sp, target)
            moved.append(target)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse({"ok": True, "moved": moved})


@app.post("/fm/upload")
async def fm_upload(request: Request, path: Optional[str] = None, files: List[UploadFile] = File(...)):
    if not is_authenticated(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    sanitized = _sanitize_path(path)
    if not sanitized:
        return JSONResponse({"error": "path_out_of_root"}, status_code=400)
    dest_dir = Path(sanitized)
    if not dest_dir.exists() or not dest_dir.is_dir():
        return JSONResponse({"error": "dest_not_dir"}, status_code=400)
    saved = []
    for uf in files:
        filename = os.path.basename(uf.filename)
        out_path = dest_dir / filename
        try:
            with open(out_path, "wb") as f:
                while True:
                    chunk = await uf.read(1024 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
            saved.append(str(out_path))
        except PermissionError:
            return JSONResponse({"error": "permission_denied"}, status_code=403)
    return JSONResponse({"ok": True, "saved": saved})

@app.on_event("startup")
def _startup():
    # Start periodic sync backup
    start_periodic_sync_backup()


