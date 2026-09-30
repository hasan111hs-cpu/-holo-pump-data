import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_URL = "https://data-api.binance.vision/api/v3/klines"
UTC = timezone.utc

# Frozen Simple signal thresholds
SYMBOL_THRESHOLDS = {
    "PUMPUSDT": 6.5,
    "ENAUSDT": 1.0,
    "HOLOUSDT": 2.0,
}


def get_one_minute(symbol, minute_start):
    start_ms = int(minute_start.timestamp() * 1000)

    params = urllib.parse.urlencode({
        "symbol": symbol,
        "interval": "1m",
        "startTime": start_ms,
        "endTime": start_ms + 59_999,
        "limit": 1,
    })

    url = f"{BASE_URL}?{params}"

    with urllib.request.urlopen(url, timeout=15) as response:
        rows = json.loads(response.read().decode())

    if not rows:
        return None

    row = rows[0]

    if int(row[0]) != start_ms:
        return None

    return row


def get_completed_minute(symbol, minute_start, retries=45, sleep_seconds=2):
    for _ in range(retries):
        row = get_one_minute(symbol, minute_start)

        if row is not None:
            now_ms = int(datetime.now(UTC).timestamp() * 1000)
            close_time_ms = int(row[6])

            if close_time_ms < now_ms:
                return row

        time.sleep(sleep_seconds)

    return None


def main():
    now = datetime.now(UTC)

    # Dubai is UTC+4 year-round. 18:00 UTC = 22:00 Dubai.
    boundary = now.replace(hour=18, minute=0, second=0, microsecond=0)

    if now < boundary:
        boundary -= timedelta(days=1)

    signal_open_minute = boundary - timedelta(days=1)
    signal_close_minute = boundary - timedelta(minutes=1)

    output = {
        "generated_at_utc": None,
        "boundary_utc": boundary.isoformat(),
        "boundary_dubai": (boundary + timedelta(hours=4)).isoformat(),
        "signal_window_start_utc": signal_open_minute.isoformat(),
        "signal_window_end_utc": boundary.isoformat(),
        "status": "OK",
        "symbols": {},
    }

    for symbol, threshold in SYMBOL_THRESHOLDS.items():
        try:
            first = get_one_minute(symbol, signal_open_minute)
            last = get_completed_minute(symbol, signal_close_minute)

            if first is None or last is None:
                output["symbols"][symbol] = {
                    "status": "DATA_ERROR",
                    "threshold_pct": threshold,
                    "error": "Exact signal open or completed final minute unavailable",
                }
                output["status"] = "DATA_ERROR"
                continue

            candle_open = float(first[1])
            candle_close = float(last[4])
            return_pct = (candle_close / candle_open - 1) * 100
            qualified = return_pct >= threshold

            output["symbols"][symbol] = {
                "status": "OK",
                "open": candle_open,
                "close": candle_close,
                "return_pct": return_pct,
                "threshold_pct": threshold,
                "qualified": qualified,
                "decision": "BUY" if qualified else "NO TRADE",
                "open_minute_utc": signal_open_minute.isoformat(),
                "close_minute_utc": signal_close_minute.isoformat(),
            }

        except Exception as exc:
            output["symbols"][symbol] = {
                "status": "DATA_ERROR",
                "threshold_pct": threshold,
                "error": str(exc),
            }
            output["status"] = "DATA_ERROR"

    output["generated_at_utc"] = datetime.now(UTC).isoformat()

    out_dir = Path("simple_fast")
    out_dir.mkdir(exist_ok=True)

    output_path = out_dir / f"{boundary.strftime('%Y-%m-%d')}.json"
    output_path.write_text(json.dumps(output, indent=2), encoding="utf-8")

    print(json.dumps(output, indent=2))
    print(f"\nSaved: {output_path}")


if __name__ == "__main__":
    main()
