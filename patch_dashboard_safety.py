"""Display-only dashboard fixes. Run once from ~ on the VM:
    /home/azureuser/venv/bin/python3 patch_dashboard_safety.py
Changes nothing unless every anchor is found exactly once. Keeps .bak copies.
No trading logic changes: one read-only route, page rendering, one Telegram header line."""
import pathlib
import shutil
import sys

ROUTE = '''

@router.get("/api/safety")
def safety():
    """Read-only safety snapshot: live mode, manual halt, live buy budget, last backup."""
    import glob, json, time
    from pathlib import Path
    home = Path.home()
    halt = Path(os.getenv("AETHER_HALT_FILE", str(home / "AETHER_HALT"))).exists()
    spent = 0.0
    try:
        s = json.loads(Path(os.getenv("AETHER_RISK_STATE", str(home / ".aether_risk_state.json"))).read_text())
        if s.get("day") == time.strftime("%Y-%m-%d", time.gmtime()):
            spent = float(s.get("usd", 0.0))
    except FileNotFoundError:
        pass
    except Exception:
        spent = None
    archives = sorted(glob.glob(str(home / "backups" / "aether-*.tar.gz")))
    last = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(os.path.getmtime(archives[-1]))) if archives else None
    return {"us_live": _flag("US_LIVE"), "halt": halt,
            "live_budget_usd": _f(os.getenv("RISK_MAX_DAILY_BUY_USD_LIVE") or 100),
            "live_spent_today_usd": spent, "last_backup_utc": last}
'''

SCRIPT = """<script>
async function loadSafety(){try{const r=await fetch('/api/safety',{credentials:'same-origin'});if(!r.ok)return;const d=await r.json();window.__safety=d;
const b=document.getElementById('liveBudget');if(b)b.textContent=d.live_spent_today_usd==null?'unreadable':'$'+d.live_spent_today_usd.toFixed(2)+' of $'+d.live_budget_usd.toFixed(0);
const k=document.getElementById('lastBackup');if(k){if(!d.last_backup_utc){k.textContent='none found'}else{const h=(Date.now()-Date.parse(d.last_backup_utc))/3.6e6;k.textContent=(h<1?'under 1':Math.round(h))+' h ago'+(h>26?' (late)':'')}}
if(typeof renderSafety==='function'){try{renderSafety()}catch(e){}}}catch(e){}}
loadSafety();setInterval(loadSafety,30000);
</script>
"""

HTML_EDITS = [
    # paper mode: the $100 live cap does not apply, so show that instead of a fake breach
    ("$('exposure').textContent=money(exposure)+' current exposure';",
     "const live=window.__safety?.us_live===true;$('exposure').textContent=money(exposure)+(live?' live exposure':' paper exposure');"),
    ("$('room').textContent=valid(limit)&&valid(exposure)?money(limit-exposure)+' remaining':'';",
     "$('room').textContent=!live?'cap applies only in LIVE':valid(limit)&&valid(exposure)?money(limit-exposure)+' remaining':'';"),
    ("$('meter').style.width=valid(limit)&&limit>0&&valid(exposure)?",
     "$('meter').style.width=live&&valid(limit)&&limit>0&&valid(exposure)?"),
    # status pill: manual halt first, then crypto kill switch
    ("$('gateStatus').textContent=kill===true?'Kill switch ON':kill===false?'Kill switch off':'Status unknown';"
     "$('gateStatus').className='pill '+(kill===true?'red':'muted');",
     "const halt=window.__safety?.halt===true;"
     "$('gateStatus').textContent=halt?'HALTED (manual)':kill===true?'Crypto kill switch ON':kill===false?'All clear':'Status unknown';"
     "$('gateStatus').className='pill '+(halt||kill===true?'red':'muted');"),
    # two new rows under Engine status
    ('<span id="engineState">Unknown</span></div></section>',
     '<span id="engineState">Unknown</span></div>'
     '<div class="split sub" style="margin-top:9px"><span>Live buys today</span><span id="liveBudget">—</span></div>'
     '<div class="split sub" style="margin-top:9px"><span>Last backup</span><span id="lastBackup">—</span></div></section>'),
]

ENGINE_EDIT = ('f"[US] Monthly rebalance ({_us_mode()}) {uae_str()}"',
               'f"[US] Monthly rebalance ({_us_mode()}){\' INCOMPLETE, will retry\' if failed else \'\'} {uae_str()}"')


def check(text, old, name):
    n = text.count(old)
    if n != 1:
        sys.exit(f"ABORT: anchor in {name} found {n} times, nothing changed:\n  {old[:80]}")


py = pathlib.Path("dashboard_v2.py")
html = pathlib.Path("dashboard_v2.html")
eng = pathlib.Path("aether_engine.py")
p_s, h_s, e_s = py.read_text(), html.read_text(), eng.read_text()

if '"/api/safety"' in p_s:
    sys.exit("already patched, nothing changed")
for name in ("@router.get(\"/api/overview\")", "def _flag(", "def _f("):
    check(p_s, name, "dashboard_v2.py")
for old, _ in HTML_EDITS:
    check(h_s, old, "dashboard_v2.html")
check(e_s, ENGINE_EDIT[0], "aether_engine.py")

for f in (py, html, eng):
    shutil.copyfile(f, f.with_name(f.name + ".bak-safety"))

p_s = p_s.rstrip("\n") + "\n" + ROUTE
for old, new in HTML_EDITS:
    h_s = h_s.replace(old, new, 1)
h_s = h_s.replace("</body>", SCRIPT + "</body>", 1) if "</body>" in h_s else h_s + "\n" + SCRIPT
e_s = e_s.replace(*ENGINE_EDIT, 1)

py.write_text(p_s)
html.write_text(h_s)
eng.write_text(e_s)
print("patched: /api/safety route, guardrail panel, status pill, 2 rows, Telegram INCOMPLETE header")
print("backups: *.bak-safety (delete after checking the dashboard)")
