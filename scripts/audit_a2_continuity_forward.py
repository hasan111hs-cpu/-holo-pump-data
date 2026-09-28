#!/usr/bin/env python3
"""
A-2 Step 2d -- INDEPENDENT FORWARD continuity verification, plus the PUMPUSDT
July 2025 seam forensic.

PURPOSE 1 -- INDEPENDENT FORWARD SCAN
-------------------------------------
In-window continuity (2025-07-01 .. 2026-08-31) currently rests ENTIRELY on the
original Step 2 implementation -- the script later proven to have a coverage
defect in its backward path. Step 2b repaired only the backward direction and
never re-scanned the window forward. This script re-derives in-window continuity
with an independent implementation and reports any disagreement with Step 2.

Critically, it MEASURES the first observed daily bar in the window for every
symbol. It never defaults to the window start. Step 2's
first_in_window_observation is read only for COMPARISON and is never trusted as
an input.

PURPOSE 2 -- THE PUMPUSDT SEAM
------------------------------
Primary Binance announcements establish that PUMPUSDT carried TWO unrelated
underlyings:

  epoch 1: launched 2025-04-12 14:30 UTC, underlying PumpBTC
           (Babylon BTC liquid staking; announced with STOUSDT and FHEUSDT)
  epoch 2: launched 2025-07-10 07:30 UTC, underlying Pump.fun (PUMP)

Step 2b established an absence 2025-06-14 .. 2025-06-30 and an effective start
of 2025-07-01 -- nine days BEFORE the Pump.fun contract existed. So either

  (a) no bar exists on 2025-07-01 and Step 2's field was a default, making the
      true effective start 2025-07-10 (age 53 days at the 2025-09-01
      formation, BELOW the 60-day gate), or
  (b) bars do exist 2025-07-01 .. 2025-07-09, in which case the run beginning
      2025-07-01 contains a SEAMLESS reassignment at 2025-07-10 that the frozen
      >= 7-day rule cannot detect by construction. The effective date would then
      be mechanically correct under the frozen rule and economically wrong by
      nine days.

This script dumps PUMPUSDT daily bars 2025-06-25 .. 2025-07-20 WITH CLOSE
PRICES. PumpBTC and Pump.fun trade at radically different price levels, so a
price discontinuity at the seam distinguishes (a) from (b) directly.

NOTHING IS TUNED. GAP_THRESHOLD_DAYS = 7, window unchanged, age gate untouched.
A missing monthly archive is INDETERMINATE and is NEVER counted as absence of
trading. universe.json is read-only and its digest is asserted unchanged.
"""

from __future__ import annotations

import hashlib
import io
import json
import pathlib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import date, timedelta

# ---------------------------------------------------------------------------
# FROZEN PARAMETERS -- IDENTICAL TO STEPS 2 AND 2b. DO NOT TUNE.
# ---------------------------------------------------------------------------
GAP_THRESHOLD_DAYS = 7
WINDOW_START = date(2025, 7, 1)
WINDOW_END = date(2026, 8, 31)

EXPECTED_EXAM_SHA256 = "8239751d74dc4d758252e0764e8a6f3d4f43b25bc4f35761050b7a146425d685"
EXPECTED_UNIVERSE_SHA256 = "ea79bbdeff91bdc3918c523bf248e2c2fe88aa34ec161f2633d0b1940c75c12a"

FORENSIC_SYMBOL = "PUMPUSDT"
FORENSIC_FROM = date(2025, 6, 25)
FORENSIC_TO = date(2025, 7, 20)

BASE = "https://data.binance.vision/data/futures/um/monthly/klines"
REPO = pathlib.Path(__file__).resolve().parents[1]
OUTDIR = REPO / "research" / "multicoin_expansion"
PREV_AUDIT = OUTDIR / "a2_continuity_audit.json"
BACKWARD = OUTDIR / "a2_continuity_audit_backward.json"
UNIVERSE = OUTDIR / "universe.json"
OUTFILE = OUTDIR / "a2_continuity_audit_forward.json"

MAX_RETRIES = 4
RETRY_SLEEP = 3.0


def sha256_file(p: pathlib.Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_day(s: str) -> date:
    y, m, d = (int(x) for x in str(s)[:10].split("-"))
    return date(y, m, d)


def month_str(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def next_month(d: date) -> date:
    return date(d.year + 1, 1, 1) if d.month == 12 else date(d.year, d.month + 1, 1)


def fetch(url: str) -> bytes | None:
    parts = urllib.parse.urlsplit(url)
    safe = urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, urllib.parse.quote(parts.path), parts.query, parts.fragment)
    )
    last = None
    for attempt in range(MAX_RETRIES):
        try:
            with urllib.request.urlopen(safe, timeout=120) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            last = e
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(RETRY_SLEEP * (attempt + 1))
    raise RuntimeError(f"fetch failed after {MAX_RETRIES} attempts: {url} :: {last}")


def load_month(symbol: str, ms: date) -> tuple[str, dict[date, dict]]:
    """Return (OK|UNAVAILABLE, {day: {rows, volume, open, high, low, close}})."""
    stem = f"{symbol}-1d-{month_str(ms)}"
    body = fetch(f"{BASE}/{symbol}/1d/{stem}.zip")
    if body is None:
        return "UNAVAILABLE", {}

    chk = fetch(f"{BASE}/{symbol}/1d/{stem}.zip.CHECKSUM")
    if chk is not None:
        expected = chk.split()[0].decode("ascii").lower()
        if expected != hashlib.sha256(body).hexdigest():
            raise RuntimeError(f"CHECKSUM mismatch for {stem}")

    days: dict[date, dict] = {}
    with zipfile.ZipFile(io.BytesIO(body)) as z:
        for raw in z.read(z.namelist()[0]).decode("utf-8", "replace").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            f = raw.split(",")
            if not f[0].lstrip("-").isdigit():
                continue
            ot = int(f[0])
            if ot > 10_000_000_000_000:
                ot //= 1000
            d = date.fromtimestamp(ot / 1000.0)
            try:
                o, hi, lo, c = float(f[1]), float(f[2]), float(f[3]), float(f[4])
                vol = float(f[5])
                trades = int(float(f[8])) if len(f) > 8 else -1
            except (ValueError, IndexError):
                o = hi = lo = c = vol = -1.0
                trades = -1
            # Same validity test as Steps 2 and 2b.
            if vol == 0.0 and trades == 0:
                continue
            rec = days.setdefault(d, {"rows": 0, "volume": 0.0, "open": o,
                                      "high": hi, "low": lo, "close": c})
            rec["rows"] += 1
            if vol > 0:
                rec["volume"] += vol
            rec["close"] = c
    return "OK", days


def all_gaps(observed: set[date], lo: date, hi: date) -> list[tuple[date, date]]:
    """Every >= GAP_THRESHOLD_DAYS run of missing days bounded by observations."""
    pts = sorted(d for d in observed if lo <= d <= hi)
    out = []
    for a, b in zip(pts, pts[1:]):
        if (b - a).days - 1 >= GAP_THRESHOLD_DAYS:
            out.append((a + timedelta(days=1), b - timedelta(days=1)))
    return out


def main() -> int:
    for p in (PREV_AUDIT, BACKWARD, UNIVERSE):
        if not p.exists():
            print(f"FATAL: {p} not found", file=sys.stderr)
            return 2

    prev = json.loads(PREV_AUDIT.read_text())
    if prev.get("frozen_inputs", {}).get("examination_set_sha256") != EXPECTED_EXAM_SHA256:
        print("FATAL: examination set sha256 mismatch", file=sys.stderr)
        return 2
    if prev.get("frozen_inputs", {}).get("gap_threshold_missing_calendar_days") != GAP_THRESHOLD_DAYS:
        print("FATAL: gap threshold differs from Step 2", file=sys.stderr)
        return 2

    u_sha = sha256_file(UNIVERSE)
    if u_sha != EXPECTED_UNIVERSE_SHA256:
        print(f"FATAL: universe.json sha256 mismatch: {u_sha}", file=sys.stderr)
        return 2

    records = prev["symbols"]
    if len(records) != 118:
        print(f"FATAL: expected 118 symbols, got {len(records)}", file=sys.stderr)
        return 2

    print("examination set: 118 symbols (sha256 chain verified)")
    print(f"window         : {WINDOW_START} .. {WINDOW_END}")
    print(f"gap threshold  : >= {GAP_THRESHOLD_DAYS} consecutive missing calendar days")
    print("first in-window observation is MEASURED, never defaulted")
    print()

    forensic: dict[str, dict] = {}
    results = []
    t0 = time.time()
    total = len(records)

    for i, rec in enumerate(sorted(records, key=lambda r: r["symbol"]), 1):
        sym = rec["symbol"]
        birth = parse_day(rec["original_first_trading_day"])
        start = max(WINDOW_START, birth)

        observed: set[date] = set()
        months_loaded, unavailable = [], []
        downloads = 0
        cur = date(start.year, start.month, 1)
        end_m = date(WINDOW_END.year, WINDOW_END.month, 1)

        while cur <= end_m:
            status, days = load_month(sym, cur)
            downloads += 1
            if status == "UNAVAILABLE":
                unavailable.append(month_str(cur))
            else:
                months_loaded.append(month_str(cur))
                for d, r in days.items():
                    if WINDOW_START <= d <= WINDOW_END:
                        observed.add(d)
                    if sym == FORENSIC_SYMBOL and FORENSIC_FROM <= d <= FORENSIC_TO:
                        forensic[d.isoformat()] = r
            cur = next_month(cur)

        # Extra month for the forensic symbol's pre-window side of the seam.
        if sym == FORENSIC_SYMBOL:
            status, days = load_month(sym, date(2025, 6, 1))
            downloads += 1
            if status == "OK":
                for d, r in days.items():
                    if FORENSIC_FROM <= d <= FORENSIC_TO:
                        forensic[d.isoformat()] = r

        measured_first = min(observed).isoformat() if observed else None
        measured_last = max(observed).isoformat() if observed else None
        step2_first = rec.get("first_in_window_observation")
        agrees = (measured_first == step2_first)

        gaps = all_gaps(observed, WINDOW_START, WINDOW_END)
        step2_gap_count = len(rec.get("gaps") or [])

        out_rec = {
            "symbol": sym,
            "original_first_trading_day": birth.isoformat(),
            "measured_first_in_window_observation": measured_first,
            "step2_reported_first_in_window_observation": step2_first,
            "first_observation_agrees_with_step2": agrees,
            "measured_last_in_window_observation": measured_last,
            "observed_trading_days_in_window": len(observed),
            "gaps": [{"gap_start": a.isoformat(), "gap_end": b.isoformat(),
                      "missing_calendar_days": (b - a).days + 1} for a, b in gaps],
            "gap_count_this_run": len(gaps),
            "gap_count_step2": step2_gap_count,
            "gap_count_agrees_with_step2": len(gaps) == step2_gap_count,
            "months_loaded": months_loaded,
            "months_unavailable": unavailable,
            "status": "INDETERMINATE" if unavailable else "COMPLETE",
            "archives_downloaded": downloads,
        }
        results.append(out_rec)

        flags = []
        if not agrees:
            flags.append(f"FIRST-OBS DISAGREES step2={step2_first} measured={measured_first}")
        if len(gaps) != step2_gap_count:
            flags.append(f"GAP COUNT DISAGREES step2={step2_gap_count} measured={len(gaps)}")
        if unavailable:
            flags.append(f"INDETERMINATE months={unavailable}")
        print(f"[{i:3d}/{total}] {sym:<16} {downloads:>3} arch  "
              f"first={measured_first}  gaps={len(gaps)}"
              + ("   <<< " + " | ".join(flags) if flags else ""), flush=True)

    elapsed = round(time.time() - t0, 1)
    disagree_first = [r for r in results if not r["first_observation_agrees_with_step2"]]
    disagree_gaps = [r for r in results if not r["gap_count_agrees_with_step2"]]
    with_gaps = [r for r in results if r["gap_count_this_run"] > 0]
    indet = [r for r in results if r["status"] == "INDETERMINATE"]

    # --- PUMPUSDT seam verdict -------------------------------------------
    seam = {"span": [FORENSIC_FROM.isoformat(), FORENSIC_TO.isoformat()],
            "observed_days": dict(sorted(forensic.items())),
            "observed_day_count": len(forensic)}
    july_pre = sorted(d for d in forensic if "2025-07-01" <= d <= "2025-07-09")
    july_post = sorted(d for d in forensic if d >= "2025-07-10")
    seam["bars_2025_07_01_to_07_09"] = july_pre
    seam["bars_from_2025_07_10"] = july_post
    if not july_pre and july_post:
        seam["interpretation"] = (
            "NO bars 2025-07-01..2025-07-09. Step 2's first_in_window_observation "
            "of 2025-07-01 was NOT measured. True effective start is the first "
            "observed bar, consistent with the Pump.fun launch of 2025-07-10. "
            "Age at 2025-09-01 formation would be 53 days -- BELOW the 60-day gate."
        )
    elif july_pre and july_post:
        pre_close = forensic[july_pre[-1]].get("close")
        post_close = forensic[july_post[0]].get("close")
        ratio = (post_close / pre_close) if (pre_close and post_close and pre_close > 0) else None
        seam["last_close_before_2025_07_10"] = pre_close
        seam["first_close_from_2025_07_10"] = post_close
        seam["close_ratio_across_seam"] = ratio
        seam["interpretation"] = (
            "Bars EXIST 2025-07-01..2025-07-09, so the run beginning 2025-07-01 "
            "spans the 2025-07-10 Pump.fun launch with NO absence. If the close "
            "ratio across the seam is far from 1, two different underlyings are "
            "concatenated and the frozen >= 7-day rule cannot detect it by "
            "construction: the effective date is mechanically correct and "
            "economically wrong by nine days."
        )
    else:
        seam["interpretation"] = "Insufficient forensic coverage to rule."

    out = {
        "schema_version": 1,
        "deliverable": "A-2 Step 2d -- independent forward continuity verification",
        "status": "COMPLETE" if not indet else "COMPLETE_WITH_INDETERMINATE",
        "purpose": (
            "Independently re-derive in-window continuity, which until now rested "
            "entirely on the Step 2 implementation later proven defective in its "
            "backward path; and resolve the PUMPUSDT 2025-07 seam."
        ),
        "frozen_inputs": {
            "examination_set_count": 118,
            "examination_set_sha256": EXPECTED_EXAM_SHA256,
            "universe_sha256": EXPECTED_UNIVERSE_SHA256,
            "window_start": WINDOW_START.isoformat(),
            "window_end": WINDOW_END.isoformat(),
            "gap_threshold_missing_calendar_days": GAP_THRESHOLD_DAYS,
        },
        "method_notes": [
            "first in-window observation is MEASURED from daily-bar timestamps",
            "Step 2's first_in_window_observation is used for COMPARISON ONLY",
            "a missing monthly archive is INDETERMINATE, never a gap",
            "evidence is daily-bar timestamps, never monthly ZIP presence",
        ],
        "counts": {
            "symbols_scanned": len(results),
            "archives_downloaded": sum(r["archives_downloaded"] for r in results),
            "symbols_with_in_window_gap": len(with_gaps),
            "symbols_disagreeing_on_first_observation": len(disagree_first),
            "symbols_disagreeing_on_gap_count": len(disagree_gaps),
            "symbols_indeterminate": len(indet),
            "elapsed_seconds": elapsed,
        },
        "agreement_with_step2": {
            "first_observation": "FULL" if not disagree_first else "DISAGREEMENT",
            "gap_detection": "FULL" if not disagree_gaps else "DISAGREEMENT",
            "disagreements_first_observation": [
                {"symbol": r["symbol"],
                 "step2": r["step2_reported_first_in_window_observation"],
                 "measured": r["measured_first_in_window_observation"]}
                for r in disagree_first],
            "disagreements_gap_count": [
                {"symbol": r["symbol"], "step2": r["gap_count_step2"],
                 "measured": r["gap_count_this_run"], "gaps": r["gaps"]}
                for r in disagree_gaps],
        },
        "symbols_with_in_window_gaps": [
            {"symbol": r["symbol"], "gaps": r["gaps"]} for r in with_gaps],
        "pumpusdt_july_2025_seam": seam,
        "symbols": results,
        "prohibitions_observed": [
            "No threshold, window or operator altered",
            "Step 2 output used for comparison only, never as an input",
            "A missing monthly archive is INDETERMINATE, never a gap",
            "universe.json read only -- digest asserted unchanged",
            "Membership not rebuilt; no eligibility recomputation performed here",
            "A-2 detector not run",
            "No weekend/volatility/volume detector statistics computed",
            "No strategy performance computed or inspected",
            "No Stage 2 work performed",
        ],
    }

    OUTDIR.mkdir(parents=True, exist_ok=True)
    OUTFILE.write_text(json.dumps(out, indent=2))

    print()
    print("=== A-2 STEP 2d FORWARD VERIFICATION SUMMARY ===")
    for k, v in out["counts"].items():
        print(f"{k}: {v}")
    print()
    print(f"first-observation agreement with Step 2 : {out['agreement_with_step2']['first_observation']}")
    print(f"gap-detection  agreement with Step 2    : {out['agreement_with_step2']['gap_detection']}")
    for d in out["agreement_with_step2"]["disagreements_first_observation"]:
        print(f"  DISAGREE {d['symbol']}: step2={d['step2']} measured={d['measured']}")
    for d in out["agreement_with_step2"]["disagreements_gap_count"]:
        print(f"  DISAGREE {d['symbol']}: step2={d['step2']} measured={d['measured']} {d['gaps']}")
    print()
    print("=== PUMPUSDT 2025-07 SEAM ===")
    for d in sorted(seam["observed_days"]):
        r = seam["observed_days"][d]
        print(f"  {d}  close={r.get('close')}  volume={r.get('volume')}")
    print()
    print(f"  bars 2025-07-01..07-09 : {seam['bars_2025_07_01_to_07_09'] or 'NONE'}")
    print(f"  bars from 2025-07-10   : {seam['bars_from_2025_07_10'][:3] or 'NONE'}")
    if "close_ratio_across_seam" in seam:
        print(f"  close ratio across seam: {seam['close_ratio_across_seam']}")
    print(f"  -> {seam['interpretation']}")
    print()
    print(f"written: {OUTFILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
