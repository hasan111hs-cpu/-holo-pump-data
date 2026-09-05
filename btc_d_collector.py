import json
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

URL = "https://api.coingecko.com/api/v3/global"
OUTPUT_DIR = Path("data")
OUTPUT_DIR.mkdir(exist_ok=True)
OUTPUT_FILE = OUTPUT_DIR / "BTC-D.json"

def load_existing():
    if not OUTPUT_FILE.exists():
        return []
    try:
        with OUTPUT_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []

def fetch_global():
    request = urllib.request.Request(
        URL,
        headers={
            "User-Agent": "HOLO-PUMP-Quant-Lab/1.0",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))

def main():
    payload = fetch_global()
    data = payload["data"]

    btc_d = float(data["market_cap_percentage"]["btc"])
    updated_at = int(data["updated_at"])
    source_updated_at_utc = datetime.fromtimestamp(
        updated_at, tz=timezone.utc
    ).isoformat()

    snapshot = {
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_updated_at_unix": updated_at,
        "source_updated_at_utc": source_updated_at_utc,
        "btc_dominance_pct": btc_d,
        "total_market_cap_usd": data["total_market_cap"]["usd"],
        "total_volume_usd": data["total_volume"]["usd"],
        "market_cap_change_percentage_24h_usd":
            data["market_cap_change_percentage_24h_usd"],
        "source": "CoinGecko /api/v3/global",
    }

    history = load_existing()
    existing_source_times = {
        row.get("source_updated_at_unix")
        for row in history
        if isinstance(row, dict)
    }

    if updated_at not in existing_source_times:
        history.append(snapshot)
        history.sort(key=lambda x: x["source_updated_at_unix"])
        with OUTPUT_FILE.open("w", encoding="utf-8") as f:
            json.dump(history, f, indent=2)
        print(
            f"Saved BTC.D {btc_d:.4f}% "
            f"at source time {source_updated_at_utc}"
        )
    else:
        print(
            f"No new BTC.D source observation. "
            f"Latest source time: {source_updated_at_utc}"
        )

if __name__ == "__main__":
    main()
