import re, sys, pathlib

P = pathlib.Path.home() / "aether_engine.py"
orig = P.read_text()
if "_dashboard_auth" in orig:
    sys.exit("Already patched, nothing to do.")

ROUTES = {"/api/strategy", "/api/backtest", "/api/us", "/account",
          "/api/stocks", "/api/history", "/api/portfolio-history", "/api/snapshot"}

# 1. Dashboard routes stop checking the webhook secret; the middleware checks the session instead.
out, done = [], {}
route, in_body = None, False
for line in orig.split("\n"):
    m = re.match(r'@app\.\w+\("([^"]*)"', line)
    if m:
        route, in_body = (m.group(1) if m.group(1) in ROUTES else None), False
    elif route and not in_body and re.match(r"(async\s+)?def\s", line):
        in_body = True
    elif route and in_body and line and not line[0].isspace() and not line.startswith("#"):
        route = None
    elif route and in_body and line.strip() == "verify_access(secret=secret)":
        line = line[: len(line) - len(line.lstrip())] + "pass  # auth: _dashboard_auth session middleware"
        done[route] = done.get(route, 0) + 1
    out.append(line)

bad = sorted(r for r in ROUTES if done.get(r) != 1)
if bad:
    sys.exit(f"Aborted, no changes written. Unexpected auth code in: {bad}")
src = "\n".join(out)

def replace_once(s, old, new, what):
    if s.count(old) != 1:
        sys.exit(f"Aborted, no changes written. No unique anchor for {what}.")
    return s.replace(old, new)

# 2. Session middleware: fail closed, public paths are explicit.
MIDDLEWARE = '''from starlette.responses import JSONResponse as _JSONResponse

_PUBLIC_PATHS = {"/login", "/logout", "/webhook/tradingview"}


@app.middleware("http")
async def _dashboard_auth(request: _Request, call_next):
    """Every path except login/logout and the TradingView webhook needs a dashboard session.
    Fails closed: unknown paths (including /docs and /openapi.json) are refused too."""
    path = request.url.path
    sid = request.cookies.get(_SESSION_COOKIE, "")
    if path in _PUBLIC_PATHS or (sid and sid in _SESSIONS):
        return await call_next(request)
    if request.method == "GET" and path == "/":
        return _RedirectResponse("/login", status_code=303)
    return _JSONResponse({"detail": "Not logged in."}, status_code=401)


'''
src = replace_once(src, '@app.get("/api/strategy")', MIDDLEWARE + '@app.get("/api/strategy")', "the middleware")

# 3. Page: no secret in URLs; any 401 goes to the login page instead of showing fake zeros.
src = replace_once(src,
    "const qs = 'secret=' + encodeURIComponent(secretToken);",
    "const qs = '';  // auth is the session cookie now\n"
    "            const _origFetch = window.fetch.bind(window);\n"
    "            window.fetch = async (...args) => {\n"
    "                const r = await _origFetch(...args);\n"
    "                if (r.status === 401) window.location.href = '/login';\n"
    "                return r;\n"
    "            };",
    "the page's qs line")

compile(src, str(P), "exec")          # refuse to write a file that doesn't parse
P.with_name(P.name + ".bak-auth").write_text(orig)
P.write_text(src)
print("Patched routes:", ", ".join(sorted(done)))
