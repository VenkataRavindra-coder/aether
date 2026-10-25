#!/usr/bin/env python3
"""Daily research health check and report (timer: weekdays 16:45 New York, after the forward journals).

- Builds the research picture from files (research_registry.build): no broker calls, no orders.
- Writes aether-research/registry/status.json (the snapshot the /research page also shows).
- Telegram: an alert whenever there is a problem (skipped or failed run, missing data, duplicate rows,
  replay mismatches, trial count drift, registry errors), and a weekly summary on Fridays.
- Each check is isolated: one broken experiment shows up as a problem line, it never stops the others.

  python3 research_health.py run       # the timer runs this
  python3 research_health.py show      # print the snapshot, send nothing
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import research_registry as rr

REPO = Path(__file__).resolve().parent


def summary_text(st: dict) -> str:
    s = st.get("summary") or {}
    lines = [f"AETHER research weekly ({st.get('generated_at', '')[:10]})",
             f"{s.get('strategies')} strategies; grades: " + ", ".join(f"{k} {v}" for k, v in (s.get('by_grade') or {}).items())]
    best = s.get("best")
    if best:
        lines.append(f"Strongest evidence: {best['name']} ({best['grade']})")
    for it in st.get("strategies", []):
        f = it.get("forward")
        if f:
            m = "-" if f.get("mean_net_bp") is None else f"{f['mean_net_bp']:+.1f} bp"
            lines.append(f"  forward {it['id']}: {f['trades']}/{f.get('target_trades')} trades, avg {m}, ${f['net_usd_5k']:+.2f} on $5k")
        p = it.get("paper") or {}
        if p.get("trades") is not None:
            lines.append(f"  paper {it['id']}: {p['trades']}/{p.get('target_trades')} trades, ${p.get('net_usd', 0):+.2f}")
    lines.append(f"Target ${s.get('target_usd_per_day'):.0f}/day: live none; paper avg "
                 + ("-" if s.get("paper_usd_per_day") is None else f"${s['paper_usd_per_day']:+.2f}") + "/day")
    probs = rr.problems(st)
    lines.append("Problems: none" if not probs else f"Problems: {len(probs)} (see /research)")
    return "\n".join(lines)


def alert_text(probs: list[str]) -> str:
    return "[RESEARCH] " + str(len(probs)) + " problem(s):\n" + "\n".join("- " + p for p in probs[:12])


def write_snapshot(st: dict, path: Path | None = None):
    path = path or rr.STATUS_OUT
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    tmp.write_text(json.dumps(st, indent=1, default=str))
    tmp.rename(path)


def run(now: datetime | None = None, send=None) -> int:
    now = now or datetime.now(rr.NY)
    if send is None:
        import notify
        send = notify.send_sync
    st = rr.build(now=now)
    write_snapshot(st)
    probs = rr.problems(st) if st.get("ok") else (st.get("errors") or ["research registry unreadable"])
    if probs:
        send(alert_text(probs))
    if now.astimezone(rr.NY).weekday() == 4:
        send(summary_text(st))
    print(json.dumps({"problems": probs, "summary": st.get("summary")}, indent=1, default=str))
    return 0          # problems are reported by Telegram and the page; the job itself succeeded


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["run", "show"])
    a = ap.parse_args(argv)
    if a.cmd == "show":
        st = rr.build()
        print(json.dumps({"problems": rr.problems(st), "summary": st.get("summary"),
                          "forward_jobs": st.get("forward_jobs"), "trials": st.get("trials")}, indent=1, default=str))
        return 0
    return run()


if __name__ == "__main__":
    sys.exit(main())
