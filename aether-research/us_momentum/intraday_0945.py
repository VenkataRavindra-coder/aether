#!/usr/bin/env python3
"""Read-only Alpaca 09:45 ET IEX bar fetch and fill selection helpers."""
from __future__ import annotations
import datetime as dt, hashlib, json, os, sys, time
from pathlib import Path
from zoneinfo import ZoneInfo
import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
DAILY_FILE = HERE / "data" / "us_daily_yf.csv.gz"
DATA_FILE = HERE / "data" / "us_0945_5m.csv.gz"
MANIFEST_FILE = REPO / "docs" / "INTRADAY_0945_MANIFEST.json"
SYMBOLS = ("SPY", "QQQ", "IWM", "EFA", "TLT", "GLD", "IEF", "SHY")
SLOTS = ("09:45", "09:50", "09:55")
NY, UTC = ZoneInfo("America/New_York"), dt.timezone.utc

class IntradayDataError(RuntimeError): pass

def sha256_file(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""): h.update(chunk)
    return h.hexdigest()

def normalize_bars(bars):
    need = {"symbol", "timestamp", "open"}
    if not need <= set(bars.columns): raise IntradayDataError(f"bar columns missing: {sorted(need-set(bars.columns))}")
    if bars.empty: return pd.DataFrame(columns=["symbol","date","slot","open"])
    x = bars[["symbol","timestamp","open"]].copy()
    x["symbol"] = x["symbol"].astype(str)
    x["timestamp"] = pd.to_datetime(x["timestamp"], utc=True, errors="coerce")
    if x.timestamp.isna().any(): raise IntradayDataError("invalid bar timestamp")
    local = x.timestamp.dt.tz_convert(NY)
    x["date"], x["slot"] = local.dt.strftime("%Y-%m-%d"), local.dt.strftime("%H:%M")
    x = x[x.slot.isin(SLOTS)].copy()
    x["open"] = pd.to_numeric(x.open, errors="coerce")
    if not np.isfinite(x.open.to_numpy(float)).all() or (x.open <= 0).any():
        raise IntradayDataError("non-positive or non-finite intraday open")
    if x.duplicated(["symbol","date","slot"]).any(): raise IntradayDataError("duplicate symbol/date/slot")
    return x[["symbol","date","slot","open"]].sort_values(["symbol","date","slot"]).reset_index(drop=True)

def select_fills(bars, daily_opens, symbols=SYMBOLS):
    """Select 09:45, then 09:50, 09:55, then the same-session daily open; never forward fill."""
    syms = tuple(symbols)
    if not set(syms) <= set(daily_opens.columns): raise IntradayDataError("daily opens missing required symbol")
    idx = pd.DatetimeIndex(pd.to_datetime(daily_opens.index)).tz_localize(None).normalize()
    if idx.has_duplicates or not idx.is_monotonic_increasing: raise IntradayDataError("calendar must be unique and sorted")
    daily = daily_opens.copy(); daily.index = idx
    norm = normalize_bars(bars); norm = norm[norm.symbol.isin(syms)]
    slots = {}
    for slot in SLOTS:
        p = norm[norm.slot == slot].pivot(index="date", columns="symbol", values="open")
        p.index = pd.to_datetime(p.index); slots[slot] = p.reindex(index=idx, columns=syms)
    first = []
    for s in syms:
        valid = slots["09:45"][s].dropna()
        if valid.empty: raise IntradayDataError(f"no 09:45 bars for {s}; overlap unknown")
        first.append(valid.index[0])
    overlap = max(first)
    prices = pd.DataFrame(index=idx, columns=syms, dtype=float)
    sources = pd.DataFrame(index=idx, columns=syms, dtype=object)
    for s in syms:
        left = pd.Series(True, index=idx)
        for slot in SLOTS:
            v = slots[slot][s]; use = left & v.notna()
            prices.loc[use,s], sources.loc[use,s] = v.loc[use].astype(float), slot
            left &= ~use
        v = pd.to_numeric(daily[s], errors="coerce"); use = left & v.notna()
        prices.loc[use,s], sources.loc[use,s] = v.loc[use].astype(float), "daily_open"
        left &= ~use; sources.loc[left,s] = "missing"
    cal = idx[idx >= overlap]; coverage = {}
    for s in syms:
        src = sources.loc[cal,s]; n = len(cal); n45 = int(slots["09:45"].loc[cal,s].notna().sum())
        coverage[s] = {"sessions":n,"on_time_0945":n45,"coverage_pct":round(100*n45/n,4) if n else 0.0,
                       "fallback_0950":int((src=="09:50").sum()),"fallback_0955":int((src=="09:55").sum()),
                       "fallback_daily_open":int((src=="daily_open").sum()),"missing":int((src=="missing").sum())}
    return prices, sources, {"overlap_start":overlap.strftime("%Y-%m-%d"),"symbols":list(syms),"coverage":coverage}

def fetch_bars(client, calendar, symbols=SYMBOLS, sleep_seconds=0.36):
    """Fetch only the 09:45-10:00 ET five-minute window for each locked daily-calendar session."""
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    rows=[]
    for d in pd.DatetimeIndex(calendar):
        day=d.date()
        start=dt.datetime.combine(day,dt.time(9,45),NY).astimezone(UTC)
        end=dt.datetime.combine(day,dt.time(10,0),NY).astimezone(UTC)
        req=StockBarsRequest(symbol_or_symbols=list(symbols),timeframe=TimeFrame(5,TimeFrameUnit.Minute),
                             start=start,end=end,adjustment=Adjustment.ALL,feed=DataFeed.IEX,limit=10000)
        frame=client.get_stock_bars(req).df
        if not frame.empty:
            frame=frame.reset_index(); tc="timestamp" if "timestamp" in frame.columns else "index"
            if tc not in frame: raise IntradayDataError("provider response has no timestamp")
            for r in frame.itertuples(index=False):
                stamp=pd.Timestamp(getattr(r,tc))
                if stamp.tzinfo is None: stamp=stamp.tz_localize(UTC)
                if stamp.tz_convert(NY).strftime("%H:%M") in SLOTS:
                    rows.append({"symbol":str(r.symbol),"timestamp":stamp.isoformat(),"open":float(r.open)})
        time.sleep(sleep_seconds)
    raw = pd.DataFrame(rows,columns=["symbol","timestamp","open"])
    normalize_bars(raw)  # validate while keeping timestamps for replay
    return raw

def daily_calendar(path=DAILY_FILE):
    df=pd.read_csv(path); op=df.pivot(index="date",columns="symbol",values="open").sort_index()
    cl=df.pivot(index="date",columns="symbol",values="close").sort_index()
    idx=pd.to_datetime(cl.index[cl["SPY"].notna()]); idx=idx[idx<=pd.Timestamp("2026-10-06")]
    return op.reindex(idx),cl.reindex(idx)

def fetch_and_write():
    """Requires inventory first; prints coverage only and writes a fingerprinted manifest."""
    sys.path.insert(0,str(HERE.parent)); import daily_candidates as dc
    dc.read_inventory()
    daily_sha=dc.check_data(DAILY_FILE,dc.DATA_SHA256); op,cl=daily_calendar()
    key,sec=os.getenv("APCA_API_KEY_ID"),os.getenv("APCA_API_SECRET_KEY")
    if not key or not sec: raise IntradayDataError("Alpaca market-data credentials missing from environment")
    from alpaca.data.historical import StockHistoricalDataClient
    bars=fetch_bars(StockHistoricalDataClient(key,sec),cl.index); _,_,report=select_fills(bars,op)
    DATA_FILE.parent.mkdir(parents=True,exist_ok=True); tmp=DATA_FILE.with_suffix(".csv.gz.tmp")
    bars.to_csv(tmp,index=False,compression="gzip"); tmp.replace(DATA_FILE)
    manifest={"schema":1,"provider":"Alpaca historical stock bars","feed":"iex","timeframe":"5Min",
              "adjustment":"all","request_window_et":["09:45","10:00"],"fallback_slots_et":list(SLOTS),
              "symbols":list(SYMBOLS),"daily_data_sha256":daily_sha,"intraday_data_sha256":sha256_file(DATA_FILE),
              "first_daily_session":cl.index.min().strftime("%Y-%m-%d"),
              "last_daily_session":cl.index.max().strftime("%Y-%m-%d"),**report}
    MANIFEST_FILE.parent.mkdir(parents=True,exist_ok=True); tmp=MANIFEST_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest,indent=2)+"\n"); tmp.replace(MANIFEST_FILE)
    print(json.dumps({"manifest":str(MANIFEST_FILE),"intraday_sha256":manifest["intraday_data_sha256"],
                      "overlap_start":manifest["overlap_start"],"coverage":manifest["coverage"]},indent=2))

def load_fills(data_path=DATA_FILE,manifest_path=MANIFEST_FILE,daily_opens=None):
    if not Path(data_path).exists() or not Path(manifest_path).exists(): raise IntradayDataError("09:45 data/manifest missing")
    m=json.loads(Path(manifest_path).read_text())
    if not (m.get("schema")==1 and m.get("feed")=="iex" and m.get("timeframe")=="5Min" and m.get("adjustment")=="all"
            and m.get("symbols")==list(SYMBOLS) and m.get("request_window_et")==["09:45","10:00"]):
        raise IntradayDataError("manifest parameters differ from registration")
    if sha256_file(data_path)!=m.get("intraday_data_sha256"): raise IntradayDataError("data fingerprint differs from manifest")
    if daily_opens is None: daily_opens,_=daily_calendar()
    bars=pd.read_csv(data_path); prices,sources,report=select_fills(bars,daily_opens)
    if report!={k:m[k] for k in ("overlap_start","symbols","coverage")}:
        raise IntradayDataError("coverage differs from manifest")
    return prices,sources,m

if __name__=="__main__":
    try: fetch_and_write()
    except Exception as exc:
        print(f"REFUSED: {type(exc).__name__}: {exc}",file=sys.stderr); raise SystemExit(2)

