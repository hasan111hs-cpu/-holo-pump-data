import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_URL = "https://data-api.binance.vision/api/v3/klines"

SYMBOLS = {
    "PUMPUSDT": 6.5,
    "ENAUSDT": 1.0,
    "HOLOUSDT": 2.0,
}

UTC = timezone.utc


def get_klines(symbol, start_ms, end_ms):
    params = urllib.parse.urlencode({
        "symbol": symbol,
        "interval": "1m",
        "startTime": start_ms,
        "endTime": end_ms,
        "limit": 1000,
    })

    url = f"{BASE_URL}?{params}"

    with urllib.request.urlopen(url, timeout=30) as response:
        return json.loads(response.read().decode())


def fetch_range(symbol, start, end):
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000) - 1

    rows = []
    cursor = start_ms

    while cursor <= end_ms:
        batch = get_klines(symbol, cursor, end_ms)

        if not batch:
            break

        rows.extend(batch)

        next_cursor = batch[-1][0] + 60_000

        if next_cursor <= cursor:
            raise RuntimeError("Binance pagination did not advance")

        cursor = next_cursor

        if len(batch) < 1000:
            break

        time.sleep(0.2)

    return rows


def main():
    now = datetime.now(UTC)

    # Most recently completed 18:00 UTC boundary = 22:00 Dubai.
    today_boundary = now.replace(
        hour=18, minute=0, second=0, microsecond=0
    )

    if now < today_boundary:
        end = today_boundary - timedelta(days=1)
    else:
        end = today_boundary

    start = end - timedelta(days=1)

    output = {
        "generated_at_utc": now.isoformat(),
        "window_start_utc": start.isoformat(),
        "window_end_utc": end.isoformat(),
        "status": "OK",
        "symbols": {},
    }

    for symbol, threshold in SYMBOLS.items():
        try:
            rows = fetch_range(symbol, start, end)

            timestamps = [int(row[0]) for row in rows]
            unique_timestamps = sorted(set(timestamps))

            expected = 1440
            duplicates = len(timestamps) - len(unique_timestamps)

            expected_times = {
                int(start.timestamp() * 1000) + i * 60_000
                for i in range(expected)
            }

            missing = sorted(expected_times - set(unique_timestamps))

            if (
                len(unique_timestamps) != expected
                or duplicates != 0
                or missing
            ):
                output["symbols"][symbol] = {
                    "status": "DATA_ERROR",
                    "rows": len(rows),
                    "unique_minutes": len(unique_timestamps),
                    "duplicates": duplicates,
                    "missing_minutes": len(missing),
                    "threshold_pct": threshold,
                }
                output["status"] = "DATA_ERROR"
                continue

            rows.sort(key=lambda x: x[0])

            candle_open = float(rows[0][1])
            candle_close = float(rows[-1][4])

            return_pct = (
                (candle_close / candle_open) - 1
            ) * 100

            # Fetch the first minute beginning exactly at the new
            # 18:00 UTC / 22:00 Dubai boundary for entry reference.
            entry_rows = fetch_range(
                symbol,
                end,
                end + timedelta(minutes=1),
            )

            entry_price = (
                float(entry_rows[0][1])
                if entry_rows
                else None
            )

            qualified = return_pct >= threshold

            output["symbols"][symbol] = {
                "status": "OK",
                "rows": len(rows),
                "unique_minutes": len(unique_timestamps),
                "duplicates": duplicates,
                "missing_minutes": 0,
                "open": candle_open,
                "close": candle_close,
                "return_pct": return_pct,
                "threshold_pct": threshold,
                "qualified": qualified,
                "decision": "BUY" if qualified else "NO TRADE",
                "entry_price_2200_dubai": entry_price,
            }

        except Exception as exc:
            output["symbols"][symbol] = {
                "status": "DATA_ERROR",
                "threshold_pct": threshold,
                "error": str(exc),
            }
            output["status"] = "DATA_ERROR"

    out_dir = Path("simple_live")
    out_dir.mkdir(exist_ok=True)

    filename = end.strftime("%Y-%m-%d") + ".json"
    path = out_dir / filename

    path.write_text(
        json.dumps(output, indent=2),
        encoding="utf-8",
    )

    print(json.dumps(output, indent=2))
    print(f"\nSaved: {path}")


if __name__ == "__main__":
    main()
