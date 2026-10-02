import os
import sys
import json
import tempfile
import importlib.util
from datetime import datetime
from zoneinfo import ZoneInfo

# Force every execution mode OFF before module import.
os.environ["LIVE_TRADING"] = "false"
os.environ["US_LIVE"] = "false"
os.environ["US_EXECUTE"] = "false"
os.environ["US_ENABLED"] = "false"
os.environ["STRATEGY_ENABLED"] = "false"

# Prevent real notification credentials from being used.
os.environ["TELEGRAM_BOT_TOKEN"] = ""
os.environ["TELEGRAM_CHAT_ID"] = ""

PATH = "/home/azureuser/aether_engine.py"

spec = importlib.util.spec_from_file_location(
    "aether_engine_uslive_candidate", PATH
)
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)

passes = []
failures = []

def check(name, condition):
    if condition:
        passes.append(name)
        print("PASS:", name)
    else:
        failures.append(name)
        print("FAIL:", name)

# Silence external side effects.
m.notify.send = lambda *a, **kw: None

m.decision_log.log = lambda *a, **kw: None
m.decision_log.log_changed = lambda *a, **kw: None


# ==================================================
# TEST 1 — DEFAULT STATE SEPARATION
# ==================================================

s = m.default_state()

check("default contains us_paper", "us_paper" in s)
check("default contains us_live", "us_live" in s)
check("default no shared object",
      s["us_paper"] is not s["us_live"])


# ==================================================
# TEST 2 — LEGACY 'us' MIGRATES TO PAPER ONLY
# ==================================================

with tempfile.TemporaryDirectory() as td:
    m.STATE_FILE = os.path.join(td, "state.json")

    legacy = {
        "us": {
            "cash": 0.02,
            "holdings": {
                "DBC": 1.0,
                "EEM": 2.0,
                "IWM": 3.0,
            },
            "last_rebalance_month": "2026-09",
            "last_rebalance_at": "2026-09-21 19:34:00 UTC",
            "signal_date": "2026-08-31",
            "targets": {
                "DBC": 1/3,
                "EEM": 1/3,
                "IWM": 1/3,
            },
            "scores": {},
            "selected": ["DBC", "EEM", "IWM"],
            "history": [{"month": "2026-09"}],
            "equity_history": [{"d": "2026-09-25"}],
            "last_snapshot_date": "2026-09-25",
            "last_prices": {},
        }
    }

    with open(m.STATE_FILE, "w") as f:
        json.dump(legacy, f)

    loaded = m.load_state()

    check(
        "legacy holdings migrate to paper",
        loaded["us_paper"]["holdings"] ==
        legacy["us"]["holdings"],
    )

    check(
        "legacy rebalance month preserved",
        loaded["us_paper"]["last_rebalance_month"] ==
        "2026-09",
    )

    check(
        "live holdings start empty",
        loaded["us_live"]["holdings"] == {},
    )

    check(
        "live cash starts None",
        loaded["us_live"]["cash"] is None,
    )

    check(
        "live history starts empty",
        loaded["us_live"]["history"] == [],
    )


# ==================================================
# TEST 3 — MODE ROUTING
# ==================================================

state = m.default_state()
state["us_paper"]["cash"] = 5000
state["us_live"]["cash"] = 100

m.US_LIVE = False

check(
    "US_LIVE false selects paper",
    m._us_state(state)["cash"] == 5000,
)

m.US_LIVE = True

check(
    "US_LIVE true selects live",
    m._us_state(state)["cash"] == 100,
)

m._us_state(state)["cash"] = 75

check(
    "live mutation does not alter paper",
    state["us_paper"]["cash"] == 5000
    and state["us_live"]["cash"] == 75,
)


# ==================================================
# TEST 4 — LIVE CAP REJECTS BUY > $100
# ==================================================

class FakeBroker:
    client = object()

    def get_account_balance(self):
        return {
            "source": "live",
            "portfolio_value": 1000.0,
            "cash": 1000.0,
        }

    def account_id(self):
        return "TEST-LIVE"

m.us_broker = FakeBroker()
m.US_LIVE = True
m.US_EXECUTE = True
m.US_REQUIRE_APPROVAL = True
m.US_LIVE_MAX_USD = 100.0

with tempfile.TemporaryDirectory() as td:
    m.STATE_FILE = os.path.join(td, "state.json")
    m.save_state(m.default_state())

    orders = [{
        "symbol": "SPY",
        "side": "BUY",
        "notional": 120.0,
    }]

    sig = {
        "weights": {"SPY": 1.0},
    }

    now = datetime(
        2026, 9, 28, 15, 45,
        tzinfo=ZoneInfo("America/New_York"),
    )

    result = m._us_live_gate(
        orders,
        sig,
        "2026-09",
        "2026-09-28",
        now,
        100.0,
    )

    check(
        "buy above $100 cap blocked",
        result is False
        and "dollar cap" in m.US_STATUS["note"].lower(),
    )


# ==================================================
# TEST 5 — ACCOUNT > CAP DOES NOT AUTOMATICALLY FAIL
# ==================================================

with tempfile.TemporaryDirectory() as td:
    m.STATE_FILE = os.path.join(td, "state.json")
    m.save_state(m.default_state())

    orders = [{
        "symbol": "SPY",
        "side": "BUY",
        "notional": 80.0,
    }]

    sig = {
        "weights": {"SPY": 1.0},
    }

    now = datetime(
        2026, 9, 28, 15, 45,
        tzinfo=ZoneInfo("America/New_York"),
    )

    result = m._us_live_gate(
        orders,
        sig,
        "2026-09",
        "2026-09-28",
        now,
        80.0,
    )

    check(
        "large broker account allowed but proposal required",
        result is False
        and "approval" in m.US_STATUS["note"].lower(),
    )


# ==================================================
# TEST 6 — NO APPROVAL = NO EXECUTION
# ==================================================

with tempfile.TemporaryDirectory() as td:
    m.STATE_FILE = os.path.join(td, "state.json")
    m.save_state(m.default_state())

    orders = [{
        "symbol": "SPY",
        "side": "BUY",
        "notional": 50.0,
    }]

    sig = {
        "weights": {"SPY": 1.0},
    }

    now = datetime(
        2026, 9, 28, 15, 45,
        tzinfo=ZoneInfo("America/New_York"),
    )

    result = m._us_live_gate(
        orders,
        sig,
        "2026-09",
        "2026-09-28",
        now,
        50.0,
    )

    st = m.load_state()["us_live"]
    proposal = st.get("live_proposal")

    check(
        "proposal created instead of immediate order",
        result is False
        and proposal is not None
        and proposal["status"] == "PENDING",
    )

    pid = proposal["id"]

    response = m._us_live_decide(
        f"/approve_{pid}"
    )

    check(
        "Telegram approval accepted",
        "APPROVED" in response,
    )

    result2 = m._us_live_gate(
        orders,
        sig,
        "2026-09",
        "2026-09-28",
        now,
        50.0,
    )

    check(
        "approved unchanged plan may execute",
        result2 is True,
    )

    st2 = m.load_state()["us_live"]

    check(
        "approval becomes USED",
        st2["live_proposal"]["status"] == "USED",
    )


# ==================================================
# TEST 7 — CHANGED PLAN CANNOT USE OLD APPROVAL
# ==================================================

with tempfile.TemporaryDirectory() as td:
    m.STATE_FILE = os.path.join(td, "state.json")
    m.save_state(m.default_state())

    approved_orders = [{
        "symbol": "SPY",
        "side": "BUY",
        "notional": 40.0,
    }]

    changed_orders = [{
        "symbol": "QQQ",
        "side": "BUY",
        "notional": 40.0,
    }]

    sig1 = {"weights": {"SPY": 1.0}}
    sig2 = {"weights": {"QQQ": 1.0}}

    now = datetime(
        2026, 9, 28, 15, 45,
        tzinfo=ZoneInfo("America/New_York"),
    )

    m._us_live_gate(
        approved_orders,
        sig1,
        "2026-09",
        "2026-09-28",
        now,
        40.0,
    )

    p = m.load_state()["us_live"]["live_proposal"]
    m._us_live_decide(f"/approve_{p['id']}")

    changed_result = m._us_live_gate(
        changed_orders,
        sig2,
        "2026-09",
        "2026-09-28",
        now,
        40.0,
    )

    latest = m.load_state()["us_live"]["live_proposal"]

    check(
        "changed plan invalidates old approval",
        changed_result is False
        and latest["status"] == "PENDING"
        and latest["id"] != p["id"],
    )


# ==================================================
# TEST 8 — STATIC SAFETY INVARIANTS
# ==================================================

source = open(PATH).read()

check(
    "pre-existing holdings protection applies to live",
    'if us["cash"] is None and holdings:' in source
    and 'if (not US_LIVE) and us["cash"] is None and holdings:'
        not in source,
)

check(
    "open orders block rebalance",
    'open_syms is None or (open_syms & set(us_strategy.UNIVERSE))'
        in source,
)

check(
    "unreconciled orders freeze engine",
    'u["frozen"] = f"unreconciled order(s)' in source,
)

check(
    "live never falls back to paper $5000 allocation",
    'if US_LIVE:' in source
    and 'else:\n        cash = us["cash"] if us["cash"] is not None else US_ALLOCATION_USD'
        in source,
)

check(
    "live authority references hard cap",
    "US_LIVE_MAX_USD" in source
    and "strategy_available" in source,
)

# ==================================================
# SUMMARY
# ==================================================

print()
print("=" * 50)
print("TOTAL PASS:", len(passes))
print("TOTAL FAIL:", len(failures))

if failures:
    print("FAILED TESTS:")
    for x in failures:
        print(" -", x)
    raise SystemExit(1)

print("ALL OFFLINE US-LIVE REGRESSION TESTS PASSED")
