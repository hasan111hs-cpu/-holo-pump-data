import json
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SYMBOLS = ["HOLOUSDT", "PUMPUSDT", "BTCUSDT"]
INTERVAL = "1m"
LIMIT = 1500
BASE_URL = "https://data-api.binance.vision/api/v3/klines"
OUTPUT_DIR = Path("data")
OUTPUT_DIR.mkdir(exist_ok=True)

def fetch_klines(symbol):
    params = urllib.parse.urlencode({"symbol": symbol, "interval": INTERVAL, "limit": LIMIT})
    request = urllib.request.Request(
        f"{BASE_URL}?{params}",
        headers={"User-Agent": "HOLO-PUMP-Quant-Lab/1.0"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))

def convert_klines(symbol, raw):
    rows = []
    for k in raw:
        rows.append({
            "symbol": symbol,
            "open_time_utc": datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc).isoformat(),
            "open_time_ms": k[0],
            "open": k[1], "high": k[2], "low": k[3], "close": k[4], "volume": k[5],
            "close_time_utc": datetime.fromtimestamp(k[6] / 1000, tz=timezone.utc).isoformat(),
            "close_time_ms": k[6],
            "quote_volume": k[7],
            "trade_count": k[8],
            "taker_buy_base_volume": k[9],
            "taker_buy_quote_volume": k[10],
        })
    return rows

def main():
    manifest = {
        "collected_at_utc": datetime.now(timezone.utc).isoformat(),
        "interval": INTERVAL,
        "symbols": SYMBOLS,
        "status": {},
    }
    for symbol in SYMBOLS:
        try:
            rows = convert_klines(symbol, fetch_klines(symbol))
            with (OUTPUT_DIR / f"{symbol}-1m.json").open("w", encoding="utf-8") as f:
                json.dump(rows, f, indent=2)
            manifest["status"][symbol] = {
                "success": True, "rows": len(rows),
                "first_open_time_utc": rows[0]["open_time_utc"],
                "last_open_time_utc": rows[-1]["open_time_utc"],
            }
        except Exception as exc:
            manifest["status"][symbol] = {"success": False, "error": str(exc)}

    with (OUTPUT_DIR / "manifest.json").open("w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    failures = [s for s, status in manifest["status"].items() if not status["success"]]
    if failures:
        raise RuntimeError("Data collection failed for: " + ", ".join(failures))

if __name__ == "__main__":
    main()
