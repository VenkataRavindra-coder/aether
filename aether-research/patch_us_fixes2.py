#!/usr/bin/env python3
"""Second audit follow-up (applies on top of commit 9161713).

 1 an unreconciled order stops the batch: later orders are not sent (marked skipped_after_unreconciled)
 2 cap check requires 0 <= value <= cap for strategy equity, buys and account equity (rejects NaN, -inf, +inf)

  python3 patch_us_fixes2.py            # dry run
  python3 patch_us_fixes2.py --apply
"""
import sys
from pathlib import Path

ENGINE = Path.home() / "aether_engine.py"

EDITS = [
    ("stop batch at first unreconciled order",
     "        results.append({**o, \"state\": state, \"filled_qty\": qty, \"avg_price\": px, \"order_id\": r[\"order_id\"]})\n",
     "        results.append({**o, \"state\": state, \"filled_qty\": qty, \"avg_price\": px, \"order_id\": r[\"order_id\"]})\n"
     "        if state == \"unreconciled\":\n"
     "            for rest in orders[orders.index(o) + 1:]:\n"
     "                results.append({**rest, \"state\": \"skipped_after_unreconciled\"})\n"
     "            break\n"),
    ("cap: every value within 0..cap",
     "        if not (0 < cap < float(\"inf\") and equity_before <= cap and buys <= cap and acct_eq <= cap):\n",
     "        if not (0 < cap < float(\"inf\") and 0 <= equity_before <= cap and 0 <= buys <= cap\n"
     "                and 0 <= acct_eq <= cap):\n"),
]


def main():
    apply = "--apply" in sys.argv
    src = ENGINE.read_text()
    if "skipped_after_unreconciled" in src:
        sys.exit("Already applied; nothing to do.")
    if "_us_within_approval" not in src:
        sys.exit("Commit 9161713 (audit fixes) is not installed; apply it first.")
    ok = True
    for name, old, _ in EDITS:
        n = src.count(old)
        print(f"{'OK ' if n == 1 else 'BAD'} {ENGINE.name}: {name} (anchor found {n}x)")
        ok &= n == 1
    if not ok:
        sys.exit("Not applied: every anchor must match exactly once.")
    new = src
    for _, old, repl in EDITS:
        new = new.replace(old, repl, 1)
    compile(new, str(ENGINE), "exec")
    if not apply:
        print("Dry run passed (patched file compiles in memory). Re-run with --apply to write.")
        return
    tmp = ENGINE.with_suffix(".py.new")
    tmp.write_text(new)
    tmp.replace(ENGINE)
    print(f"Applied {len(EDITS)} edits to {ENGINE.name}.")


if __name__ == "__main__":
    main()
