#!/usr/bin/env python3
"""
A-2 classification reconnaissance probe.

Purpose:
- Fetch Binance USD-M Futures exchangeInfo.
- Preserve the raw response exactly.
- Print complete raw records for the ruled probe symbols.
- Print the complete underlyingType / underlyingSubType vocabulary and counts.
- Print SHA256 of the raw response.

This script DOES NOT enumerate the research universe, download archives,
classify the universe, or build universe.json.
"""

import collections
import hashlib
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

URL = "https://fapi.binance.com/fapi/v1/exchangeInfo"
RAW_OUT = Path("research/multicoin_expansion/a2_exchangeInfo_probe_raw.json")

TARGETS = [
    "XAUUSDT", "XAGUSDT", "CLUSDT", "BZUSDT", "MUUSDT",
    "SNDKUSDT", "SKHYNIXUSDT", "SOXLUSDT", "SPCXUSDT", "PAXGUSDT",
    "SOLUSDT", "DOGEUSDT", "ZECUSDT", "HYPEUSDT", "1000PEPEUSDT", "XRPUSDT",
]


def frozen_value(value):
    """Convert a possibly nested descriptor into a stable printable/countable value."""
    if value is None:
        return "<MISSING>"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return str(value)


def main():
    req = urllib.request.Request(
        URL,
        headers={
            "User-Agent": "stage1-a2-classification-probe/1.0",
            "Accept": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            status = getattr(resp, "status", None)
            raw = resp.read()
            print(f"REQUEST_SUCCEEDED: yes")
            print(f"HTTP_STATUS: {status}")
    except urllib.error.HTTPError as e:
        print("REQUEST_SUCCEEDED: no")
        print(f"HTTP_STATUS: {e.code}")
        try:
            body = e.read()
            if body:
                print("HTTP_ERROR_BODY:")
                print(body.decode("utf-8", errors="replace"))
        finally:
            return 2
    except Exception as e:
        print("REQUEST_SUCCEEDED: no")
        print("HTTP_STATUS: unavailable")
        print(f"ERROR_TYPE: {type(e).__name__}")
        print(f"ERROR: {e}")
        return 2

    sha256 = hashlib.sha256(raw).hexdigest()
    RAW_OUT.parent.mkdir(parents=True, exist_ok=True)
    RAW_OUT.write_bytes(raw)

    try:
        payload = json.loads(raw)
    except Exception as e:
        print(f"JSON_PARSE_FAILED: {type(e).__name__}: {e}")
        print(f"RAW_SHA256: {sha256}")
        print(f"RAW_BYTES: {len(raw)}")
        return 3

    symbols = payload.get("symbols")
    if not isinstance(symbols, list):
        print("INVALID_RESPONSE: top-level 'symbols' is not a list")
        print(f"RAW_SHA256: {sha256}")
        print(f"RAW_BYTES: {len(raw)}")
        return 3

    by_symbol = {
        rec.get("symbol"): rec
        for rec in symbols
        if isinstance(rec, dict) and isinstance(rec.get("symbol"), str)
    }

    print()
    print("=== COMPLETE RAW RECORDS FOR PROBE SYMBOLS ===")
    for symbol in TARGETS:
        print()
        print(f"--- {symbol} ---")
        rec = by_symbol.get(symbol)
        if rec is None:
            print("NOT_PRESENT_IN_CURRENT_EXCHANGEINFO")
        else:
            print(json.dumps(rec, ensure_ascii=False, sort_keys=True, indent=2))

    type_counts = collections.Counter()
    subtype_counts = collections.Counter()

    for rec in symbols:
        if not isinstance(rec, dict):
            continue

        type_counts[frozen_value(rec.get("underlyingType"))] += 1

        subtype = rec.get("underlyingSubType")
        if subtype is None:
            subtype_counts["<MISSING>"] += 1
        elif isinstance(subtype, list):
            if not subtype:
                subtype_counts["<EMPTY_LIST>"] += 1
            else:
                for item in subtype:
                    subtype_counts[frozen_value(item)] += 1
        else:
            # Preserve unexpected schema rather than silently normalising it away.
            subtype_counts[f"<NON_LIST>:{frozen_value(subtype)}"] += 1

    print()
    print("=== DISTINCT underlyingType VOCABULARY ===")
    for value, count in sorted(type_counts.items()):
        print(f"{value}\t{count}")

    print()
    print("=== DISTINCT underlyingSubType VOCABULARY ===")
    for value, count in sorted(subtype_counts.items()):
        print(f"{value}\t{count}")

    print()
    print("=== RAW SNAPSHOT ===")
    print(f"RAW_FILE: {RAW_OUT.as_posix()}")
    print(f"RAW_BYTES: {len(raw)}")
    print(f"RAW_SHA256: {sha256}")
    print(f"SYMBOL_RECORD_COUNT: {len(symbols)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
