"""Daily live-equity mark (systemd timer, Mon-Fri 15:50 ET). Read-only on Alpaca.
Evaluates weekly/monthly/drawdown breakers, latches + writes ~/AETHER_HALT on HALT,
sends one Telegram summary per run."""
from __future__ import annotations

import sys

import aether_ops
import safety_guard as g


def main() -> int:
    aether_ops.load_env()
    try:
        equity = float(aether_ops.alpaca_live_get("/v2/account")["equity"])
    except Exception as e:
        aether_ops.telegram(f"AETHER risk_mark FAILED to read live equity: {e}")
        return 1
    if equity <= 0:
        print("live account unfunded; ledger not marked")
        return 0
    s = g.mark(equity)
    icon = {"OK": "OK", "WARN": "WARN", "REDUCE": "REDUCE (size x0.5)", "HALT": "HALT"}[s["level"]]
    msg = (f"AETHER live risk mark: {icon}\n"
           f"equity ${s['equity']:.2f} | peak ${s['peak']:.2f} | DD {s['drawdown']:.2%}\n"
           f"week {-s['weekly_loss']:+.2%} | month {-s['monthly_loss']:+.2%}")
    if s.get("latched"):
        msg += "\nLATCHED: " + "; ".join(s["latched"]["reasons"]) + "\nEntries blocked until review + risk_admin.py reset"
    aether_ops.telegram(msg)
    print(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
