"""fetch_0945.py coverage and manifest (offline: no network, temp files only)."""
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "aether-research" / "us_momentum"))
import fetch_0945 as f  # noqa: E402

CAL = ["2020-07-27", "2020-07-28", "2020-07-29", "2020-07-30"]


def _rows(missing=()):
    out = []
    for s in f.SYMBOLS:
        for d in CAL:
            first = "2020-07-28" if s == "EFA" else "2020-07-27"
            if d < first:
                continue
            out.append((s, d, "daily", 1.0))
            if (s, d) in missing:
                out.append((s, d, "09:55", 1.0))
            else:
                out.append((s, d, "09:45", 1.0))
    return out


def test_overlap_starts_when_every_symbol_has_a_0945_bar():
    cov = f.coverage(_rows(), CAL)
    assert cov["overlap_start"] == "2020-07-28" and cov["sessions"] == 3
    assert all(v["coverage_0945"] == 1.0 for v in cov["symbols"].values())


def test_substitutes_are_counted_by_kind_and_never_priced():
    cov = f.coverage(_rows(missing={("TLT", "2020-07-29")}), CAL)
    t = cov["symbols"]["TLT"]
    assert t["n_0955"] == 1 and t["coverage_0945"] == pytest.approx(2 / 3) and t["n_none"] == 0


def test_a_symbol_without_any_0945_bar_refuses():
    rows = [r for r in _rows() if not (r[0] == "SHY" and r[2] == "09:45")]
    with pytest.raises(f.Refused, match="SHY"):
        f.coverage(rows, CAL)


def test_manifest_lines_the_runner_reads(tmp_path):
    import daily_candidates as dc
    cov = f.coverage(_rows(), CAL)
    m = tmp_path / "m.md"
    out = f.REPO / "aether-research" / "us_momentum" / "data" / "x.csv.gz"
    f.write_manifest(cov, "b" * 64, m, out)
    assert dc.read_manifest(m) == {"sha256": "b" * 64, "overlap_start": "2020-07-28"}


def test_fetch_refuses_a_second_time(tmp_path):
    p = tmp_path / "exists.csv.gz"
    p.write_text("x")
    with pytest.raises(f.Refused, match="one fetch only"):
        f.fetch(out=p, manifest=tmp_path / "m.md")
