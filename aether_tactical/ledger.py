"""Append-only paper ledger (JSONL) and the pre-registered verdict.

One row per (day, strategy). Verdict rules (pre-registered 2026-10-21):
- "collecting" until MIN_SESSIONS scored sessions;
- then "unsupported" if mean net bp per trade <= 0, or fewer than MIN_TRADES trades;
- "worth a live micro test" only if mean net bp > 0 AND t-stat >= 2.0 over at least MIN_TRADES trades;
- otherwise "inconclusive".
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path

MIN_SESSIONS, MIN_TRADES, MIN_T = 40, 20, 2.0


def path() -> Path:
    return Path(os.getenv("TACTICAL_LEDGER", str(Path(__file__).resolve().parent.parent / "tactical_paper.jsonl")))


def rows() -> list[dict]:
    p = path()
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


def has(day: str, strategy: str) -> bool:
    return any(r.get("day") == day and r.get("strategy") == strategy for r in rows())


def append(row: dict) -> bool:
    """False (and nothing written) if that day/strategy is already scored: the ledger never rewrites."""
    if has(row["day"], row["strategy"]):
        return False
    with path().open("a") as f:
        f.write(json.dumps(row, separators=(",", ":")) + "\n")
    return True


def summary() -> dict:
    out = {}
    for name in sorted({r["strategy"] for r in rows()}):
        rs = [r for r in rows() if r["strategy"] == name and r.get("state") != "no_data"]
        nets = [r["trade"]["net_bp"] for r in rs if r.get("trade")]
        n = len(nets)
        mean = sum(nets) / n if n else None
        sd = math.sqrt(sum((x - mean) ** 2 for x in nets) / (n - 1)) if n > 1 else None
        t = mean / (sd / math.sqrt(n)) if n > 1 and sd else None
        if len(rs) < MIN_SESSIONS:
            verdict = f"collecting ({len(rs)} of {MIN_SESSIONS} sessions)"
        elif n < MIN_TRADES or mean is None or mean <= 0:
            verdict = "unsupported"
        elif t is not None and t >= MIN_T:
            verdict = "worth a live micro test"
        else:
            verdict = "inconclusive"
        out[name] = {"sessions": len(rs), "trades": n, "wins": sum(1 for x in nets if x > 0),
                     "mean_net_bp": None if mean is None else round(mean, 2),
                     "t_stat": None if t is None else round(t, 2),
                     "total_net_pct": round(sum(nets) / 100, 3), "verdict": verdict}
    return out
