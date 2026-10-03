#!/usr/bin/env python3
"""Adds decision-log hooks to aether_engine.py. Additions only; no trading logic changes.

  python3 patch_decision_log.py            # dry run: checks every anchor, changes nothing
  python3 patch_decision_log.py --apply    # writes the file only if ALL anchors match exactly once
"""
import sys
from pathlib import Path

ENGINE = Path.home() / "aether_engine.py"

EDITS = [
    ("import", "import notify\n", "import notify\nimport decision_log\n"),

    ("bar_t in status",
     '    bar_t = closed[i]["t"]\n',
     '    bar_t = closed[i]["t"]\n    info["bar_t"] = bar_t\n'),

    ("crypto order",
     "    record_trade_history(state, symbol, side, qty, executed, price, status_, detail)\n",
     "    record_trade_history(state, symbol, side, qty, executed, price, status_, detail)\n"
     "    decision_log.log(\"crypto\", \"order\", symbol=symbol, decision=side, reason=note, price=price, mode=_mode(),\n"
     "                     status=status_, requested_qty=qty, executed_qty=executed, is_closing=is_closing, detail=detail)\n"),

    ("crypto decision helper",
     "def strategy_cycle(fetch=None):\n",
     "def _crypto_event(note: str) -> str:\n"
     "    if note == \"entered\":\n"
     "        return \"entry\"\n"
     "    if note.startswith(\"exited\"):\n"
     "        return \"exit\"\n"
     "    if note.startswith(\"entry signal blocked\"):\n"
     "        return \"blocked\"\n"
     "    if note.startswith(\"entry signal skipped\"):\n"
     "        return \"skipped\"\n"
     "    if note in (\"entry order rejected\",) or note.startswith(\"exit failed\"):\n"
     "        return \"order_failed\"\n"
     "    return \"evaluate\"\n"
     "\n"
     "\n"
     "def _log_crypto_decision(info: dict):\n"
     "    \"\"\"Decision log: one row per new closed bar per symbol, plus any change of state or note.\"\"\"\n"
     "    note = info.get(\"note\") or \"\"\n"
     "    keys = (\"ema_fast\", \"ema_slow\", \"ema_trend\", \"rsi\", \"atr\", \"trend_ok\", \"stop\", \"entry\", \"qty\", \"bars\")\n"
     "    decision_log.log_changed(\n"
     "        f\"crypto:{info.get('symbol')}\", (info.get(\"bar_t\"), info.get(\"state\"), note),\n"
     "        engine=\"crypto\", event=_crypto_event(note), mode=_mode(), symbol=info.get(\"symbol\"),\n"
     "        bar_time=info.get(\"bar_t\"), decision=info.get(\"state\"), reason=note or None, price=info.get(\"price\"),\n"
     "        **{k: info[k] for k in keys if k in info})\n"
     "\n"
     "\n"
     "def strategy_cycle(fetch=None):\n"),

    ("kill-switch",
     "key=f\"kill:{today}\", cooldown=86400)\n",
     "key=f\"kill:{today}\", cooldown=86400)\n"
     "    if killed != was_killed:\n"
     "        decision_log.log(\"crypto\", \"kill_switch\", decision=\"active\" if killed else \"cleared\", mode=_mode(),\n"
     "                         reason=f\"daily loss limit {STRATEGY_MAX_DAILY_LOSS * 100:g}%\",\n"
     "                         equity=equity, start_equity=day[\"start_equity\"])\n"),

    ("crypto per-symbol",
     "            STRATEGY_STATUS[\"symbols\"][symbol] = info\n",
     "            STRATEGY_STATUS[\"symbols\"][symbol] = info\n"
     "            _log_crypto_decision(info)\n"),

    ("crypto error",
     "            notify.send(f\"[ALERT] Strategy error for {symbol}: {str(e)[:300]}\", key=f\"err:{symbol}\", cooldown=3600)\n",
     "            notify.send(f\"[ALERT] Strategy error for {symbol}: {str(e)[:300]}\", key=f\"err:{symbol}\", cooldown=3600)\n"
     "            decision_log.log_changed(f\"crypto:{symbol}\", (\"error\", str(e)[:120]), engine=\"crypto\", event=\"error\",\n"
     "                                     mode=_mode(), symbol=symbol, reason=str(e)[:300])\n"),

    ("us signals",
     "        return\n    holdings = _us_holdings(us)\n",
     "        return\n"
     "    decision_log.log(\"us\", \"signals\", mode=_us_mode(), decision=\",\".join(sig[\"selected\"]),\n"
     "                     reason=f\"signals as of {sig['signal_date']}\", month=mkey,\n"
     "                     scores=sig[\"scores\"], weights=sig[\"weights\"])\n"
     "    holdings = _us_holdings(us)\n"),

    ("us rebalance result",
     "    US_STATUS[\"note\"] = \"rebalanced\" if not failed else \"rebalance incomplete; will retry\"\n",
     "    decision_log.log(\"us\", \"rebalance\", mode=_us_mode(), decision=\"complete\" if not failed else \"incomplete\",\n"
     "                     reason=f\"{len(results)} order(s), {len(failed)} failed\", month=mkey,\n"
     "                     equity_before=round(equity_before, 2), equity_after=round(equity_after, 2),\n"
     "                     orders=[{k: r.get(k) for k in (\"symbol\", \"side\", \"state\", \"qty\", \"notional\", \"filled_qty\", \"avg_price\")}\n"
     "                             for r in results])\n"
     "    US_STATUS[\"note\"] = \"rebalanced\" if not failed else \"rebalance incomplete; will retry\"\n"),

    ("us status wrapper",
     "def us_cycle(fetch_daily=None, fetch_prices=None, market=None):\n    fetch_daily = fetch_daily or fetch_us_daily\n",
     "def us_cycle(fetch_daily=None, fetch_prices=None, market=None):\n"
     "    try:\n"
     "        return _us_cycle_inner(fetch_daily, fetch_prices, market)\n"
     "    finally:\n"
     "        decision_log.log_changed(\"us:status\", US_STATUS.get(\"note\"), engine=\"us\", event=\"status\",\n"
     "                                 mode=_us_mode(), reason=US_STATUS.get(\"note\"),\n"
     "                                 last_error=US_STATUS.get(\"last_error\"))\n"
     "\n"
     "\n"
     "def _us_cycle_inner(fetch_daily=None, fetch_prices=None, market=None):\n"
     "    fetch_daily = fetch_daily or fetch_us_daily\n"),
]


def main():
    apply = "--apply" in sys.argv
    src = ENGINE.read_text()
    if "import decision_log" in src:
        sys.exit("aether_engine.py already contains decision-log hooks; nothing to do.")
    ok = True
    for name, old, _ in EDITS:
        n = src.count(old)
        print(f"{'OK ' if n == 1 else 'BAD'} {name}: anchor found {n}x")
        ok &= n == 1
    if not ok:
        sys.exit("Not applied: every anchor must match exactly once.")
    if not apply:
        print("Dry run passed. Re-run with --apply to write the changes.")
        return
    for _, old, new in EDITS:
        src = src.replace(old, new, 1)
    ENGINE.write_text(src)
    print(f"Applied {len(EDITS)} edits to {ENGINE}.")


if __name__ == "__main__":
    main()
