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

BACKFILL_DAYS = 40


def fetch_batch(symbol, start_ms, end_ms):
    params = urllib.parse.urlencode({
        "symbol": symbol,
        "interval": INTERVAL,
        "limit": LIMIT,
        "startTime": start_ms,
        "endTime": end_ms,
    })

    request = urllib.request.Request(
        f"{BASE_URL}?{params}",
        headers={"User-Agent": "HOLO-PUMP-Quant-Lab/2.0"},
    )

    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def convert_klines(symbol, raw):
    rows = []

    for k in raw:
        rows.append({
            "symbol": symbol,
            "open_time_utc": datetime.fromtimestamp(
                k[0] / 1000,
                tz=timezone.utc
            ).isoformat(),
            "open_time_ms": k[0],
            "open": k[1],
            "high": k[2],
            "low": k[3],
            "close": k[4],
            "volume": k[5],
            "close_time_utc": datetime.fromtimestamp(
                k[6] / 1000,
                tz=timezone.utc
            ).isoformat(),
            "close_time_ms": k[6],
            "quote_volume": k[7],
            "trade_count": k[8],
            "taker_buy_base_volume": k[9],
            "taker_buy_quote_volume": k[10],
        })

    return rows


def load_existing(symbol):
    path = OUTPUT_DIR / f"{symbol}-1m.json"

    if not path.exists():
        return []

    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def fetch_range(symbol, start_ms, end_ms):
    all_rows = []
    cursor = start_ms

    while cursor <= end_ms:
        raw = fetch_batch(symbol, cursor, end_ms)

        if not raw:
            break

        rows = convert_klines(symbol, raw)
        all_rows.extend(rows)

        last_open_ms = rows[-1]["open_time_ms"]
        next_cursor = last_open_ms + 60_000

        if next_cursor <= cursor:
            break

        cursor = next_cursor

        if len(raw) < LIMIT:
            break

        time.sleep(0.15)

    return all_rows


def merge_rows(existing, new_rows):
    merged = {}

    for row in existing:
        merged[row["open_time_ms"]] = row

    for row in new_rows:
        merged[row["open_time_ms"]] = row

    rows = list(merged.values())
    rows.sort(key=lambda x: x["open_time_ms"])

    return rows


def main():
    now = datetime.now(timezone.utc)
    now_ms = int(now.timestamp() * 1000)

    target_start = now - timedelta(days=BACKFILL_DAYS)
    target_start_ms = int(target_start.timestamp() * 1000)

    manifest = {
        "collected_at_utc": now.isoformat(),
        "interval": INTERVAL,
        "backfill_days": BACKFILL_DAYS,
        "symbols": SYMBOLS,
        "status": {},
    }

    for symbol in SYMBOLS:
        print(f"Processing {symbol}...")

        try:
            existing = load_existing(symbol)

            if existing:
                earliest_ms = existing[0]["open_time_ms"]
                latest_ms = existing[-1]["open_time_ms"]

                new_rows = []

                if earliest_ms > target_start_ms:
                    older = fetch_range(
                        symbol,
                        target_start_ms,
                        earliest_ms - 60_000
                    )
                    new_rows.extend(older)

                newer = fetch_range(
                    symbol,
                    latest_ms + 60_000,
                    now_ms
                )
                new_rows.extend(newer)

            else:
                new_rows = fetch_range(
                    symbol,
                    target_start_ms,
                    now_ms
                )

            rows = merge_rows(existing, new_rows)

            output_file = OUTPUT_DIR / f"{symbol}-1m.json"

            with output_file.open("w", encoding="utf-8") as f:
                json.dump(rows, f, indent=2)

            manifest["status"][symbol] = {
                "success": True,
                "rows": len(rows),
                "first_open_time_utc": rows[0]["open_time_utc"],
                "last_open_time_utc": rows[-1]["open_time_utc"],
            }

            print(
                f"{symbol}: {len(rows)} rows "
                f"{rows[0]['open_time_utc']} -> "
                f"{rows[-1]['open_time_utc']}"
            )

        except Exception as exc:
            manifest["status"][symbol] = {
                "success": False,
                "error": str(exc),
            }

            print(f"{symbol} FAILED: {exc}")

    with (OUTPUT_DIR / "manifest.json").open(
        "w",
        encoding="utf-8"
    ) as f:
        json.dump(manifest, f, indent=2)

    failures = [
        symbol
        for symbol, status in manifest["status"].items()
        if not status["success"]
    ]

    if failures:
        raise RuntimeError(
            "Data collection failed for: "
            + ", ".join(failures)
        )

    print("Collection completed successfully.")


if __name__ == "__main__":
    main()
