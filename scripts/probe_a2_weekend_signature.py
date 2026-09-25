#!/usr/bin/env python3
"""A-2 weekend-signature validation probe.

VALIDATION ONLY. This script does not build, modify, or classify the wider
research universe.

Point-in-time rationale
-----------------------
Instrument classification is a static property of what an instrument represents,
not a time-varying trading signal and not a return-predictive feature. Computing
the classification once over the full available archive window and applying that
static classification across study months therefore does not constitute
look-ahead for the trading research.

The labelled set, date window, primary partition, metrics, sample minima, and
separation gate below are pre-registered. They must not be tuned after observing
the probe results.

PAXG and XAUT are named A-2 exclusions/overrides and are deliberately not part
of this signature-validation labelled set.
"""
from __future__ import annotations

import csv
import io
import math
import re
import statistics
import sys
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

CACHE = Path("_stage1_cache")
ROOT = Path("data/futures/um")
START = date(2025, 7, 1)
END = date(2026, 8, 31)
MIN_SATURDAYS = 8
MIN_WEEKDAYS = 20

NON_DIGITAL = (
    "XAUUSDT",
    "XAGUSDT",
    "CLUSDT",
    "BZUSDT",
    "MUUSDT",
    "SNDKUSDT",
    "SKHYNIXUSDT",
    "SOXLUSDT",
    "SPCXUSDT",
)

DIGITAL = (
    "SOLUSDT",
    "XRPUSDT",
    "DOGEUSDT",
    "BNBUSDT",
    "ADAUSDT",
    "SUIUSDT",
    "ZECUSDT",
    "HYPEUSDT",
    "1000PEPEUSDT",
    "ENAUSDT",
    "PUMPUSDT",
    "HOLOUSDT",
)

LABELS = {s: "NON-DIGITAL" for s in NON_DIGITAL}
LABELS.update({s: "DIGITAL" for s in DIGITAL})
LABELLED_SET = NON_DIGITAL + DIGITAL

MONTH_RE = re.compile(r"-(\d{4})-(\d{2})\.zip$")


@dataclass(frozen=True)
class Day:
    day: date
    range_pct: float
    quote_volume: float


@dataclass(frozen=True)
class Result:
    symbol: str
    label: str
    r_range: float
    r_volume: float
    saturday_n: int
    weekday_n: int
    median_saturday_range: float
    median_weekday_range: float
    secondary_r_range: float
    secondary_r_volume: float
    weekend_n: int


def ts_ms(value: str) -> int:
    """Normalize Binance millisecond/microsecond open timestamps to ms."""
    n = int(float(value))
    return n // 1000 if n > 10**14 else n


def month_from_name(path: Path) -> date | None:
    m = MONTH_RE.search(path.name)
    return date(int(m.group(1)), int(m.group(2)), 1) if m else None


def candidate_archives(symbol: str) -> list[Path]:
    """Return cached monthly 1d ZIPs in the pre-registered date window."""
    directory = CACHE / ROOT / "monthly" / "klines" / symbol / "1d"
    if not directory.is_dir():
        return []
    wanted_first = date(START.year, START.month, 1)
    wanted_last = date(END.year, END.month, 1)
    out: list[Path] = []
    for p in directory.glob("*.zip"):
        month = month_from_name(p)
        if month is not None and wanted_first <= month <= wanted_last:
            out.append(p)
    return sorted(out)


def csv_rows(blob_path: Path):
    with zipfile.ZipFile(blob_path) as z:
        names = [n for n in z.namelist() if n.lower().endswith(".csv")]
        if len(names) != 1:
            raise RuntimeError(f"{blob_path}: expected exactly one CSV, found {names}")
        with z.open(names[0]) as raw:
            with io.TextIOWrapper(raw, encoding="utf-8") as text:
                yield from csv.reader(text)


def load_days(symbol: str) -> list[Day]:
    archives = candidate_archives(symbol)
    if not archives:
        raise RuntimeError(
            f"{symbol}: no cached monthly 1d archives found under "
            f"{CACHE / ROOT / 'monthly' / 'klines' / symbol / '1d'}"
        )

    by_date: dict[date, Day] = {}
    for archive in archives:
        for row in csv_rows(archive):
            # Binance archive CSVs may include a header. Data rows have numeric
            # open timestamps in column 0.
            if len(row) < 8:
                continue
            try:
                opened_ms = ts_ms(row[0].strip())
                opened = datetime.fromtimestamp(opened_ms / 1000, tz=timezone.utc)
                open_price = float(row[1])
                high = float(row[2])
                low = float(row[3])
                quote_volume = float(row[7])
            except (ValueError, OverflowError):
                continue

            d = opened.date()
            if not (START <= d <= END):
                continue
            if open_price <= 0:
                raise RuntimeError(f"{symbol} {d}: non-positive open price {open_price}")
            if high < low:
                raise RuntimeError(f"{symbol} {d}: high {high} < low {low}")
            if quote_volume < 0:
                raise RuntimeError(f"{symbol} {d}: negative quote volume {quote_volume}")

            # Partition is derived strictly from the kline OPEN TIME in UTC.
            day = Day(
                day=d,
                range_pct=(high - low) / open_price,
                quote_volume=quote_volume,
            )
            if d in by_date:
                raise RuntimeError(f"{symbol}: duplicate 1d kline for UTC date {d}")
            by_date[d] = day

    return [by_date[d] for d in sorted(by_date)]


def mean_ratio(numer: list[float], denom: list[float], what: str) -> float:
    if not numer or not denom:
        raise RuntimeError(f"{what}: empty partition")
    denominator = statistics.fmean(denom)
    if denominator <= 0:
        raise RuntimeError(f"{what}: denominator mean is not positive")
    return statistics.fmean(numer) / denominator


def compute(symbol: str) -> Result:
    days = load_days(symbol)

    # Python weekday(): Monday=0 ... Saturday=5, Sunday=6.
    saturday = [x for x in days if x.day.weekday() == 5]
    weekdays = [x for x in days if x.day.weekday() <= 4]
    weekend = [x for x in days if x.day.weekday() >= 5]

    if len(saturday) < MIN_SATURDAYS or len(weekdays) < MIN_WEEKDAYS:
        raise InsufficientData(
            symbol=symbol,
            saturday_n=len(saturday),
            weekday_n=len(weekdays),
        )

    sat_range = [x.range_pct for x in saturday]
    wd_range = [x.range_pct for x in weekdays]
    we_range = [x.range_pct for x in weekend]
    sat_volume = [x.quote_volume for x in saturday]
    wd_volume = [x.quote_volume for x in weekdays]
    we_volume = [x.quote_volume for x in weekend]

    return Result(
        symbol=symbol,
        label=LABELS[symbol],
        r_range=mean_ratio(sat_range, wd_range, f"{symbol} R_range"),
        r_volume=mean_ratio(sat_volume, wd_volume, f"{symbol} R_volume"),
        saturday_n=len(saturday),
        weekday_n=len(weekdays),
        median_saturday_range=statistics.median(sat_range),
        median_weekday_range=statistics.median(wd_range),
        secondary_r_range=mean_ratio(we_range, wd_range, f"{symbol} secondary R_range"),
        secondary_r_volume=mean_ratio(we_volume, wd_volume, f"{symbol} secondary R_volume"),
        weekend_n=len(weekend),
    )


class InsufficientData(RuntimeError):
    def __init__(self, symbol: str, saturday_n: int, weekday_n: int):
        self.symbol = symbol
        self.saturday_n = saturday_n
        self.weekday_n = weekday_n
        super().__init__(
            f"{symbol}: UNCLASSIFIED — Saturdays={saturday_n} "
            f"(minimum {MIN_SATURDAYS}), weekdays={weekday_n} "
            f"(minimum {MIN_WEEKDAYS})"
        )


def f(x: float) -> str:
    return f"{x:.8f}"


def print_table(results: list[Result]) -> None:
    headers = (
        "symbol",
        "label",
        "R_range",
        "R_volume",
        "Saturday_n",
        "weekday_n",
        "median_Sat_range_pct",
        "median_weekday_range_pct",
        "secondary_SatSun_R_range",
        "secondary_SatSun_R_volume",
    )
    print(" | ".join(headers))
    print(" | ".join("-" * len(h) for h in headers))
    for r in results:
        print(
            " | ".join(
                (
                    r.symbol,
                    r.label,
                    f(r.r_range),
                    f(r.r_volume),
                    str(r.saturday_n),
                    str(r.weekday_n),
                    f(r.median_saturday_range),
                    f(r.median_weekday_range),
                    f(r.secondary_r_range),
                    f(r.secondary_r_volume),
                )
            )
        )


def main() -> int:
    print("A-2 WEEKEND SIGNATURE VALIDATION PROBE")
    print(f"window_utc={START.isoformat()}..{END.isoformat()}")
    print("primary_partition=Saturday UTC vs Monday-Friday UTC")
    print("secondary_partition=Saturday+Sunday UTC vs Monday-Friday UTC (reporting only)")
    print()

    results: list[Result] = []
    insufficient: list[InsufficientData] = []

    # Compute all rows first so a minimum-data failure is visible for every
    # affected labelled symbol. No gate/threshold computation is allowed after
    # any minimum-data failure.
    for symbol in LABELLED_SET:
        try:
            results.append(compute(symbol))
        except InsufficientData as exc:
            insufficient.append(exc)
        except Exception as exc:
            print(f"ERROR: {symbol}: {exc}", file=sys.stderr)
            print("FAIL — probe aborted; no thresholds computed.", file=sys.stderr)
            return 2

    if results:
        print_table(results)
        print()

    if insufficient:
        for exc in insufficient:
            print(str(exc), file=sys.stderr)
        print(
            "FAIL — minimum-data gate failed. Probe halted; "
            "no separation gate or thresholds computed.",
            file=sys.stderr,
        )
        return 3

    digital = [r for r in results if r.label == "DIGITAL"]
    non_digital = [r for r in results if r.label == "NON-DIGITAL"]

    min_digital_range = min(r.r_range for r in digital)
    max_non_digital_range = max(r.r_range for r in non_digital)
    min_digital_volume = min(r.r_volume for r in digital)
    max_non_digital_volume = max(r.r_volume for r in non_digital)

    margin_range = min_digital_range / max_non_digital_range
    margin_volume = min_digital_volume / max_non_digital_volume

    range_ordered = min_digital_range > max_non_digital_range
    volume_ordered = min_digital_volume > max_non_digital_volume
    range_margin_ok = margin_range >= 3.0
    volume_margin_ok = margin_volume >= 3.0

    print("SEPARATION GATE")
    print(f"R_range min_digital={f(min_digital_range)}")
    print(f"R_range max_non_digital={f(max_non_digital_range)}")
    print(f"R_range separation_margin={f(margin_range)}x")
    print(f"R_volume min_digital={f(min_digital_volume)}")
    print(f"R_volume max_non_digital={f(max_non_digital_volume)}")
    print(f"R_volume separation_margin={f(margin_volume)}x")
    print()

    failures: list[str] = []
    if not range_ordered:
        failures.append(
            "R_range ordering failed: not every DIGITAL exceeds every NON-DIGITAL"
        )
    if not volume_ordered:
        failures.append(
            "R_volume ordering failed: not every DIGITAL exceeds every NON-DIGITAL"
        )
    if not range_margin_ok:
        failures.append("R_range separation margin is below 3.0x")
    if not volume_margin_ok:
        failures.append("R_volume separation margin is below 3.0x")

    if failures:
        print("FAIL")
        for reason in failures:
            print(f"- {reason}")
        print("Probe halted; thresholds were not computed.")
        return 4

    # Thresholds are authorized for proposal ONLY after every gate condition
    # above passes. They are not applied to the wider universe here.
    threshold_range = math.sqrt(max_non_digital_range * min_digital_range)
    threshold_volume = math.sqrt(max_non_digital_volume * min_digital_volume)

    print("PASS")
    print("PROPOSED THRESHOLDS — NOT APPLIED TO UNIVERSE")
    print(
        f"R_range observed_gap=[{f(max_non_digital_range)}, "
        f"{f(min_digital_range)}], geometric_threshold={f(threshold_range)}"
    )
    print(
        f"R_volume observed_gap=[{f(max_non_digital_volume)}, "
        f"{f(min_digital_volume)}], geometric_threshold={f(threshold_volume)}"
    )
    print(
        "Three-zone classifier remains unauthorised pending reviewer/user ruling; "
        "no universe classification was performed."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
