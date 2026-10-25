"""AETHER research registry: one read-only view of every strategy, its evidence and its operational health.

Reads FILES only: the curated registry (aether-research/registry/strategies.json), historical results JSON,
the forward journals and their last-run status files, the tactical paper ledger and the trial database.
It never imports executors, never calls a broker and never writes anything except the health snapshot that
research_health.py asks it to write. Missing or unreadable data is reported as such, never filled in.

Stages are kept apart on purpose:
- historical: backtests on past data (results files / recorded verdicts);
- forward:    rules replayed on new data as it arrives, no orders (shadow observation);
- paper:      orders on a paper account;
- live:       orders with real money.
"""
from __future__ import annotations

import json
import math
import sqlite3
import statistics
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parent
REGISTRY = REPO / "aether-research" / "registry" / "strategies.json"
STATUS_OUT = REPO / "aether-research" / "registry" / "status.json"
TRIAL_DB = REPO / "aether-research" / "research.sqlite"
PAPER_LEDGER = REPO / "tactical_exec_ledger.jsonl"
NY = ZoneInfo("America/New_York")

SLEEVE_USD = 5_000.0
TARGET_USD_PER_DAY = 2.0
RUN_GRACE = timedelta(minutes=20)        # a scheduled run must have finished this long after its start time
STATUSES = {"retired", "inconclusive", "collecting", "frozen_no_go", "candidate", "supported"}
STAGES = ("historical", "forward", "paper", "live")
GRADES = ("strong", "promising", "weak lead", "inconclusive", "rejected")   # best first
WEEKDAYS = {"Mon..Fri": {0, 1, 2, 3, 4}}


# ------------------------------------------------------------------ loading
def load_registry(path: Path = REGISTRY) -> dict:
    return json.loads(path.read_text())


def validate(reg: dict, repo: Path = REPO) -> list[str]:
    """Structural problems in the registry (empty list = fine). Used by tests and the health check."""
    errs, ids, trial_ids = [], set(), []
    for s in reg.get("strategies", []):
        sid = s.get("id")
        if not sid or sid in ids:
            errs.append(f"missing or duplicate id {sid!r}")
        ids.add(sid)
        if s.get("status") not in STATUSES:
            errs.append(f"{sid}: status {s.get('status')!r} not in {sorted(STATUSES)}")
        if s.get("stage") not in STAGES:
            errs.append(f"{sid}: stage {s.get('stage')!r} not in {STAGES}")
        if s["status"] == "retired" and not s.get("reasons"):
            errs.append(f"{sid}: retired without reasons")
        trial_ids += s.get("trials", {}).get("ids", [])
        for key in ("doc",):
            p = (s.get("registration") or {}).get(key)
            if p and not (repo / p).exists():
                errs.append(f"{sid}: registration {p} missing")
        h = s.get("historical") or {}
        if h.get("source") == "results_json" and not (repo / h["results_file"]).exists():
            errs.append(f"{sid}: results file {h['results_file']} missing")
        if s.get("live") is not None:
            errs.append(f"{sid}: a live record exists; live strategies are not part of this registry yet")
    if len(trial_ids) != len(set(trial_ids)):
        errs.append("a trial id is claimed by two strategies")
    return errs


def _read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None


def _jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


# ------------------------------------------------------------------ evidence per stage
def historical(entry: dict, repo: Path = REPO) -> dict | None:
    h = entry.get("historical")
    if not h:
        return None
    if h.get("source") == "results_json":
        doc = _read_json(repo / h["results_file"])
        if doc is None:
            return {"source": h["results_file"], "error": "results file not found"}
        v = doc["variants"][h["variant"]]
        if "cagr_pct" in v and "verdict" not in v:          # portfolio-style results (etf-breakout-v1)
            judged = bool(v.get("base"))
            crit = doc.get("criteria") or {}
            comp = doc.get("comparators") or {}
            return {"source": h["results_file"], "verdict": doc.get("verdict") if judged else "not judged (neighbour)",
                    "trades": v.get("trades"), "win_rate": v.get("win_rate"), "profit_factor": v.get("profit_factor"),
                    "max_drawdown_pct": v.get("max_drawdown_pct"), "sharpe": v.get("sharpe"), "cagr_pct": v.get("cagr_pct"),
                    "total_net_pct": v.get("total_pct"), "boot95_low": (v.get("sharpe_boot95") or [None])[0],
                    "dsr": (v.get("dsr") or {}).get("dsr"),
                    "buy_hold_sharpe": (comp.get("hold_same_etfs") or {}).get("sharpe"),
                    "vs_equal_weight_low": (v.get("sharpe_diff_vs_equal_weight_95") or [None])[0],
                    "benchmark": "hold {:.2f} / 200-day filter {:.2f} Sharpe".format(
                        (comp.get("hold_same_etfs") or {}).get("sharpe", float("nan")),
                        (comp.get("sma200_same_etfs") or {}).get("sharpe", float("nan"))),
                    "criteria_passed": sum(bool(x) for x in crit.values()) if judged else None,
                    "criteria_total": len(crit) if judged else None,
                    "usd_per_day_5k": ((doc.get("dollars") or {}).get("5000") or {}).get("usd_per_trading_day") if judged else None,
                    "n_trials": (doc.get("n_info") or {}).get("n")}
        rnd = v.get("random_entry") or {}
        bm = doc.get("benchmarks") or {}
        bh = v.get("buy_hold_sharpe")
        return {"source": h["results_file"], "verdict": v.get("verdict"), "trades": v.get("trades"),
                "win_rate": v.get("win_rate"), "mean_net_bp": v.get("mean_net_bp"),
                "avg_win_bp": v.get("avg_win_bp"), "avg_loss_bp": v.get("avg_loss_bp"),
                "profit_factor": v.get("profit_factor"), "max_drawdown_pct": v.get("max_drawdown_pct"),
                "sharpe": v.get("sharpe"), "total_net_pct": v.get("total_net_pct"),
                "buy_hold_sharpe": bh if bh is not None else bm.get("buy_hold_sharpe"),
                "boot95_low": (v.get("boot95") or [None])[0], "t_stat": v.get("t_stat"),
                "dsr": (v.get("dsr") or {}).get("dsr"), "random_p": rnd.get("p"),
                "half1_mean_bp": v.get("half1_mean_bp"), "half2_mean_bp": v.get("half2_mean_bp"),
                "criteria_passed": sum(bool(x) for x in (v.get("criteria") or {}).values()),
                "criteria_total": len(v.get("criteria") or {}),
                "usd_per_day_5k": v.get("usd_per_trading_day_on_sleeve"), "n_trials": (doc.get("n_info") or {}).get("n")}
    return {"source": "strategy.md", **(h.get("headline") or {})}


def _profit_factor(xs):
    g, l = sum(x for x in xs if x > 0), -sum(x for x in xs if x < 0)
    return (g / l) if l > 0 else (None if g == 0 else math.inf)


def _daily(points):
    """[(day, value)] -> sorted daily totals and their running sum."""
    by: dict[str, float] = {}
    for d, x in points:
        by[d] = by.get(d, 0.0) + x
    days = sorted(by)
    cum, out = 0.0, []
    for d in days:
        cum += by[d]
        out.append({"day": d, "value": round(by[d], 4), "cumulative": round(cum, 4)})
    return out


def forward(entry: dict, repo: Path = REPO) -> dict | None:
    f = entry.get("forward")
    if not f:
        return None
    rows = [r for r in _jsonl(repo / f["journal"]) if r.get("kind", "trade") == "trade" and r.get("variant") == f["variant"]]
    nets = [r["net_bp"] for r in rows]
    n = len(nets)
    usd = [(r["exit_t"][:10], SLEEVE_USD * r["net_bp"] / 1e4) for r in rows]
    return {"trades": n, "target_trades": f.get("target_trades"), "ends": f.get("ends"), "started": f.get("started"),
            "mean_net_bp": statistics.fmean(nets) if n else None,
            "win_rate": (sum(x > 0 for x in nets) / n) if n else None,
            "profit_factor": _profit_factor(nets) if n else None,
            "net_usd_5k": round(sum(x for _, x in usd), 2) if n else 0.0,
            "daily_usd_5k": _daily(usd), "note": "replayed rules on new data, no orders; dollars assume a $5,000 sleeve"}


def paper(entry: dict, ledger: Path = PAPER_LEDGER) -> dict | None:
    p = entry.get("paper")
    if not p:
        return None
    if p.get("source") != "tactical_exec_ledger":
        return {"source": p.get("source"), "note": p.get("note")}
    rows = [r for r in _jsonl(ledger) if r.get("strategy") == p["strategy"]]
    trades = [r for r in rows if r.get("trade")]
    nets = [r["trade"]["net_bp"] for r in trades]
    n = len(nets)
    return {"source": "tactical paper ledger", "symbol": p.get("symbol"), "account": p.get("account"),
            "sessions": len(rows), "trades": n, "target_trades": p.get("target_trades"),
            "mean_net_bp": statistics.fmean(nets) if n else None,
            "win_rate": (sum(x > 0 for x in nets) / n) if n else None,
            "profit_factor": _profit_factor(nets) if n else None,
            "net_usd": round(sum(r["trade"]["pnl_usd"] for r in trades), 2),
            "daily_usd": _daily([(r["day"], r["trade"]["pnl_usd"]) for r in trades]),
            "unscored": [r["day"] for r in rows if str(r.get("status", "")).startswith("unscored")]}


# ------------------------------------------------------------------ evidence quality (not return)
def grade(entry: dict, hist: dict | None, fwd: dict | None) -> tuple[str, str]:
    """(grade, why). Strongest evidence first; a high return alone never raises the grade."""
    verdict = ((hist or {}).get("verdict") or entry.get("verdict") or "").lower()
    if entry["status"] in ("retired", "frozen_no_go") or "unsupported" in verdict or "no-go" in verdict:
        return "rejected", "failed its registered criteria"
    supported = verdict == "historically supported"
    fwd_done = bool(fwd and fwd.get("target_trades") and fwd["trades"] >= fwd["target_trades"])
    if supported and fwd_done and (fwd.get("mean_net_bp") or 0) > 0:
        return "strong", "historical criteria passed and forward results consistent"
    if supported:
        return "promising", "historical criteria passed; forward evidence still collecting"
    h = hist or {}
    if (h.get("vs_equal_weight_low") or 0) > 0 and (h.get("boot95_low") or 0) > 0:
        return "weak lead", "beats its own comparator by more than noise, but criteria not met"
    if ((h.get("mean_net_bp") or 0) > 0 and h.get("random_p") is not None and h["random_p"] <= 0.05
            and (h.get("half1_mean_bp") or 0) > 0 and (h.get("half2_mean_bp") or 0) > 0):
        return "weak lead", "positive in both halves and better than random entries, but criteria not met"
    return "inconclusive", "not enough evidence either way"


def rank_key(item: dict):
    h = item.get("historical") or {}
    dsr = h.get("dsr")
    low = h.get("boot95_low")
    return (GRADES.index(item["grade"]), -(dsr if isinstance(dsr, (int, float)) else -1.0),
            -(low if isinstance(low, (int, float)) else -1e9))


# ------------------------------------------------------------------ operational health
def last_scheduled(now: datetime, schedule: dict) -> datetime | None:
    """Most recent scheduled start at or before now - RUN_GRACE (New York wall clock, DST-safe)."""
    tz = ZoneInfo(schedule.get("tz", "America/New_York"))
    days = WEEKDAYS[schedule.get("days", "Mon..Fri")]
    hh, mm = map(int, schedule["time"].split(":"))
    ref = (now - RUN_GRACE).astimezone(tz)
    for back in range(0, 10):
        d = ref.date() - timedelta(days=back)
        t = datetime.combine(d, time(hh, mm), tzinfo=tz)
        if d.weekday() in days and t <= ref:
            return t
    return None


def next_scheduled(now: datetime, schedule: dict) -> datetime:
    tz = ZoneInfo(schedule.get("tz", "America/New_York"))
    days = WEEKDAYS[schedule.get("days", "Mon..Fri")]
    hh, mm = map(int, schedule["time"].split(":"))
    ref = now.astimezone(tz)
    for ahead in range(0, 10):
        d = ref.date() + timedelta(days=ahead)
        t = datetime.combine(d, time(hh, mm), tzinfo=tz)
        if d.weekday() in days and t > ref:
            return t
    raise RuntimeError("no scheduled time in the next 10 days")


def forward_health(f: dict, now: datetime, repo: Path = REPO) -> dict:
    """ok / warning / error / not started, with plain-English problems. Never raises."""
    out = {"unit": f.get("unit"), "problems": [], "state": "ok", "last_run": None,
           "next_run": next_scheduled(now, f["schedule"]).isoformat(timespec="minutes")}
    try:
        st = _read_json(repo / f["status_file"])
        expected = last_scheduled(now, f["schedule"])
        started = datetime.combine(date.fromisoformat(f["started"]), time(0), tzinfo=NY)
        if st is None:
            if expected is None or expected < started:
                out["state"] = "not started"
                return out
            out["problems"].append("forward journal has never recorded a run")
        else:
            out["last_run"] = st.get("finished_at") or st.get("started_at")
            out["latest_session"] = st.get("latest_session")
            if not st.get("ok"):
                out["problems"].append(f"last run failed: {st.get('error') or 'unknown error'}")
            fin = datetime.fromisoformat(st["finished_at"]) if st.get("finished_at") else None
            if expected is not None and expected >= started and (fin is None or fin < expected):
                out["problems"].append(f"skipped run: nothing finished since {expected:%a %d %b %H:%M} New York")
            if st.get("failed_forward_sessions"):
                out["problems"].append("missing data for " + ", ".join(st["failed_forward_sessions"]))
            if st.get("duplicates"):
                out["problems"].append(f"{st['duplicates']} duplicate journal rows")
            mm = (st.get("summary") or {}).get("_mismatches") or st.get("mismatches_new")
            if mm:
                out["problems"].append(f"{mm} replay mismatches (determinism check failed)")
        if out["problems"]:
            out["state"] = "error"
    except Exception as e:
        out["state"] = "error"
        out["problems"].append(f"health check could not read the status: {type(e).__name__}: {str(e)[:160]}")
    return out


def trial_check(reg: dict, db: Path = TRIAL_DB) -> dict:
    """Registry trial ids vs the trial database, so N cannot drift silently."""
    claimed = sorted(i for s in reg["strategies"] for i in s.get("trials", {}).get("ids", []))
    acc = reg.get("trial_accounting", {})
    out = {"registry_logged": len(claimed), "backfilled": acc.get("backfilled"), "outside_db": acc.get("outside_db"),
           "n": len(claimed) + (acc.get("backfilled") or 0) + (acc.get("outside_db") or 0)}
    if not db.exists():
        out["db"] = None
        out["problem"] = None
        return out
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
    try:
        ids = sorted(r[0] for r in con.execute("SELECT id FROM research_trials"))
        try:
            back = int(con.execute("SELECT COALESCE(SUM(count), 0) FROM untracked_trials").fetchone()[0])
        except sqlite3.OperationalError:
            back = None
    finally:
        con.close()
    out.update(db=len(ids), db_backfilled=back)
    missing = sorted(set(ids) - set(claimed))
    phantom = sorted(set(claimed) - set(ids))
    out["problem"] = None
    if missing or phantom:
        out["problem"] = (f"trial database ids not in the registry: {missing}" if missing else "") + \
                         (f"; registry ids not in the database: {phantom}" if phantom else "")
    return out


# ------------------------------------------------------------------ the whole picture
def build(now: datetime | None = None, repo: Path = REPO, registry: Path | None = None,
          ledger: Path | None = None, db: Path | None = None) -> dict:
    now = now or datetime.now(NY)
    out = {"ok": True, "generated_at": now.astimezone(NY).isoformat(timespec="seconds"), "errors": []}
    try:
        reg = load_registry(registry or (repo / "aether-research" / "registry" / "strategies.json"))
    except Exception as e:
        return {"ok": False, "generated_at": out["generated_at"], "errors": [f"registry unreadable: {type(e).__name__}: {e}"]}
    out["registry_problems"] = validate(reg, repo)
    items, health = [], {}
    for s in reg["strategies"]:
        item = {k: s.get(k) for k in ("id", "name", "family", "version", "markets", "timeframe", "status", "stage",
                                      "verdict", "reasons", "next_evaluation", "registered", "registration", "trials",
                                      "params", "data")}
        for key, fn in (("historical", lambda: historical(s, repo)), ("forward", lambda: forward(s, repo)),
                        ("paper", lambda: paper(s, ledger or (repo / "tactical_exec_ledger.jsonl")))):
            try:
                item[key] = fn()
            except Exception as e:
                item[key] = {"error": f"{type(e).__name__}: {str(e)[:160]}"}
                out["errors"].append(f"{s['id']} {key}: {type(e).__name__}")
        item["live"] = None
        item["grade"], item["grade_why"] = grade(s, item["historical"], item["forward"])
        f = s.get("forward")
        if f:
            key = f.get("unit") or f["status_file"]
            if key not in health:
                health[key] = forward_health(f, now, repo)
            item["forward_health"] = health[key]["state"]
        p = item.get("paper") or {}
        if p.get("unscored"):
            out["errors"].append(f"{s['id']} paper: unscored days {p['unscored']}")
        items.append(item)
    items.sort(key=rank_key)
    out["strategies"] = items
    out["forward_jobs"] = health
    try:
        out["trials"] = trial_check(reg, db or (repo / "aether-research" / "research.sqlite"))
    except Exception as e:
        out["trials"] = {"problem": f"trial database unreadable: {type(e).__name__}"}
    counts: dict[str, int] = {}
    for it in items:
        counts[it["grade"]] = counts.get(it["grade"], 0) + 1
    paper_days = {}
    for it in items:
        for d in ((it.get("paper") or {}).get("daily_usd") or []):
            paper_days[d["day"]] = paper_days.get(d["day"], 0.0) + d["value"]
    out["summary"] = {
        "strategies": len(items), "by_grade": counts,
        "best": next(({"id": it["id"], "name": it["name"], "grade": it["grade"]} for it in items), None),
        "target_usd_per_day": TARGET_USD_PER_DAY,
        "live_usd_per_day": None, "live_note": "no live strategy (Oct 22 decision); live account holds VTI by hand",
        "paper_days": len(paper_days),
        "paper_usd_per_day": round(sum(paper_days.values()) / len(paper_days), 2) if paper_days else None,
        "problems": sum(len(h["problems"]) for h in health.values()) + len(out["registry_problems"])
                    + (1 if (out.get("trials") or {}).get("problem") else 0) + len(out["errors"]),
    }
    return out


def problems(status: dict) -> list[str]:
    """Every problem in a build() result, as alert lines."""
    out = list(status.get("registry_problems") or [])
    for unit, h in (status.get("forward_jobs") or {}).items():
        out += [f"{unit}: {p}" for p in h.get("problems", [])]
    t = status.get("trials") or {}
    if t.get("problem"):
        out.append(t["problem"])
    out += status.get("errors") or []
    return out
