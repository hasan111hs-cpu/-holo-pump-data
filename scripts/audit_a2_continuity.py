#!/usr/bin/env python3
"""A-2 Step 2: frozen 7-day continuity / age-integrity audit.

DATA-INTEGRITY AUDIT ONLY.

Frozen rule
-----------
effective_first_trading_day = first trading day of the latest continuous
trading run reaching the study period, where a run is broken by >= 7
consecutive calendar days with no valid trading observations.

The original first_trading_day is preserved for provenance and is never
modified. This script does not run the A-2 detector, compute weekend/volatility/
volume detector statistics, inspect strategy performance, or perform Stage 2.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sys
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO_UNIVERSE = Path("research/multicoin_expansion/universe.json")
OUTPUT = Path("research/multicoin_expansion/a2_continuity_audit.json")
CACHE = Path("_stage1_cache")
ROOT = Path("data/futures/um")
BASE_URL = "https://data.binance.vision"

WINDOW_START = date(2025, 7, 1)
WINDOW_END = date(2026, 8, 31)
GAP_THRESHOLD_DAYS = 7  # FROZEN. DO NOT TUNE AFTER OBSERVING RESULTS.
AGE_GATE_DAYS = 60
EXPECTED_EXAM_SHA256 = "8239751d74dc4d758252e0764e8a6f3d4f43b25bc4f35761050b7a146425d685"
MONTH_RE = re.compile(r"-(\d{4})-(\d{2})\.zip$")

# Documentary cause known before this mechanical audit. Do not infer causes
# from price, volume, weekend behavior, or strategy results.
DOCUMENTED_CAUSES = {
    "PUMPUSDT": "documented ticker reassignment: PumpBTC -> Pump.fun",
}


@dataclass(frozen=True)
class Gap:
    last_pre_gap: date
    first_post_gap: date

    @property
    def missing_calendar_days(self) -> int:
        return (self.first_post_gap - self.last_pre_gap).days - 1


class AuditError(RuntimeError):
    pass


def halt(message: str) -> None:
    raise AuditError(message)


def ts_ms(value: str) -> int:
    n = int(float(value))
    return n // 1000 if n > 10**14 else n


def month_floor(d: date) -> date:
    return date(d.year, d.month, 1)


def previous_month(d: date) -> date:
    return date(d.year - 1, 12, 1) if d.month == 1 else date(d.year, d.month - 1, 1)


def month_iter(start: date, end: date):
    cur = month_floor(start)
    stop = month_floor(end)
    while cur <= stop:
        yield cur
        cur = date(cur.year + (cur.month == 12), 1 if cur.month == 12 else cur.month + 1, 1)


def archive_key(symbol: str, month: date) -> str:
    ym = f"{month.year:04d}-{month.month:02d}"
    return f"data/futures/um/monthly/klines/{symbol}/1d/{symbol}-1d-{ym}.zip"


def cache_path(symbol: str, month: date) -> Path:
    return CACHE / archive_key(symbol, month)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def parse_checksum(text: str, expected_filename: str) -> str:
    """Parse Binance .CHECKSUM and require the line to name our ZIP exactly."""
    matches: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        digest = parts[0].strip().lower()
        named = parts[-1].lstrip("*")
        if named == expected_filename and re.fullmatch(r"[0-9a-f]{64}", digest):
            matches.append(digest)
    if len(matches) != 1:
        halt(f"checksum file did not contain exactly one entry for {expected_filename}: {matches}")
    return matches[0]


def http_get(url: str, allow_404: bool = False) -> bytes | None:
    req = urllib.request.Request(url, headers={"User-Agent": "a2-continuity-audit/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if allow_404 and e.code == 404:
            return None
        halt(f"HTTP {e.code} downloading {url}")
    except urllib.error.URLError as e:
        halt(f"download failed for {url}: {e}")
    raise AssertionError("unreachable")


def ensure_verified_archive(symbol: str, month: date) -> tuple[Path | None, bool]:
    """Return archive path; download targeted missing month and verify .CHECKSUM.

    Cached Stage-1 files are reused as-is because Stage 1 already checksum-verified
    them before saving the cache. Any archive downloaded by THIS audit is verified
    against Binance's published .CHECKSUM before it is written into the restored
    cache hierarchy.
    """
    path = cache_path(symbol, month)
    if path.is_file():
        return path, False

    key = archive_key(symbol, month)
    url = f"{BASE_URL}/{key}"
    checksum_url = f"{url}.CHECKSUM"
    print(f"BACKWARD FETCH {symbol} {month:%Y-%m}: {url}")
    blob = http_get(url, allow_404=True)
    if blob is None:
        print(f"BACKWARD FETCH {symbol} {month:%Y-%m}: archive not published (404)")
        return None, False
    checksum_blob = http_get(checksum_url)
    assert checksum_blob is not None
    checksum_text = checksum_blob.decode("utf-8", errors="strict")
    expected = parse_checksum(checksum_text, path.name)
    actual = sha256_bytes(blob)
    if actual != expected:
        halt(
            f"{symbol} {month:%Y-%m}: SHA256 mismatch: "
            f"published={expected} downloaded={actual}"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(blob)
    return path, True


def read_archive_dates(path: Path) -> set[date]:
    out: set[date] = set()
    try:
        with zipfile.ZipFile(path) as z:
            names = [n for n in z.namelist() if n.lower().endswith(".csv")]
            if len(names) != 1:
                halt(f"{path}: expected exactly one CSV, found {names}")
            with z.open(names[0]) as raw:
                with io.TextIOWrapper(raw, encoding="utf-8") as text:
                    for row in csv.reader(text):
                        if not row:
                            continue
                        try:
                            opened = datetime.fromtimestamp(ts_ms(row[0].strip()) / 1000, tz=timezone.utc)
                        except (ValueError, OverflowError):
                            continue  # header/non-data row
                        d = opened.date()
                        if d in out:
                            halt(f"{path}: duplicate 1d UTC date {d}")
                        out.add(d)
    except zipfile.BadZipFile as e:
        halt(f"bad ZIP {path}: {e}")
    return out


def load_json(path: Path):
    if not path.is_file():
        halt(f"required file missing: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def rebuild_exam(universe: dict) -> list[str]:
    symbols: set[str] = set()
    months = universe.get("months")
    if not isinstance(months, dict) or len(months) != 12:
        halt("universe.json: expected exactly 12 months")
    for month_key, month in months.items():
        ranking = month.get("full_eligible_ranking")
        if not isinstance(ranking, list):
            halt(f"{month_key}: missing full_eligible_ranking")
        for row in ranking[:30]:
            symbols.add(row["symbol"])
    ordered = sorted(symbols)
    digest = hashlib.sha256("\n".join(ordered).encode("utf-8")).hexdigest()
    if digest != EXPECTED_EXAM_SHA256:
        halt(
            "examination-set SHA256 mismatch: "
            f"expected={EXPECTED_EXAM_SHA256} actual={digest} count={len(ordered)}"
        )
    if len(ordered) != 118:
        halt(f"examination-set count mismatch despite hash check: {len(ordered)}")
    return ordered


def original_first_days(universe: dict, symbols: list[str]) -> dict[str, date]:
    found: dict[str, set[date]] = {s: set() for s in symbols}
    for month in universe["months"].values():
        for row in month["full_eligible_ranking"]:
            s = row["symbol"]
            if s in found:
                found[s].add(date.fromisoformat(row["first_trading_day"]))
    out: dict[str, date] = {}
    for s in symbols:
        if not found[s]:
            halt(f"{s}: no first_trading_day found in frozen ranking rows")
        if len(found[s]) != 1:
            halt(f"{s}: inconsistent original first_trading_day values: {sorted(found[s])}")
        out[s] = next(iter(found[s]))
    return out


def require_window_archives(symbol: str) -> list[Path]:
    """Require cached archive hierarchy; tolerate pre-listing months only.

    Stage 1 may naturally lack months before a contract's listing. We therefore
    collect cached files for the fixed window and hard-fail later if no daily
    observations exist. We do NOT infer continuity from ZIP presence.
    """
    directory = CACHE / ROOT / "monthly" / "klines" / symbol / "1d"
    if not directory.is_dir():
        halt(f"{symbol}: expected cached 1d hierarchy absent: {directory}")
    paths: list[Path] = []
    for m in month_iter(WINDOW_START, WINDOW_END):
        p = cache_path(symbol, m)
        if p.is_file():
            paths.append(p)
    if not paths:
        halt(f"{symbol}: no cached 1d archives overlap {WINDOW_START}..{WINDOW_END}")
    return paths


def load_window_dates(symbol: str) -> set[date]:
    dates: set[date] = set()
    for path in require_window_archives(symbol):
        for d in read_archive_dates(path):
            if WINDOW_START <= d <= WINDOW_END:
                if d in dates:
                    halt(f"{symbol}: duplicate UTC date across archives: {d}")
                dates.add(d)
    if not dates:
        halt(f"{symbol}: cached archives yielded no bars in audit window")
    return dates


def find_gaps(dates: set[date]) -> list[Gap]:
    ordered = sorted(dates)
    gaps: list[Gap] = []
    for a, b in zip(ordered, ordered[1:]):
        g = Gap(a, b)
        if g.missing_calendar_days >= GAP_THRESHOLD_DAYS:
            gaps.append(g)
    return gaps


def has_at_least_n_continuous_days_ending_at(dates: set[date], end: date, n: int) -> bool:
    return all((end - timedelta(days=i)) in dates for i in range(n))


def backward_boundary_context(
    symbol: str,
    original_first: date,
    first_in_window: date,
) -> tuple[set[date], int]:
    """Targeted backward fetch for a possible gap crossing 2025-07-01.

    Preconditions: first in-window observation > WINDOW_START and original
    first_trading_day < WINDOW_START.

    Walk backward month-by-month. Stop once we have found trading before the
    first in-window observation and at least 7 consecutive prior trading days
    ending at the latest pre-gap trading day. That is enough to locate the start
    of any >=7-day absence crossing the boundary; full history is unnecessary.
    """
    collected: set[date] = set()
    downloaded = 0
    month = previous_month(month_floor(first_in_window))
    earliest_month = month_floor(original_first)

    while month >= earliest_month:
        path, did_download = ensure_verified_archive(symbol, month)
        downloaded += int(did_download)
        if path is not None:
            collected.update(d for d in read_archive_dates(path) if d < first_in_window)

        if collected:
            last_pre = max(collected)
            missing = (first_in_window - last_pre).days - 1
            if missing >= GAP_THRESHOLD_DAYS:
                # The gap start is located at last_pre; no older data can change
                # this boundary gap's post-gap effective start.
                return collected, downloaded
            if has_at_least_n_continuous_days_ending_at(collected, last_pre, GAP_THRESHOLD_DAYS):
                # >=7 continuous prior trading days established and no >=7 gap
                # exists between last_pre and first_in_window.
                return collected, downloaded

        month = previous_month(month)

    # Reaching the original listing month is also a complete targeted search.
    return collected, downloaded


def formation_date(month: dict) -> date:
    raw = month["formation_timestamp"]
    return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()


def main() -> int:
    universe = load_json(REPO_UNIVERSE)
    symbols = rebuild_exam(universe)
    originals = original_first_days(universe, symbols)

    print("A-2 STEP 2 — 7-DAY CONTINUITY AUDIT")
    print(f"Examination set: {len(symbols)} symbols")
    print(f"Examination SHA256: {EXPECTED_EXAM_SHA256}")
    print(f"Frozen gap threshold: >= {GAP_THRESHOLD_DAYS} consecutive missing calendar days")
    print(f"Audit window: {WINDOW_START} .. {WINDOW_END} UTC daily bars")

    symbol_results: dict[str, dict] = {}
    backward_symbols = 0
    backward_downloads = 0

    for i, symbol in enumerate(symbols, 1):
        window_dates = load_window_dates(symbol)
        first_in = min(window_dates)
        all_relevant_dates = set(window_dates)
        boundary_checked = False
        boundary_download_count = 0

        if first_in > WINDOW_START and originals[symbol] < WINDOW_START:
            boundary_checked = True
            backward_symbols += 1
            prior, boundary_download_count = backward_boundary_context(
                symbol, originals[symbol], first_in
            )
            backward_downloads += boundary_download_count
            all_relevant_dates.update(prior)

        gaps = find_gaps(all_relevant_dates)
        # Only gaps whose resumption can affect the study are relevant. Prior
        # context exists solely to identify a boundary-crossing gap.
        gaps = [g for g in gaps if g.first_post_gap >= WINDOW_START]

        effective = originals[symbol]
        if gaps:
            effective = max(g.first_post_gap for g in gaps)

        symbol_results[symbol] = {
            "symbol": symbol,
            "original_first_trading_day": originals[symbol].isoformat(),
            "effective_first_trading_day": effective.isoformat(),
            "first_in_window_observation": first_in.isoformat(),
            "boundary_backward_check_performed": boundary_checked,
            "backward_archives_downloaded": boundary_download_count,
            "gaps": [
                {
                    "last_pre_gap_date": g.last_pre_gap.isoformat(),
                    "first_post_gap_date": g.first_post_gap.isoformat(),
                    "gap_length_missing_calendar_days": g.missing_calendar_days,
                    "cause": DOCUMENTED_CAUSES.get(symbol, "undetermined — continuity signature only"),
                }
                for g in gaps
            ],
        }
        print(
            f"[{i:03d}/{len(symbols)}] {symbol}: "
            f"first_window={first_in} gaps={len(gaps)} effective={effective}"
            + (f" backward_fetches={boundary_download_count}" if boundary_checked else "")
        )

    flagged = [r for r in symbol_results.values() if r["gaps"]]
    resets = [r for r in symbol_results.values() if r["effective_first_trading_day"] != r["original_first_trading_day"]]

    # Every symbol not requiring a boundary backward check and not carrying a
    # detected >=7-day gap needs no further continuity work for this frozen
    # downstream scope. Its effective start is either already known from the
    # frozen provenance or is on/before the window and no later reset was found.
    no_further_work = [
        r for r in symbol_results.values()
        if not r["boundary_backward_check_performed"] and not r["gaps"]
    ]

    eligibility_changes: list[dict] = []
    membership_changes: list[dict] = []

    for month_key, month in universe["months"].items():
        formation = formation_date(month)
        ranking = month["full_eligible_ranking"]
        old_top15 = [r["symbol"] for r in ranking[:15]]
        corrected_rows: list[dict] = []

        for row in ranking:
            symbol = row["symbol"]
            effective = date.fromisoformat(symbol_results[symbol]["effective_first_trading_day"]) if symbol in symbol_results else date.fromisoformat(row["first_trading_day"])
            corrected_age = (formation - effective).days
            old_age_eligible = row["contract_age_days"] >= AGE_GATE_DAYS
            new_age_eligible = corrected_age >= AGE_GATE_DAYS

            if old_age_eligible != new_age_eligible:
                eligibility_changes.append({
                    "formation_month": month_key,
                    "symbol": symbol,
                    "original_first_trading_day": row["first_trading_day"],
                    "effective_first_trading_day": effective.isoformat(),
                    "original_contract_age_days": row["contract_age_days"],
                    "corrected_contract_age_days": corrected_age,
                    "original_age_eligible": old_age_eligible,
                    "corrected_age_eligible": new_age_eligible,
                    "original_rank": row["rank"],
                })

            if new_age_eligible:
                corrected_rows.append(row)

        # The effective-date rule only moves starts later, never earlier, so it
        # can only REMOVE rows from the frozen eligible ranking. No contract
        # absent from full_eligible_ranking can become newly age-eligible.
        new_top15 = [r["symbol"] for r in corrected_rows[:15]]
        if new_top15 != old_top15:
            membership_changes.append({
                "formation_month": month_key,
                "original_top15": old_top15,
                "corrected_top15": new_top15,
                "removed": [s for s in old_top15 if s not in new_top15],
                "added": [s for s in new_top15 if s not in old_top15],
            })

    pump_only = len(resets) == 1 and resets[0]["symbol"] == "PUMPUSDT"

    result = {
        "schema_version": 1,
        "deliverable": "A-2 Step 2 — 7-day continuity audit",
        "status": "COMPLETE",
        "frozen_inputs": {
            "examination_set_count": len(symbols),
            "examination_set_sha256": EXPECTED_EXAM_SHA256,
            "audit_window_start": WINDOW_START.isoformat(),
            "audit_window_end": WINDOW_END.isoformat(),
            "gap_threshold_missing_calendar_days": GAP_THRESHOLD_DAYS,
            "age_gate_days": AGE_GATE_DAYS,
            "age_gate_operator": ">=",
        },
        "rule": {
            "effective_first_trading_day": (
                "first trading day of the latest continuous trading run reaching the study period, "
                "where a run is broken by >= 7 consecutive calendar days with no valid trading observations"
            ),
            "original_first_trading_day_preserved": True,
        },
        "counts": {
            "symbols_audited": len(symbols),
            "symbols_with_detected_gap": len(flagged),
            "symbols_with_effective_start_reset": len(resets),
            "symbols_resolved_by_backward_fetch": backward_symbols,
            "backward_archives_downloaded": backward_downloads,
            "symbols_requiring_no_further_work": len(no_further_work),
            "eligibility_changes": len(eligibility_changes),
            "formation_months_with_top15_membership_change": len(membership_changes),
        },
        "pumpusdt_is_only_reset": pump_only,
        "flagged_symbols": flagged,
        "effective_start_resets": resets,
        "eligibility_changes": eligibility_changes,
        "top15_membership_changes": membership_changes,
        "symbols": [symbol_results[s] for s in symbols],
        "prohibitions_observed": [
            "A-2 detector not run",
            "No weekend/volatility/volume detector statistics computed",
            "No strategy performance computed or inspected",
            "No Stage 2 work performed",
            "universe.json not modified",
        ],
    }

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print("\n=== A-2 CONTINUITY AUDIT SUMMARY ===")
    for k, v in result["counts"].items():
        print(f"{k}: {v}")
    print(f"PUMPUSDT is the only reset: {pump_only}")

    if flagged:
        print("\nFLAGGED SYMBOLS")
        for r in flagged:
            print(
                f"{r['symbol']}: original={r['original_first_trading_day']} "
                f"effective={r['effective_first_trading_day']}"
            )
            for g in r["gaps"]:
                print(
                    f"  {g['last_pre_gap_date']} -> {g['first_post_gap_date']} "
                    f"missing_days={g['gap_length_missing_calendar_days']} cause={g['cause']}"
                )
    else:
        print("\nFLAGGED SYMBOLS: none")

    print("\nELIGIBILITY CHANGES")
    if eligibility_changes:
        for c in eligibility_changes:
            print(
                f"{c['formation_month']} {c['symbol']}: "
                f"age {c['original_contract_age_days']} -> {c['corrected_contract_age_days']}; "
                f"eligible {c['original_age_eligible']} -> {c['corrected_age_eligible']}; "
                f"rank={c['original_rank']}"
            )
    else:
        print("none")

    print("\nTOP-15 MEMBERSHIP CHANGES")
    if membership_changes:
        for c in membership_changes:
            print(f"{c['formation_month']}: removed={c['removed']} added={c['added']}")
    else:
        print("none")

    print(f"\nWrote {OUTPUT}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AuditError as e:
        print(f"HALT: {e}", file=sys.stderr)
        sys.exit(2)
