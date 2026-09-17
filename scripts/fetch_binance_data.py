#!/usr/bin/env python3
"""
Download Binance USDT-M futures 1m klines + funding rates for one symbol,
verify every file against Binance's SHA256 checksum, merge into single
gzip CSVs, and write a data manifest (timestamps, gaps, missing months).

Usage:
  python fetch_binance_data.py --symbol ENAUSDT --start 2024-09 --end 2026-08 --out data
Standard library only.
"""
import argparse
import csv
import gzip
import hashlib
import io
import json
import sys
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

BASE = "https://data.binance.vision/data/futures/um/monthly"
KLINE_COLS = ["open_time", "open", "high", "low", "close", "volume", "close_time",
              "quote_volume", "count", "taker_buy_volume", "taker_buy_quote_volume", "ignore"]
MINUTE_MS = 60_000


def months(start, end):
    y, m = map(int, start.split("-"))
    ey, em = map(int, end.split("-"))
    while (y, m) <= (ey, em):
        yield f"{y:04d}-{m:02d}"
        m += 1
        if m == 13:
            y, m = y + 1, 1


def fetch(url, retries=5):
    err = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=120) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            err = e
        except Exception as e:  # network hiccup: retry with backoff
            err = e
        time.sleep(2 ** attempt)
    raise RuntimeError(f"Failed after {retries} attempts: {url} ({err})")


def download_verified(url):
    """Return CSV rows from a verified monthly zip, or None if the month doesn't exist."""
    blob = fetch(url)
    if blob is None:
        return None
    checksum = fetch(url + ".CHECKSUM")
    if checksum is None:
        raise RuntimeError(f"Missing checksum for {url}")
    expected = checksum.decode().split()[0].lower()
    actual = hashlib.sha256(blob).hexdigest()
    if actual != expected:
        raise RuntimeError(f"Checksum mismatch for {url}")
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        name = next(n for n in z.namelist() if n.endswith(".csv"))
        text = z.read(name).decode()
    return list(csv.reader(io.StringIO(text)))


def to_ms(value):
    ts = int(float(value))
    return ts // 1000 if ts > 10**14 else ts  # microseconds -> milliseconds


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", required=True)
    p.add_argument("--start", required=True, help="YYYY-MM")
    p.add_argument("--end", required=True, help="YYYY-MM")
    p.add_argument("--out", default="data")
    a = p.parse_args()

    sym = a.symbol.upper()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    kl_path = out / f"{sym}_futures_1m_{a.start}_{a.end}.csv.gz"
    fr_path = out / f"{sym}_funding_{a.start}_{a.end}.csv.gz"

    manifest = {"symbol": sym, "market": "Binance USDT-M perpetual futures",
                "window_months": [a.start, a.end], "klines": {}, "funding": {},
                "missing_months": {"klines": [], "funding": []}}

    # ---- 1m klines ----
    rows_total, first, prev, gaps, dupes = 0, None, None, [], 0
    with gzip.open(kl_path, "wt", newline="") as f:
        w = csv.writer(f)
        w.writerow(KLINE_COLS)
        for mo in months(a.start, a.end):
            url = f"{BASE}/klines/{sym}/1m/{sym}-1m-{mo}.zip"
            rows = download_verified(url)
            if rows is None:
                print(f"[klines]  {mo}: not available")
                manifest["missing_months"]["klines"].append(mo)
                continue
            n = 0
            for r in rows:
                if not r or not r[0].strip().lstrip("-").isdigit():
                    continue  # header row (newer files) or blank
                t = to_ms(r[0])
                if prev is not None:
                    if t <= prev:
                        dupes += 1
                        continue
                    if t - prev > MINUTE_MS:
                        gaps.append({"from": iso(prev), "to": iso(t),
                                     "missing_bars": (t - prev) // MINUTE_MS - 1})
                r[0], r[6] = str(t), str(to_ms(r[6]))
                w.writerow(r[:12])
                first = t if first is None else first
                prev = t
                n += 1
            rows_total += n
            print(f"[klines]  {mo}: {n:,} bars verified")

    manifest["klines"] = {
        "file": kl_path.name, "rows": rows_total,
        "first_bar": iso(first) if first else None,
        "last_bar": iso(prev) if prev else None,
        "gap_count": len(gaps),
        "missing_bars_total": sum(g["missing_bars"] for g in gaps),
        "largest_gaps": sorted(gaps, key=lambda g: -g["missing_bars"])[:20],
        "duplicates_dropped": dupes,
        "sha256": sha256_file(kl_path),
    }

    # ---- funding rates ----
    fr_rows, fr_first, fr_last, header_written = 0, None, None, False
    with gzip.open(fr_path, "wt", newline="") as f:
        w = csv.writer(f)
        for mo in months(a.start, a.end):
            url = f"{BASE}/fundingRate/{sym}/{sym}-fundingRate-{mo}.zip"
            rows = download_verified(url)
            if rows is None:
                print(f"[funding] {mo}: not available")
                manifest["missing_months"]["funding"].append(mo)
                continue
            n = 0
            for r in rows:
                if not r:
                    continue
                if not r[0].strip().isdigit():
                    if not header_written:
                        w.writerow(r)
                        header_written = True
                    continue
                t = to_ms(r[0])
                r[0] = str(t)
                w.writerow(r)
                fr_first = t if fr_first is None else fr_first
                fr_last = t
                n += 1
            fr_rows += n
            print(f"[funding] {mo}: {n} settlements verified")

    manifest["funding"] = {
        "file": fr_path.name, "rows": fr_rows,
        "first": iso(fr_first) if fr_first else None,
        "last": iso(fr_last) if fr_last else None,
        "sha256": sha256_file(fr_path),
    }

    (out / "data_manifest.json").write_text(json.dumps(manifest, indent=2))
    print(json.dumps({k: v for k, v in manifest["klines"].items() if k != "largest_gaps"}, indent=2))

    if rows_total == 0:
        sys.exit("No kline data downloaded. Check symbol and months.")


if __name__ == "__main__":
    main()
