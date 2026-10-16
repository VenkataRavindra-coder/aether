"""AETHER live-readiness check #5.1: credentials and account state.
READ-ONLY: places no orders, changes no settings. Run from ~ on the VM:
    /home/ratelark/ratelark/.venv/bin/python3 live_check.py
"""
import os
import sys

sys.path.insert(0, "/home/ratelark/ratelark")
for _env_path in ("/home/ratelark/ratelark/.env", "/home/ratelark/ratelark/.env.live"):
    if not os.path.exists(_env_path):   # .env.live is optional: live keys may still be in .env
        continue
    with open(_env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from alpaca.trading.client import TradingClient  # noqa: E402
from alpaca.trading.requests import GetOrdersRequest  # noqa: E402
from alpaca.trading.enums import QueryOrderStatus  # noqa: E402

results = []


def report(level, label, msg):
    results.append(level)
    print(f"{level:4} [{label}] {msg}")


def check_account(label, key_env, secret_env, paper):
    key, sec = os.getenv(key_env), os.getenv(secret_env)
    if not key or not sec:
        report("FAIL", label, f"{key_env} / {secret_env} not set in .env")
        return
    try:
        c = TradingClient(key, sec, paper=paper)
        a = c.get_account()
    except Exception as e:
        report("FAIL", label, f"cannot read account: {type(e).__name__}: {str(e)[:120]}")
        return
    num = str(a.account_number)
    report("PASS", label, f"connected to {'PAPER' if paper else 'LIVE'} account ...{num[-4:]}")
    status = str(getattr(a.status, "value", a.status))
    report("PASS" if status == "ACTIVE" else "FAIL", label, f"status {status}")
    report("FAIL" if a.trading_blocked else "PASS", label, f"trading_blocked={a.trading_blocked}")
    report("FAIL" if a.account_blocked else "PASS", label, f"account_blocked={a.account_blocked}")
    eq = float(a.equity or 0)
    if not paper:
        report("INFO" if eq == 0 else "PASS", label,
               f"equity ${eq:,.2f}" + (" (unfunded, expected until micro-live)" if eq == 0 else ""))
        shorting = getattr(a, "shorting_enabled", None)
        report("WARN" if shorting else "PASS", label,
               f"shorting_enabled={shorting}" + (" -> disable it as a backstop for the long-only engine" if shorting else ""))
    else:
        report("PASS", label, f"equity ${eq:,.2f}")
    try:
        pos = c.get_all_positions()
        opn = c.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))
        report("PASS", label, f"{len(pos)} positions, {len(opn)} open orders (read OK)")
    except Exception as e:
        report("FAIL", label, f"cannot read positions/orders: {str(e)[:120]}")


def check_not_swapped(label, key_env, secret_env, wrong_paper):
    """Keys must be rejected by the OTHER environment, proving they sit in the right slot."""
    key, sec = os.getenv(key_env), os.getenv(secret_env)
    if not key or not sec:
        return
    try:
        TradingClient(key, sec, paper=wrong_paper).get_account()
        report("FAIL", label, f"keys ALSO work on the {'paper' if wrong_paper else 'live'} endpoint: slots may be swapped")
    except Exception:
        report("PASS", label, f"keys rejected by the {'paper' if wrong_paper else 'live'} endpoint (correct slot)")


check_account("paper", "APCA_API_KEY_ID", "APCA_API_SECRET_KEY", paper=True)
check_account("live", "APCA_LIVE_API_KEY_ID", "APCA_LIVE_API_SECRET_KEY", paper=False)
check_not_swapped("paper", "APCA_API_KEY_ID", "APCA_API_SECRET_KEY", wrong_paper=False)
check_not_swapped("live", "APCA_LIVE_API_KEY_ID", "APCA_LIVE_API_SECRET_KEY", wrong_paper=True)

fails, warns = results.count("FAIL"), results.count("WARN")
print(f"\n{'READY' if not fails else 'NOT READY'}: {fails} fail, {warns} warn")
sys.exit(1 if fails else 0)
