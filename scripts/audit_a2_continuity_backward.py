#!/usr/bin/env python3
"""
A-2 Step 2b -- BACKWARD continuity completion audit.

WHY THIS EXISTS
---------------
The Step 2 audit (research/multicoin_expansion/a2_continuity_audit.json) returned
0 gaps / 0 effective-start resets across 118 symbols. That result is NOT evidence
of continuity before 2025-07-01. Its boundary backward check was gated on

        if first_in_window_observation > AUDIT_WINDOW_START and original < AUDIT_WINDOW_START

For every one of the 73 symbols whose original_first_trading_day precedes
2025-07-01, the archive contains a bar ON 2025-07-01, so the left-hand condition
evaluated False and the backward check NEVER FIRED -- not once across all 118
symbols. Pre-window history was therefore never examined for those 73 symbols.

The FROZEN rule text is:

    effective_first_trading_day = first trading day of the latest continuous
    trading run reaching the study period, where a run is broken by >= 7
    consecutive calendar days with no valid trading observations

Evaluating that requires walking backwards from the first in-window observation
until a >= 7-day break is found or the symbol's birth is reached, irrespective of
whether the first in-window observation falls on the window's first day.

NOTHING IS TUNED HERE. GAP_THRESHOLD_DAYS remains 7. AGE_GATE_DAYS remains 60.
No threshold, operator, window, ranking rule or selection count is altered. This
script corrects an implementation that under-covered the frozen rule; it does not
change the rule. The walk moves strictly BACKWARDS from 2025-07-01 and therefore
touches no SEALED period.

TERMINATION
-----------
The walk stops when (a) a >= 7-day break is found -- the rule's own stopping
condition, since the latest run has then been established -- or (b) the symbol's
original_first_trading_day is reached. There is NO discretionary lookback
horizon: introducing one after observing Step 2's output would be a post-hoc
design choice.

A MISSING MONTHLY ARCHIVE IS NOT A GAP
--------------------------------------
A 404 on a monthly archive is recorded as INDETERMINATE and halts that symbol's
walk. It is never counted as absence of trading. (This is the error that produced
71 false positives in the earlier manifest-based screen.)
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import date, timedelta

# ---------------------------------------------------------------------------
# FROZEN PARAMETERS -- DO NOT TUNE AFTER OBSERVING RESULTS
# ---------------------------------------------------------------------------
GAP_THRESHOLD_DAYS = 7          # FROZEN (identical to Step 2)
AGE_GATE_DAYS = 60              # FROZEN (identical to Step 2)
AUDIT_WINDOW_START = date(2025, 7, 1)   # anchor: known-present for all 118

EXPECTED_EXAM_SHA256 = "8239751d74dc4d758252e0764e8a6f3d4f43b25bc4f35761050b7a146425d685"
EXPECTED_UNIVERSE_SHA256 = "ea79bbdeff91bdc3918c523bf248e2c2fe88aa34ec161f2633d0b1940c75c12a"

# Forensic sub-question: the PUMPUSDT seam. Every daily bar in this span is
# dumped verbatim so the 2025-06-13 removal date can be checked against data.
FORENSIC_SYMBOL = "PUMPUSDT"
FORENSIC_FROM = date(2025, 4, 1)
FORENSIC_TO = date(2025, 7, 31)

BASE = "https://data.binance.vision/data/futures/um/monthly/klines"
REPO = pathlib.Path(__file__).resolve().parents[1]
OUTDIR = REPO / "research" / "multicoin_expansion"
PREV_AUDIT = OUTDIR / "a2_continuity_audit.json"
UNIVERSE = OUTDIR / "universe.json"
OUTFILE = OUTDIR / "a2_continuity_audit_backward.json"

MAX_RETRIES = 4
RETRY_SLEEP = 3.0


def sha256_file(p: pathlib.Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_day(s: str) -> date:
    y, m, d = (int(x) for x in s.split("-"))
    return date(y, m, d)


def month_str(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def month_start(d: date) -> date:
    return date(d.year, d.month, 1)


def prev_month(d: date) -> date:
    return date(d.year - 1, 12, 1) if d.month == 1 else date(d.year, d.month - 1, 1)


def fetch(url: str) -> bytes | None:
    """Return body, or None on HTTP 404. Raise on anything else after retries."""
    # Quote the path only -- some archive symbols contain non-ASCII characters
    # which break urllib if passed through raw.
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
        except Exception as e:  # noqa: BLE001 -- transient network
            last = e
        time.sleep(RETRY_SLEEP * (attempt + 1))
    raise RuntimeError(f"fetch failed after {MAX_RETRIES} attempts: {url} :: {last}")


def load_month(symbol: str, ms: date) -> tuple[str, dict[date, dict]]:
    """
    Return (status, {day: {"rows": n, "volume": v}}).

    status is one of:
      OK            archive present and verified
      UNAVAILABLE   archive absent (HTTP 404) -- NOT evidence of no trading
    """
    stem = f"{symbol}-1d-{month_str(ms)}"
    body = fetch(f"{BASE}/{symbol}/1d/{stem}.zip")
    if body is None:
        return "UNAVAILABLE", {}

    chk = fetch(f"{BASE}/{symbol}/1d/{stem}.zip.CHECKSUM")
    if chk is not None:
        # CHECKSUM files embed the filename, which may be non-ASCII, so split on
        # bytes and decode only the digest field.
        expected = chk.split()[0].decode("ascii").lower()
        actual = hashlib.sha256(body).hexdigest()
        if expected != actual:
            raise RuntimeError(f"CHECKSUM mismatch for {stem}: {expected} != {actual}")

    days: dict[date, dict] = {}
    with zipfile.ZipFile(io.BytesIO(body)) as z:
        name = z.namelist()[0]
        for raw in z.read(name).decode("utf-8", "replace").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            f = raw.split(",")
            if not f[0].lstrip("-").isdigit():
                continue  # header row
            ot = int(f[0])
            if ot > 10_000_000_000_000:   # microseconds
                ot //= 1000
            d = date.fromtimestamp(ot / 1000.0)
            try:
                vol = float(f[5])
                trades = int(float(f[8])) if len(f) > 8 else -1
            except (ValueError, IndexError):
                vol, trades = -1.0, -1
            # A bar with zero volume AND zero trades is not a valid trading
            # observation. Same validity test as Step 2.
            if vol == 0.0 and trades == 0:
                continue
            rec = days.setdefault(d, {"rows": 0, "volume": 0.0})
            rec["rows"] += 1
            if vol > 0:
                rec["volume"] += vol
    return "OK", days


def find_latest_break(observed: set[date], lo: date, hi: date) -> tuple[date, date] | None:
    """
    Return the LATEST (gap_start, gap_end) inclusive run of >= GAP_THRESHOLD_DAYS
    consecutive calendar days with no observation, bounded on BOTH sides by an
    observation inside [lo, hi]. Returns None if there is no such gap.

    Requiring an observation on both sides is what keeps a partially-loaded
    leading edge from being mistaken for a gap.
    """
    pts = sorted(d for d in observed if lo <= d <= hi)
    latest = None
    for a, b in zip(pts, pts[1:]):
        missing = (b - a).days - 1
        if missing >= GAP_THRESHOLD_DAYS:
            latest = (a + timedelta(days=1), b - timedelta(days=1))
    return latest


def walk_back(symbol: str, original: date, forensic: dict | None) -> dict:
    """Walk backwards month by month from 2025-06 to month(original)."""
    observed: set[date] = {AUDIT_WINDOW_START}   # anchor, confirmed by Step 2
    months_loaded: list[str] = []
    downloads = 0
    cur = prev_month(AUDIT_WINDOW_START)
    target = month_start(original)

    walk_status = "COMPLETE_TO_BIRTH"
    indeterminate_from = None
    break_found = None

    while cur >= target:
        status, days = load_month(symbol, cur)
        downloads += 1
        if status == "UNAVAILABLE":
            walk_status = "INDETERMINATE"
            indeterminate_from = month_str(cur)
            break
        months_loaded.append(month_str(cur))
        observed |= set(days.keys())

        if forensic is not None:
            for d, rec in days.items():
                if FORENSIC_FROM <= d <= FORENSIC_TO:
                    forensic[d.isoformat()] = rec

        # Coverage is complete from the start of the earliest loaded month
        # through the anchor, so gaps inside that span are trustworthy.
        brk = find_latest_break(observed, cur, AUDIT_WINDOW_START)
        if brk is not None:
            break_found = brk
            walk_status = "TERMINATED_ON_BREAK"
            break
        cur = prev_month(cur)

    if break_found is not None:
        gs, ge = break_found
        after = sorted(d for d in observed if d > ge)
        effective = after[0]
        return {
            "symbol": symbol,
            "original_first_trading_day": original.isoformat(),
            "effective_first_trading_day": effective.isoformat(),
            "effective_start_changed": effective != original,
            "walk_status": walk_status,
            "latest_break": {
                "gap_start": gs.isoformat(),
                "gap_end": ge.isoformat(),
                "missing_calendar_days": (ge - gs).days + 1,
            },
            "months_loaded": months_loaded,
            "archives_downloaded": downloads,
            "indeterminate_from_month": None,
        }

    if walk_status == "INDETERMINATE":
        earliest = min(observed)
        return {
            "symbol": symbol,
            "original_first_trading_day": original.isoformat(),
            "effective_first_trading_day": None,
            "effective_start_changed": None,
            "walk_status": "INDETERMINATE",
            "latest_break": None,
            "earliest_confirmed_continuous_day": earliest.isoformat(),
            "months_loaded": months_loaded,
            "archives_downloaded": downloads,
            "indeterminate_from_month": indeterminate_from,
        }

    earliest = min(d for d in observed)
    return {
        "symbol": symbol,
        "original_first_trading_day": original.isoformat(),
        "effective_first_trading_day": earliest.isoformat(),
        "effective_start_changed": earliest != original,
        "walk_status": "COMPLETE_TO_BIRTH",
        "latest_break": None,
        "months_loaded": months_loaded,
        "archives_downloaded": downloads,
        "indeterminate_from_month": None,
    }


def main() -> int:
    if not PREV_AUDIT.exists():
        print(f"FATAL: {PREV_AUDIT} not found", file=sys.stderr)
        return 2
    prev = json.loads(PREV_AUDIT.read_text())

    # Chain provably to the SAME examination set as Step 2.
    got = prev.get("frozen_inputs", {}).get("examination_set_sha256")
    if got != EXPECTED_EXAM_SHA256:
        print(f"FATAL: examination set sha256 mismatch\n  expected {EXPECTED_EXAM_SHA256}\n  got      {got}", file=sys.stderr)
        return 2
    if prev.get("frozen_inputs", {}).get("gap_threshold_missing_calendar_days") != GAP_THRESHOLD_DAYS:
        print("FATAL: gap threshold differs from Step 2", file=sys.stderr)
        return 2
    if prev.get("frozen_inputs", {}).get("age_gate_days") != AGE_GATE_DAYS:
        print("FATAL: age gate differs from Step 2", file=sys.stderr)
        return 2

    if UNIVERSE.exists():
        u_sha = sha256_file(UNIVERSE)
        if u_sha != EXPECTED_UNIVERSE_SHA256:
            print(f"FATAL: universe.json sha256 mismatch\n  expected {EXPECTED_UNIVERSE_SHA256}\n  got      {u_sha}", file=sys.stderr)
            return 2
        universe = json.loads(UNIVERSE.read_text())
    else:
        print("FATAL: universe.json not found", file=sys.stderr)
        return 2

    records = prev["symbols"]
    if len(records) != 118:
        print(f"FATAL: expected 118 symbols in prior audit, got {len(records)}", file=sys.stderr)
        return 2

    in_scope, out_of_scope = [], []
    for r in records:
        o = parse_day(r["original_first_trading_day"])
        (in_scope if o < AUDIT_WINDOW_START else out_of_scope).append((r["symbol"], o))

    print(f"examination set        : 118 symbols (sha256 chain verified)")
    print(f"backward walk required : {len(in_scope)} (born before {AUDIT_WINDOW_START})")
    print(f"no pre-window history  : {len(out_of_scope)}")
    print()

    forensic: dict[str, dict] = {}
    results = []
    t0 = time.time()
    for i, (sym, o) in enumerate(sorted(in_scope), 1):
        f = forensic if sym == FORENSIC_SYMBOL else None
        rec = walk_back(sym, o, f)
        results.append(rec)
        flag = ""
        if rec["walk_status"] == "TERMINATED_ON_BREAK":
            flag = f"  <-- BREAK {rec['latest_break']['gap_start']}..{rec['latest_break']['gap_end']} ({rec['latest_break']['missing_calendar_days']}d) effective={rec['effective_first_trading_day']}"
        elif rec["walk_status"] == "INDETERMINATE":
            flag = f"  <-- INDETERMINATE from {rec['indeterminate_from_month']}"
        print(f"[{i:3d}/{len(in_scope)}] {sym:<16} {rec['archives_downloaded']:>3} archives  {rec['walk_status']}{flag}", flush=True)

    elapsed = round(time.time() - t0, 1)

    changed = [r for r in results if r.get("effective_start_changed") is True]
    indet = [r for r in results if r["walk_status"] == "INDETERMINATE"]

    # Eligibility impact, reported only -- NOT rebuilt here. Rebuilding
    # membership is a separate authorised step.
    impact = []
    for r in changed:
        eff = parse_day(r["effective_first_trading_day"])
        rows = []
        for month, mrec in sorted(universe.get("months", {}).items()):
            fd = mrec.get("formation_date") or mrec.get("formation_day") or mrec.get("as_of")
            if not fd:
                continue
            fdd = parse_day(str(fd)[:10])
            age_orig = (fdd - parse_day(r["original_first_trading_day"])).days
            age_eff = (fdd - eff).days
            is_member = any(m.get("symbol") == r["symbol"] for m in mrec.get("members", []))
            rank = next((m.get("rank") for m in mrec.get("members", []) if m.get("symbol") == r["symbol"]), None)
            if age_orig >= AGE_GATE_DAYS and age_eff < AGE_GATE_DAYS:
                rows.append({
                    "formation_month": month,
                    "formation_date": fdd.isoformat(),
                    "age_days_under_original": age_orig,
                    "age_days_under_effective": age_eff,
                    "was_eligible": True,
                    "now_eligible": False,
                    "currently_selected_member": is_member,
                    "current_rank": rank,
                })
        impact.append({"symbol": r["symbol"], "affected_formation_months": rows})

    out = {
        "schema_version": 1,
        "deliverable": "A-2 Step 2b -- backward continuity completion audit",
        "status": "COMPLETE" if not indet else "COMPLETE_WITH_INDETERMINATE",
        "supersedes_coverage_gap_in": "a2_continuity_audit.json",
        "coverage_gap_corrected": (
            "Step 2's boundary backward check was gated on "
            "first_in_window_observation > audit_window_start, which was False for all "
            "73 symbols born before 2025-07-01 because each has a bar on 2025-07-01. "
            "The check therefore fired 0 times and pre-window history was never examined."
        ),
        "frozen_inputs": {
            "examination_set_count": 118,
            "examination_set_sha256": EXPECTED_EXAM_SHA256,
            "universe_sha256": EXPECTED_UNIVERSE_SHA256,
            "anchor_day": AUDIT_WINDOW_START.isoformat(),
            "gap_threshold_missing_calendar_days": GAP_THRESHOLD_DAYS,
            "age_gate_days": AGE_GATE_DAYS,
            "age_gate_operator": ">=",
        },
        "rule": {
            "effective_first_trading_day": (
                "first trading day of the latest continuous trading run reaching the study "
                "period, where a run is broken by >= 7 consecutive calendar days with no "
                "valid trading observations"
            ),
            "unchanged_from_step2": True,
            "lookback_horizon": "symbol birth -- no discretionary bound",
        },
        "counts": {
            "symbols_in_examination_set": 118,
            "symbols_requiring_backward_walk": len(in_scope),
            "symbols_with_no_pre_window_history": len(out_of_scope),
            "symbols_walked": len(results),
            "archives_downloaded": sum(r["archives_downloaded"] for r in results),
            "symbols_with_pre_window_break": sum(1 for r in results if r["walk_status"] == "TERMINATED_ON_BREAK"),
            "symbols_with_effective_start_change": len(changed),
            "symbols_indeterminate": len(indet),
            "elapsed_seconds": elapsed,
        },
        "effective_start_changes": [
            {
                "symbol": r["symbol"],
                "original_first_trading_day": r["original_first_trading_day"],
                "effective_first_trading_day": r["effective_first_trading_day"],
                "latest_break": r["latest_break"],
            }
            for r in changed
        ],
        "eligibility_impact_report_only": impact,
        "indeterminate_symbols": indet,
        "pumpusdt_forensic_daily_bars": {
            "span": [FORENSIC_FROM.isoformat(), FORENSIC_TO.isoformat()],
            "observed_days": dict(sorted(forensic.items())),
            "observed_day_count": len(forensic),
        },
        "symbols": results,
        "prohibitions_observed": [
            "No threshold, window, operator, ranking rule or selection count altered",
            "No discretionary lookback horizon introduced",
            "Walk moves strictly backwards from 2025-07-01 -- no SEALED period touched",
            "A missing monthly archive is recorded INDETERMINATE, never counted as a gap",
            "A-2 detector not run",
            "No weekend/volatility/volume detector statistics computed",
            "No strategy performance computed or inspected",
            "No Stage 2 work performed",
            "universe.json not modified",
            "Membership not rebuilt -- eligibility impact is reported only",
        ],
    }

    OUTDIR.mkdir(parents=True, exist_ok=True)
    OUTFILE.write_text(json.dumps(out, indent=2, sort_keys=False))

    print()
    print("=== A-2 STEP 2b BACKWARD CONTINUITY AUDIT SUMMARY ===")
    for k, v in out["counts"].items():
        print(f"{k}: {v}")
    print()
    if changed:
        print("EFFECTIVE START CHANGES:")
        for c in out["effective_start_changes"]:
            b = c["latest_break"]
            print(f"  {c['symbol']}: {c['original_first_trading_day']} -> {c['effective_first_trading_day']}"
                  f"  (break {b['gap_start']}..{b['gap_end']}, {b['missing_calendar_days']}d)")
    else:
        print("EFFECTIVE START CHANGES: none")
    print()
    if indet:
        print("INDETERMINATE:")
        for r in indet:
            print(f"  {r['symbol']}: archive unavailable from {r['indeterminate_from_month']};"
                  f" continuous back to {r['earliest_confirmed_continuous_day']}")
    print()
    print(f"PUMPUSDT forensic: {len(forensic)} observed trading days in "
          f"{FORENSIC_FROM} .. {FORENSIC_TO}")
    if forensic:
        ks = sorted(forensic)
        print(f"  first={ks[0]}  last={ks[-1]}")
        june = [k for k in ks if k.startswith("2025-06")]
        july = [k for k in ks if k.startswith("2025-07")]
        print(f"  June 2025 observed days: {len(june)}" + (f"  ({june[0]} .. {june[-1]})" if june else ""))
        print(f"  July 2025 observed days: {len(july)}" + (f"  ({july[0]} .. {july[-1]})" if july else ""))
    print()
    print(f"written: {OUTFILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
