import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

SYMBOLS = ["HOLOUSDT", "PUMPUSDT", "BTCUSDT"]
INTERVAL = "1m"
LIMIT = 1000
BASE_URL = "https://data-api.binance.vision/api/v3/klines"
OUTPUT_DIR = Path("data")
OUTPUT_DIR.mkdir(exist_ok=True)

RETAIN_DAYS = 40          # hard cap on what is KEPT, not just fetched
MAX_GAP_REPAIRS = 20      # per symbol per run


def fetch_batch(symbol, start_ms, end_ms):
    params = urllib.parse.urlencode({
        "symbol": symbol, "interval": INTERVAL, "limit": LIMIT,
        "startTime": start_ms, "endTime": end_ms,
    })
    req = urllib.request.Request(
        f"{BASE_URL}?{params}",
        headers={"User-Agent": "HOLO-PUMP-Quant-Lab/3.0"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def convert(symbol, raw):
    # compact rows only - ISO strings dropped, they are derivable from open_time_ms
    return [{
        "t": k[0], "o": k[1], "h": k[2], "l": k[3], "c": k[4],
        "v": k[5], "qv": k[7], "n": k[8], "tbb": k[9], "tbq": k[10],
    } for k in raw]


def load_existing(symbol):
    p = OUTPUT_DIR / f"{symbol}-1m.json"
    if not p.exists():
        return []
    try:
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if data and "open_time_ms" in data[0]:      # migrate old verbose format
            return [{"t": r["open_time_ms"], "o": r["open"], "h": r["high"],
                     "l": r["low"], "c": r["close"], "v": r["volume"],
                     "qv": r["quote_volume"], "n": r["trade_count"],
                     "tbb": r["taker_buy_base_volume"],
                     "tbq": r["taker_buy_quote_volume"]} for r in data]
        return data
    except Exception:
        return []


def fetch_range(symbol, start_ms, end_ms):
    rows, cursor = [], start_ms
    while cursor <= end_ms:
        raw = fetch_batch(symbol, cursor, end_ms)
        if not raw:
            break
        batch = convert(symbol, raw)
        rows.extend(batch)
        nxt = batch[-1]["t"] + 60_000
        if nxt <= cursor:
            break
        cursor = nxt
        if len(raw) < LIMIT:
            break
        time.sleep(0.15)
    return rows


def find_gaps(rows):
    """Interior missing minutes. These are fatal under the fail-closed rule."""
    gaps, seen = [], sorted(r["t"] for r in rows)
    for a, b in zip(seen, seen[1:]):
        if b - a > 60_000:
            gaps.append((a + 60_000, b - 60_000))
    return gaps


def main():
    now = datetime.now(timezone.utc)
    now_ms = int(now.timestamp() * 1000)
    keep_from_ms = int((now - timedelta(days=RETAIN_DAYS)).timestamp() * 1000)

    manifest = {"collected_at_utc": now.isoformat(), "interval": INTERVAL,
                "retain_days": RETAIN_DAYS, "symbols": SYMBOLS, "status": {}}

    for symbol in SYMBOLS:
        print(f"Processing {symbol}...")
        try:
            merged = {r["t"]: r for r in load_existing(symbol)}

            if merged:
                latest = max(merged)
                for r in fetch_range(symbol, latest + 60_000, now_ms):
                    merged[r["t"]] = r
                earliest = min(merged)
                if earliest > keep_from_ms:
                    for r in fetch_range(symbol, keep_from_ms, earliest - 60_000):
                        merged[r["t"]] = r
            else:
                for r in fetch_range(symbol, keep_from_ms, now_ms):
                    merged[r["t"]] = r

            # trim BEFORE gap repair so we never chase holes we do not keep
            merged = {t: r for t, r in merged.items() if t >= keep_from_ms}

            # repair interior gaps - the bug that made holes permanent
            repaired = 0
            for lo, hi in find_gaps(list(merged.values()))[:MAX_GAP_REPAIRS]:
                print(f"  repairing gap {lo} -> {hi}")
                for r in fetch_range(symbol, lo, hi):
                    merged[r["t"]] = r
                repaired += 1

            rows = sorted(merged.values(), key=lambda x: x["t"])
            remaining = find_gaps(rows)
            missing = sum((b - a) // 60_000 + 1 for a, b in remaining)

            out = OUTPUT_DIR / f"{symbol}-1m.json"
            with out.open("w", encoding="utf-8") as f:
                json.dump(rows, f, separators=(",", ":"))   # no indent

            manifest["status"][symbol] = {
                "success": True, "rows": len(rows),
                "first_open_time_ms": rows[0]["t"], "last_open_time_ms": rows[-1]["t"],
                "gaps_repaired": repaired, "gaps_remaining": len(remaining),
                "missing_minutes": missing,
                "file_mb": round(out.stat().st_size / 1e6, 2),
            }
            print(f"  {len(rows)} rows, {out.stat().st_size/1e6:.1f} MB, "
                  f"{repaired} gaps repaired, {missing} minutes still missing")

        except Exception as exc:
            manifest["status"][symbol] = {"success": False, "error": str(exc)}
            print(f"{symbol} FAILED: {exc}")

    with (OUTPUT_DIR / "manifest.json").open("w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    failed = [s for s, st in manifest["status"].items() if not st["success"]]
    if failed:
        raise RuntimeError("Data collection failed for: " + ", ".join(failed))
    print("Collection completed successfully.")


if __name__ == "__main__":
    main()
