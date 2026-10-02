"""Switches AETHER to the new dashboard. Run on the VM from the home directory.
New page at /, old page kept at /classic. Writes nothing unless every check passes."""
import pathlib
import sys

HOME = pathlib.Path.home()
P = HOME / "aether_engine.py"
src = P.read_text()

if "dashboard_v2" in src:
    sys.exit("Already installed, nothing to do.")
for f in ("dashboard_v2.py", "dashboard_v2.html"):
    if not (HOME / f).exists():
        sys.exit(f"~/{f} is missing: copy it to the VM first. Nothing changed.")
if "_dashboard_auth" not in src:
    sys.exit("The login patch isn't installed, so the new page would be unprotected. Nothing changed.")

OLD = '@app.get("/", response_class=HTMLResponse)'
if src.count(OLD) != 1:
    sys.exit("Couldn't find the old dashboard route exactly once. Nothing changed.")
new = src.replace(OLD, '@app.get("/classic", response_class=HTMLResponse)')
new = new.rstrip("\n") + (
    "\n\n\n# New dashboard at /, old one kept at /classic.\n"
    "import dashboard_v2 as _dashboard_v2  # noqa: E402\n"
    "app.include_router(_dashboard_v2.router)\n"
)
compile(new, str(P), "exec")
compile((HOME / "dashboard_v2.py").read_text(), "dashboard_v2.py", "exec")
P.with_name(P.name + ".bak-v2").write_text(src)
P.write_text(new)
print("Installed: new dashboard at /, classic view at /classic")
