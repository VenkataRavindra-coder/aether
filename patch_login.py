import os, shutil, sys

SRC = os.path.expanduser("~/aether_engine.py")
BAK = SRC + ".bak-before-login"
MARK = "# --- DASHBOARD LOGIN (cookie session) ---"

try:
    import python_multipart
except ImportError:
    try:
        import multipart
    except ImportError:
        sys.exit("python-multipart missing. Run: ~/venv/bin/pip install python-multipart")

src = open(SRC).read()
if MARK in src:
    sys.exit("Already patched.")

BLOCK = r'''# --- DASHBOARD LOGIN (cookie session) ---
import time as _time
from urllib.parse import parse_qsl as _parse_qsl, urlencode as _urlencode
from fastapi import Form as _Form, Request as _Request
from fastapi.responses import HTMLResponse as _HTMLResponse, RedirectResponse as _RedirectResponse

DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")
_SESSIONS = set()
_SESSION_COOKIE = "aether_session"
_OPEN_PATHS = {"/login", "/logout", "/webhook/tradingview"}


@app.middleware("http")
async def _dashboard_session(request: _Request, call_next):
    path = request.url.path
    if path not in _OPEN_PATHS:
        sid = request.cookies.get(_SESSION_COOKIE, "")
        if sid and sid in _SESSIONS:
            raw = request.scope.get("query_string", b"").decode()
            pairs = [(k, v) for k, v in _parse_qsl(raw, keep_blank_values=True) if k != "secret"]
            pairs.append(("secret", WEBHOOK_SECRET))
            request.scope["query_string"] = _urlencode(pairs).encode()
        elif path == "/" and "secret" not in request.query_params:
            return _RedirectResponse("/login", status_code=303)
    return await call_next(request)


_LOGIN_HTML = """<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AETHER login</title>
<body style="background:#0d1117;color:#c9d1d9;font-family:system-ui;display:flex;height:100vh;align-items:center;justify-content:center;margin:0">
<form method="post" action="/login" style="display:flex;gap:8px">
<input type="password" name="password" placeholder="Password" autofocus style="padding:10px;border-radius:6px;border:1px solid #30363d;background:#161b22;color:#c9d1d9">
<button style="padding:10px 16px;border-radius:6px;border:0;background:#238636;color:#fff">Enter</button>
</form></body>"""


@app.get("/login", response_class=_HTMLResponse)
def _login_page():
    return _LOGIN_HTML


@app.post("/login")
def _login(password: str = _Form(default="")):
    if not DASHBOARD_PASSWORD:
        raise HTTPException(status_code=503, detail="DASHBOARD_PASSWORD is not set in .env")
    if not secrets.compare_digest(password.encode(), DASHBOARD_PASSWORD.encode()):
        _time.sleep(1)
        raise HTTPException(status_code=401, detail="Wrong password")
    sid = secrets.token_urlsafe(32)
    _SESSIONS.add(sid)
    resp = _RedirectResponse("/", status_code=303)
    resp.set_cookie(_SESSION_COOKIE, sid, httponly=True, samesite="strict", max_age=86400)
    return resp


@app.get("/logout")
def _logout(request: _Request):
    _SESSIONS.discard(request.cookies.get(_SESSION_COOKIE, ""))
    resp = _RedirectResponse("/login", status_code=303)
    resp.delete_cookie(_SESSION_COOKIE)
    return resp
'''

lines = src.split("\n")
idx = next((i for i, l in enumerate(lines) if l.startswith("@app.")), None)
if idx is None:
    sys.exit("No @app. route found to insert before.")

new = "\n".join(lines[:idx]) + "\n" + BLOCK + "\n\n" + "\n".join(lines[idx:])
compile(new, SRC, "exec")          # syntax check only, writes nothing
shutil.copy2(SRC, BAK)
tmp = SRC + ".new"
open(tmp, "w").write(new)
os.replace(tmp, SRC)
print("Patched. Backup at", BAK)
