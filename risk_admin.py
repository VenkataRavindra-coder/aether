"""Manual risk admin.
  python risk_admin.py status
  python risk_admin.py mark [--equity 100]      # initialise / manual mark
  python risk_admin.py cashflow +50             # deposit (+) or withdrawal (-)
  python risk_admin.py reset                    # after manual review of a latched breaker
"""
from __future__ import annotations

import json
import sys

import aether_ops
import safety_guard as g


def main(argv: list[str]) -> int:
    aether_ops.load_env()
    cmd = argv[1] if len(argv) > 1 else "status"
    if cmd == "status":
        L = g.load_ledger()
        print(json.dumps({"limits": g.limits().__dict__, "halt_file": g.halt_file().exists(),
                          "ledger": L}, indent=2))
    elif cmd == "mark":
        eq = float(argv[3]) if len(argv) > 3 and argv[2] == "--equity" else \
            float(aether_ops.alpaca_live_get("/v2/account")["equity"])
        print(json.dumps(g.mark(eq), indent=2))
    elif cmd == "cashflow":
        g.record_cash_flow(float(argv[2]))
        print("recorded cash flow", argv[2])
    elif cmd == "reset":
        g.reset_after_review()
        print("latch cleared, anchors rebased.")
        if g.halt_file().exists():
            print("~/AETHER_HALT still present: `rm ~/AETHER_HALT` to resume entries.")
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
