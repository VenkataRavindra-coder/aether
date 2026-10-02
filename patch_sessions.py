"""Keeps you logged in to the AETHER dashboard across engine restarts (7-day signed cookie)."""
import pathlib
import sys

P = pathlib.Path.home() / "aether_engine.py"
src = P.read_text()

if "_session_ok(" in src:
    sys.exit("Already patched, nothing to do.")


def replace_once(s, old, new, what):
    if s.count(old) != 1:
        sys.exit(f"Aborted, nothing changed: couldn't find {what} exactly once.")
    return s.replace(old, new)


HELPERS = '''import hmac as _hmac
import hashlib as _hashlib
import os as _os
import pathlib as _pathlib

_SESSION_DAYS = 7
_SESSION_KEY_FILE = _pathlib.Path.home() / ".aether_session_key"


def _rotate_session_key():
    """New signing key: every existing login cookie stops working."""
    key = secrets.token_bytes(32)
    tmp = _SESSION_KEY_FILE.with_name(_SESSION_KEY_FILE.name + ".tmp")
    tmp.write_bytes(key)
    _os.chmod(tmp, 0o600)
    tmp.replace(_SESSION_KEY_FILE)
    return key


def _session_key():
    try:
        key = _SESSION_KEY_FILE.read_bytes()
        if len(key) >= 32:
            return key
    except FileNotFoundError:
        pass
    return _rotate_session_key()


def _session_token():
    expires = str(int(_time.time()) + _SESSION_DAYS * 86400)
    sig = _hmac.new(_session_key(), expires.encode(), _hashlib.sha256).hexdigest()
    return expires + "." + sig


def _session_ok(token):
    try:
        expires, sig = token.split(".", 1)
        if int(expires) < _time.time():
            return False
        good = _hmac.new(_session_key(), expires.encode(), _hashlib.sha256).hexdigest()
        return _hmac.compare_digest(sig, good)
    except Exception:
        return False


'''

anchor = "from starlette.responses import JSONResponse as _JSONResponse"
src = replace_once(src, anchor, HELPERS + anchor, "the dashboard login middleware")
src = replace_once(
    src,
    '''    sid = secrets.token_urlsafe(32)
    _SESSIONS.add(sid)
    resp = _RedirectResponse("/", status_code=303)
    resp.set_cookie(_SESSION_COOKIE, sid, httponly=True, samesite="strict", max_age=86400)''',
    '''    sid = _session_token()
    resp = _RedirectResponse("/", status_code=303)
    resp.set_cookie(_SESSION_COOKIE, sid, httponly=True, samesite="strict", max_age=_SESSION_DAYS * 86400)''',
    "the login cookie code",
)
src = replace_once(src, '    _SESSIONS.discard(request.cookies.get(_SESSION_COOKIE, ""))', "    _rotate_session_key()", "the logout code")
src = replace_once(src, "if path in _PUBLIC_PATHS or (sid and sid in _SESSIONS):", "if path in _PUBLIC_PATHS or (sid and _session_ok(sid)):", "the session check")

compile(src, str(P), "exec")
P.with_name(P.name + ".bak-sessions").write_text(P.read_text())
P.write_text(src)
print("Patched: logins now last 7 days and survive restarts.")
