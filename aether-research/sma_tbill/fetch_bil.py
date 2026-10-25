#!/usr/bin/env python3
"""One-time download of BIL daily bars for registration sma-tbill-v1 (prereg section 3). No returns are computed.

  python fetch_bil.py fetch      # Yahoo via yfinance, adjusted, 2007-05-30 .. 2026-10-06; refuses if the file exists

Writes data/bil_daily_yf.csv.gz in the same layout as the registered ETF file (symbol, date, open, close) and
data/bil_manifest.json with its SHA-256, row count and first/last date. Both are committed BEFORE the registration
tag is created; the runner refuses unless the file matches the manifest at the tag. Prints dates and counts only.

yfinance is research-only and not part of the engine's environment: run this with a separate virtual environment
(prereg section 9).
"""
from __future__ import annotations

import csv
import gzip
import hashlib
import json
import math
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
FILE = DATA / "bil_daily_yf.csv.gz"
MANIFEST = DATA / "bil_manifest.json"
SYMBOL = "BIL"
FIRST, LAST = date(2007, 5, 30), date(2026, 10, 6)      # BIL inception .. the registered ETF file's last day


class Refused(Exception):
    pass


def rows_from_frame(df) -> list[tuple[str, str, float, float]]:
    """(symbol, date, open, close) rows from a yfinance single-ticker frame; only FIRST..LAST; refuses bad prices."""
    cols = df.columns
    if hasattr(cols, "nlevels") and cols.nlevels > 1:
        df = df.xs(SYMBOL, axis=1, level=1) if SYMBOL in set(cols.get_level_values(1)) else df[SYMBOL]
    out = []
    for ts, r in df[["Open", "Close"]].iterrows():
        d = ts.date() if hasattr(ts, "date") else date.fromisoformat(str(ts)[:10])
        if not (FIRST <= d <= LAST):
            continue
        o, c = float(r["Open"]), float(r["Close"])
        if not (math.isfinite(o) and math.isfinite(c) and o > 0 and c > 0):
            raise Refused(f"bad price on {d}: open {o}, close {c}")
        out.append((SYMBOL, d.isoformat(), o, c))
    if not out:
        raise Refused("no BIL rows in the registered period")
    return out


def write(rows, path: Path = FILE, manifest: Path = MANIFEST, meta: dict | None = None) -> dict:
    if path.exists() or manifest.exists():
        raise Refused(f"{path.name} or its manifest already exists: one fetch only (prereg section 3)")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    # no file name and mtime 0 in the gzip header: the same rows always give the same bytes and hash
    with open(tmp, "wb") as raw, gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as gz:
        with _text(gz) as f:
            w = csv.writer(f)
            w.writerow(["symbol", "date", "open", "close"])
            w.writerows(rows)
    tmp.rename(path)
    h = hashlib.sha256(path.read_bytes()).hexdigest()
    info = {"file": path.name, "sha256": h, "rows": len(rows), "first": rows[0][1], "last": rows[-1][1],
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **(meta or {})}
    manifest.write_text(json.dumps(info, indent=1))
    return info


def _text(gz):
    import io
    return io.TextIOWrapper(gz, encoding="utf-8", newline="")


def fetch() -> dict:
    import yfinance as yf
    df = yf.download(SYMBOL, start=FIRST.isoformat(), end=(LAST + timedelta(days=1)).isoformat(),
                     auto_adjust=True, progress=False, threads=False)
    return write(rows_from_frame(df), meta={"source": "Yahoo via yfinance, auto_adjust=True",
                                            "yfinance": getattr(yf, "__version__", "?")})


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv != ["fetch"]:
        print(__doc__)
        return 2
    try:
        info = fetch()
    except Refused as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2
    print(f"saved {FILE}: {info['rows']} rows, {info['first']} .. {info['last']}, sha256 {info['sha256']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
