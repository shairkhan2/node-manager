import os
import uuid
import asyncio
import itertools
from typing import Dict, Optional, Tuple, Set, Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Form, File, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware


app = FastAPI()
SESSION_SECRET = os.getenv("SESSION_SECRET", "change_me_manager")
AGENT_REGISTRATION_KEY = os.getenv("AGENT_REGISTRATION_KEY", "changeme")
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))


class AgentConn:
    def __init__(self, name: str, websocket: WebSocket):
        self.name = name
        self.websocket = websocket
        self.id = str(uuid.uuid4())


agents: Dict[str, AgentConn] = {}
# Map (agent_id, pty_id) -> set of browser websockets subscribed to that PTY
terminal_clients: Dict[Tuple[str, str], Set[WebSocket]] = {}
# Pending request futures for agent RPCs: (agent_id, req_id) -> Future
pending_requests: Dict[Tuple[str, str], asyncio.Future] = {}
# Latest metrics per agent
agent_metrics: Dict[str, Dict[str, Any]] = {}


def is_admin(req: Request) -> bool:
    return bool(req.session.get("admin"))


@app.get("/login", response_class=HTMLResponse)
def login_page(req: Request):
    return templates.TemplateResponse("login.html", {"request": req})


@app.post("/login")
def login_submit(req: Request, username: str = Form(...), password: str = Form(...)):
    # Simple env-driven admin
    if username == os.getenv("MANAGER_USER", "admin") and password == os.getenv("MANAGER_PASS", "admin"):
        req.session["admin"] = username
        return RedirectResponse("/", status_code=303)
    return RedirectResponse("/login", status_code=303)


@app.get("/logout")
def logout(req: Request):
    req.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/", response_class=HTMLResponse)
def index(req: Request):
    if not is_admin(req):
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": req, "agents": list(agents.values())},
    )


@app.websocket("/ws/agent")
async def ws_agent(ws: WebSocket):
    await ws.accept()
    try:
        # First message must be hello with reg key and name
        first = await ws.receive_json()
        if first.get("kind") != "hello" or first.get("key") != AGENT_REGISTRATION_KEY:
            await ws.close(code=1008)
            return
        name = first.get("name") or "agent"
        agent = AgentConn(name=name, websocket=ws)
        agents[agent.id] = agent
        # Notify dashboard via in-memory; dashboard polls HTTP
        try:
            while True:
                # Receive messages from agent: may be pty_output, pty_exit, pings, etc.
                msg = await ws.receive_text()
                # Try decode JSON; ignore if not JSON
                try:
                    import json as _json
                    data = _json.loads(msg)
                except Exception:
                    continue
                kind = data.get("kind")
                # Agent metrics push
                if kind == "metrics":
                    agent_metrics[agent.id] = data.get("metrics", {})
                    continue
                if kind == "pty_output":
                    key = (agent.id, data.get("pty_id"))
                    payload = data.get("data", "")
                    # fan-out to all browser clients for this PTY
                    for client in list(terminal_clients.get(key, set())):
                        try:
                            await client.send_text(payload)
                        except Exception:
                            # cleanup on failure
                            try:
                                terminal_clients.get(key, set()).discard(client)
                            except Exception:
                                pass
                elif kind == "pty_exit":
                    key = (agent.id, data.get("pty_id"))
                    # notify clients
                    note = "\r\n\x1b[31m[PTY exited]\x1b[0m\r\n"
                    for client in list(terminal_clients.get(key, set())):
                        try:
                            await client.send_text(note)
                            await client.close()
                        except Exception:
                            pass
                    terminal_clients.pop(key, None)
                else:
                    # Resolve pending RPC replies if present
                    req_id = data.get("req_id")
                    if req_id:
                        key = (agent.id, req_id)
                        fut = pending_requests.get(key)
                        if fut and not fut.done():
                            fut.set_result(data)
        except WebSocketDisconnect:
            pass
        finally:
            agents.pop(agent.id, None)
    except Exception:
        await ws.close()


@app.get("/agents")
def list_agents(req: Request):
    if not is_admin(req):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    results = []
    for a in agents.values():
        m = agent_metrics.get(a.id) or {}
        results.append({"id": a.id, "name": a.name, "metrics": m})
    return JSONResponse({"agents": results})


@app.get("/terminal/{agent_id}", response_class=HTMLResponse)
def open_terminal(req: Request, agent_id: str):
    if not is_admin(req):
        return RedirectResponse("/login", status_code=303)
    if agent_id not in agents:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse("terminal.html", {"request": req, "agent_id": agent_id})


@app.get("/agent/{agent_id}", response_class=HTMLResponse)
def agent_detail(req: Request, agent_id: str):
    if not is_admin(req):
        return RedirectResponse("/login", status_code=303)
    if agent_id not in agents:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse("agent_detail.html", {"request": req, "agent_id": agent_id})


@app.websocket("/ws/terminal/{agent_id}")
async def ws_terminal(ws: WebSocket, agent_id: str):
    # simple session auth check
    session = ws.scope.get("session") or {}
    if not session.get("admin"):
        await ws.close(code=1008)
        return
    if agent_id not in agents:
        await ws.close(code=1001)
        return
    await ws.accept()
    agent = agents[agent_id]
    # Create a PTY id for this session
    pty_id = str(uuid.uuid4())
    key = (agent_id, pty_id)
    terminal_clients.setdefault(key, set()).add(ws)
    # Ask agent to spawn a PTY
    import json as _json
    try:
        await agent.websocket.send_text(_json.dumps({"kind": "spawn_pty", "pty_id": pty_id, "cols": 80, "rows": 24}))
    except Exception:
        await ws.close(code=1011)
        terminal_clients.get(key, set()).discard(ws)
        return
    try:
        while True:
            msg = await ws.receive_text()
            # Try parse resize messages as JSON
            try:
                data = _json.loads(msg)
                if isinstance(data, dict) and data.get("type") == "resize":
                    cols = int(data.get("cols") or 80)
                    rows = int(data.get("rows") or 24)
                    await agent.websocket.send_text(_json.dumps({"kind": "pty_resize", "pty_id": pty_id, "cols": cols, "rows": rows}))
                    continue
            except Exception:
                pass
            # Otherwise forward as input
            await agent.websocket.send_text(_json.dumps({"kind": "pty_input", "pty_id": pty_id, "data": msg}))
    except WebSocketDisconnect:
        pass
    finally:
        # Tell agent to kill PTY
        try:
            await agent.websocket.send_text(_json.dumps({"kind": "pty_kill", "pty_id": pty_id}))
        except Exception:
            pass
        try:
            terminal_clients.get(key, set()).discard(ws)
        except Exception:
            pass


# ----------- Agent RPC helpers -----------

async def _send_agent_rpc(agent_id: str, payload: Dict[str, Any], timeout: float = 30.0) -> Dict[str, Any]:
    import json as _json
    if agent_id not in agents:
        raise RuntimeError("agent not connected")
    req_id = str(uuid.uuid4())
    payload = dict(payload)
    payload["req_id"] = req_id
    agent = agents[agent_id]
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    pending_requests[(agent_id, req_id)] = fut
    await agent.websocket.send_text(_json.dumps(payload))
    try:
        result = await asyncio.wait_for(fut, timeout=timeout)
        return result
    finally:
        pending_requests.pop((agent_id, req_id), None)


# ----------- Remote File Manager -----------

@app.get("/files/{agent_id}", response_class=HTMLResponse)
def agent_files_page(req: Request, agent_id: str, path: Optional[str] = None):
    if not is_admin(req):
        return RedirectResponse("/login", status_code=303)
    if agent_id not in agents:
        return RedirectResponse("/", status_code=303)
    start_path = path or "/root"
    return templates.TemplateResponse(
        "remote_file_manager.html",
        {"request": req, "agent_id": agent_id, "start_path": start_path, "roots": ["/root", "/home/ubuntu"]},
    )


@app.get("/mgr/fm/list")
async def mgr_fm_list(req: Request, agent_id: str, path: Optional[str] = None):
    if not is_admin(req):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        res = await _send_agent_rpc(agent_id, {"kind": "fm_list", "path": path or "/root"})
        return JSONResponse(res.get("data") or {"error": "bad_reply"})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/mgr/fm/download")
async def mgr_fm_download(req: Request, agent_id: str, path: str):
    if not is_admin(req):
        return RedirectResponse("/login", status_code=303)
    try:
        res = await _send_agent_rpc(agent_id, {"kind": "fm_read", "path": path}, timeout=120)
        ok = res.get("ok")
        if not ok:
            return JSONResponse({"error": res.get("error", "read_failed")}, status_code=400)
        import base64
        data_b64 = res.get("data_b64", "")
        content = base64.b64decode(data_b64)
        filename = os.path.basename(path)
        from starlette.responses import Response
        return Response(content, media_type="application/octet-stream", headers={"Content-Disposition": f"attachment; filename={filename}"})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/mgr/fm/upload")
async def mgr_fm_upload(req: Request, agent_id: str, path: str, files: Any = File(...)):
    if not is_admin(req):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        import base64
        for uf in files:
            content = await uf.read()
            b64 = base64.b64encode(content).decode("ascii")
            target = path.rstrip("/") + "/" + os.path.basename(uf.filename)
            res = await _send_agent_rpc(agent_id, {"kind": "fm_write", "path": target, "data_b64": b64}, timeout=120)
            if not res.get("ok"):
                return JSONResponse({"error": res.get("error", "upload_failed")}, status_code=400)
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/mgr/fm/mkdir")
async def mgr_fm_mkdir(req: Request):
    if not is_admin(req):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    data = await req.json()
    agent_id = data.get("agent_id")
    base = data.get("path")
    name = data.get("name")
    try:
        res = await _send_agent_rpc(agent_id, {"kind": "fm_mkdir", "path": base, "name": name})
        if not res.get("ok"):
            return JSONResponse({"error": res.get("error", "mkdir_failed")}, status_code=400)
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/mgr/fm/delete")
async def mgr_fm_delete(req: Request):
    if not is_admin(req):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    data = await req.json()
    agent_id = data.get("agent_id")
    path = data.get("path")
    try:
        res = await _send_agent_rpc(agent_id, {"kind": "fm_delete", "path": path})
        if not res.get("ok"):
            return JSONResponse({"error": res.get("error", "delete_failed")}, status_code=400)
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/mgr/fm/rename")
async def mgr_fm_rename(req: Request):
    if not is_admin(req):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    data = await req.json()
    agent_id = data.get("agent_id")
    path = data.get("path")
    new_name = data.get("new_name")
    try:
        res = await _send_agent_rpc(agent_id, {"kind": "fm_rename", "path": path, "new_name": new_name})
        if not res.get("ok"):
            return JSONResponse({"error": res.get("error", "rename_failed")}, status_code=400)
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


# ----------- Gensyn controls -----------

@app.get("/mgr/gensyn/status")
async def mgr_gensyn_status(req: Request, agent_id: str):
    if not is_admin(req):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        res = await _send_agent_rpc(agent_id, {"kind": "gensyn_status"}, timeout=15)
        return JSONResponse(res)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/mgr/gensyn/start")
async def mgr_gensyn_start(req: Request):
    if not is_admin(req):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    data = await req.json()
    agent_id = data.get("agent_id")
    try:
        res = await _send_agent_rpc(agent_id, {"kind": "gensyn_start"}, timeout=60)
        return JSONResponse(res)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/mgr/gensyn/stop")
async def mgr_gensyn_stop(req: Request):
    if not is_admin(req):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    data = await req.json()
    agent_id = data.get("agent_id")
    try:
        res = await _send_agent_rpc(agent_id, {"kind": "gensyn_stop"}, timeout=30)
        return JSONResponse(res)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/mgr/login/start")
async def mgr_login_start(req: Request):
    if not is_admin(req):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    data = await req.json()
    agent_id = data.get("agent_id")
    try:
        res = await _send_agent_rpc(agent_id, {"kind": "login_start"}, timeout=10)
        return JSONResponse(res)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/mgr/login/email")
async def mgr_login_email(req: Request):
    if not is_admin(req):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    data = await req.json()
    agent_id = data.get("agent_id")
    email = data.get("email")
    try:
        res = await _send_agent_rpc(agent_id, {"kind": "login_email", "email": email}, timeout=10)
        return JSONResponse(res)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/mgr/login/otp")
async def mgr_login_otp(req: Request):
    if not is_admin(req):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    data = await req.json()
    agent_id = data.get("agent_id")
    otp = data.get("otp")
    try:
        res = await _send_agent_rpc(agent_id, {"kind": "login_otp", "otp": otp}, timeout=10)
        return JSONResponse(res)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/mgr/login/status")
async def mgr_login_status(req: Request, agent_id: str):
    if not is_admin(req):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        res = await _send_agent_rpc(agent_id, {"kind": "login_status"}, timeout=10)
        return JSONResponse(res)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/mgr/login/screenshot")
async def mgr_login_screenshot(req: Request, agent_id: str):
    if not is_admin(req):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        res = await _send_agent_rpc(agent_id, {"kind": "login_shot"}, timeout=20)
        ok = res.get("ok")
        if not ok:
            return JSONResponse({"error": res.get("error", "no_shot")}, status_code=400)
        import base64
        b64 = res.get("data_b64", "")
        content = base64.b64decode(b64)
        from starlette.responses import Response
        return Response(content, media_type="image/png")
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/mgr/gensyn/peer")
async def mgr_gensyn_peer(req: Request, agent_id: str):
    if not is_admin(req):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        res = await _send_agent_rpc(agent_id, {"kind": "peer_info"}, timeout=20)
        return JSONResponse(res)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/mgr/fm/move")
async def mgr_fm_move(req: Request):
    if not is_admin(req):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    data = await req.json()
    agent_id = data.get("agent_id")
    srcs = data.get("srcs") or []
    dest_dir = data.get("dest_dir")
    try:
        res = await _send_agent_rpc(agent_id, {"kind": "fm_move", "srcs": srcs, "dest_dir": dest_dir})
        if not res.get("ok"):
            return JSONResponse({"error": res.get("error", "move_failed")}, status_code=400)
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


# ----------- Run Command -----------

@app.get("/command/{agent_id}", response_class=HTMLResponse)
def command_page(req: Request, agent_id: str):
    if not is_admin(req):
        return RedirectResponse("/login", status_code=303)
    if agent_id not in agents:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse("command.html", {"request": req, "agent_id": agent_id, "result": None})


@app.post("/command/{agent_id}", response_class=HTMLResponse)
async def command_run(req: Request, agent_id: str, cmd: str = Form(...)):
    if not is_admin(req):
        return RedirectResponse("/login", status_code=303)
    if agent_id not in agents:
        return RedirectResponse("/", status_code=303)
    try:
        res = await _send_agent_rpc(agent_id, {"kind": "exec", "cmd": cmd}, timeout=300)
        return templates.TemplateResponse("command.html", {"request": req, "agent_id": agent_id, "result": res})
    except Exception as e:
        return templates.TemplateResponse("command.html", {"request": req, "agent_id": agent_id, "result": {"error": str(e)}})


# ----------- Status & VPN -----------

@app.get("/mgr/status/public_ip")
async def mgr_public_ip(req: Request, agent_id: str):
    if not is_admin(req):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        res = await _send_agent_rpc(agent_id, {"kind": "public_ip"}, timeout=10)
        return JSONResponse(res)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/mgr/vpn/status")
async def mgr_vpn_status(req: Request, agent_id: str):
    if not is_admin(req):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        res = await _send_agent_rpc(agent_id, {"kind": "vpn_status"}, timeout=10)
        return JSONResponse(res)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/mgr/vpn/on")
async def mgr_vpn_on(req: Request):
    if not is_admin(req):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    data = await req.json()
    agent_id = data.get("agent_id")
    try:
        res = await _send_agent_rpc(agent_id, {"kind": "vpn_on"}, timeout=30)
        return JSONResponse(res)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/mgr/vpn/off")
async def mgr_vpn_off(req: Request):
    if not is_admin(req):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    data = await req.json()
    agent_id = data.get("agent_id")
    try:
        res = await _send_agent_rpc(agent_id, {"kind": "vpn_off"}, timeout=30)
        return JSONResponse(res)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/mgr/vpn/config")
async def mgr_vpn_config(req: Request):
    if not is_admin(req):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    data = await req.json()
    agent_id = data.get("agent_id")
    config = data.get("config")
    if not config:
        return JSONResponse({"error": "missing_config"}, status_code=400)
    try:
        res = await _send_agent_rpc(agent_id, {"kind": "vpn_set_config", "config": config}, timeout=30)
        return JSONResponse(res)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


