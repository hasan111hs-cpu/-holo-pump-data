import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_URL = "https://data-api.binance.vision/api/v3/klines"
UTC = timezone.utc

# Frozen strategy definitions.
STRATEGIES = {
    "SimpleP":  {"symbol": "PUMPUSDT", "threshold_pct": 6.5, "tp_pct": 4.0,  "sl_pct": 2.5},
    "SimpleP2": {"symbol": "PUMPUSDT", "threshold_pct": 6.5, "tp_pct": 2.5,  "sl_pct": 5.5},
    "SimpleE":  {"symbol": "ENAUSDT",  "threshold_pct": 1.0, "tp_pct": 4.0,  "sl_pct": 2.5},
    "SimpleE2": {"symbol": "ENAUSDT",  "threshold_pct": 1.0, "tp_pct": 6.75, "sl_pct": 6.75},
    "SimpleH":  {"symbol": "HOLOUSDT", "threshold_pct": 2.0, "tp_pct": 4.0,  "sl_pct": 2.5},
    "SimpleH2": {"symbol": "HOLOUSDT", "threshold_pct": 2.0, "tp_pct": 6.0,  "sl_pct": 5.0},
}

SYMBOL_THRESHOLDS = {
    "PUMPUSDT": 6.5,
    "ENAUSDT": 1.0,
    "HOLOUSDT": 2.0,
}


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
    """Fetch [start, end) one-minute Binance Spot klines."""
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000) - 1
    rows = []
    cursor = start_ms

    while cursor <= end_ms:
        batch = get_klines(symbol, cursor, end_ms)
        if not batch:
            break

        rows.extend(batch)
        next_cursor = int(batch[-1][0]) + 60_000
        if next_cursor <= cursor:
            raise RuntimeError("Binance pagination did not advance")
        cursor = next_cursor

        if len(batch) < 1000:
            break
        time.sleep(0.2)

    rows.sort(key=lambda x: int(x[0]))
    return rows


def validate_exact_minutes(rows, start, expected_minutes):
    timestamps = [int(r[0]) for r in rows]
    unique = set(timestamps)
    expected = {
        int(start.timestamp() * 1000) + i * 60_000
        for i in range(expected_minutes)
    }
    return {
        "rows": len(rows),
        "unique_minutes": len(unique),
        "duplicates": len(timestamps) - len(unique),
        "missing_minutes": len(expected - unique),
        "ok": (
            len(unique) == expected_minutes
            and len(timestamps) == expected_minutes
            and len(expected - unique) == 0
        ),
    }


def settle_trade(rows, entry_price, tp_pct, sl_pct, expiry_close):
    tp = entry_price * (1 + tp_pct / 100)
    sl = entry_price * (1 - sl_pct / 100)

    for r in rows:
        minute_open = float(r[1])
        minute_high = float(r[2])
        minute_low = float(r[3])
        minute_close = float(r[4])
        ts = datetime.fromtimestamp(int(r[0]) / 1000, UTC).isoformat()

        hit_tp = minute_high >= tp
        hit_sl = minute_low <= sl

        # With OHLC alone, order inside one 1m candle is unknowable.
        if hit_tp and hit_sl:
            return {
                "status": "AMBIGUOUS_SAME_MINUTE",
                "exit_time_utc": ts,
                "entry_price": entry_price,
                "tp_price": tp,
                "sl_price": sl,
                "minute_open": minute_open,
                "minute_high": minute_high,
                "minute_low": minute_low,
                "minute_close": minute_close,
                "note": "TP and SL both touched within the same 1m candle; intra-minute order is unknowable."
            }

        if hit_tp:
            return {
                "status": "TP",
                "exit_time_utc": ts,
                "entry_price": entry_price,
                "exit_price": tp,
                "return_pct": tp_pct,
                "tp_price": tp,
                "sl_price": sl,
            }

        if hit_sl:
            return {
                "status": "SL",
                "exit_time_utc": ts,
                "entry_price": entry_price,
                "exit_price": sl,
                "return_pct": -sl_pct,
                "tp_price": tp,
                "sl_price": sl,
            }

    forced_return = (expiry_close / entry_price - 1) * 100
    return {
        "status": "TIME_EXIT",
        "entry_price": entry_price,
        "exit_price": expiry_close,
        "return_pct": forced_return,
        "tp_price": tp,
        "sl_price": sl,
    }


def build_signal_file(now, end):
    start = end - timedelta(days=1)
    output = {
        "generated_at_utc": now.isoformat(),
        "window_start_utc": start.isoformat(),
        "window_end_utc": end.isoformat(),
        "status": "OK",
        "symbols": {},
    }

    for symbol, threshold in SYMBOL_THRESHOLDS.items():
        try:
            rows = fetch_range(symbol, start, end)
            check = validate_exact_minutes(rows, start, 1440)

            if not check["ok"]:
                output["symbols"][symbol] = {
                    "status": "DATA_ERROR",
                    **{k: v for k, v in check.items() if k != "ok"},
                    "threshold_pct": threshold,
                }
                output["status"] = "DATA_ERROR"
                continue

            candle_open = float(rows[0][1])
            candle_close = float(rows[-1][4])
            return_pct = (candle_close / candle_open - 1) * 100

            entry_rows = fetch_range(symbol, end, end + timedelta(minutes=1))
            entry_price = float(entry_rows[0][1]) if entry_rows else None

            if entry_price is None:
                output["symbols"][symbol] = {
                    "status": "DATA_ERROR",
                    "error": "22:00 Dubai / 18:00 UTC entry minute unavailable",
                    "threshold_pct": threshold,
                }
                output["status"] = "DATA_ERROR"
                continue

            output["symbols"][symbol] = {
                "status": "OK",
                **{k: v for k, v in check.items() if k != "ok"},
                "open": candle_open,
                "close": candle_close,
                "return_pct": return_pct,
                "threshold_pct": threshold,
                "qualified": return_pct >= threshold,
                "decision": "BUY" if return_pct >= threshold else "NO TRADE",
                "entry_price_2200_dubai": entry_price,
            }

        except Exception as exc:
            output["symbols"][symbol] = {
                "status": "DATA_ERROR",
                "threshold_pct": threshold,
                "error": str(exc),
            }
            output["status"] = "DATA_ERROR"

    return output


def settle_previous_signal(current_end):
    """Settle strategies whose signal/entry occurred at current_end - 24h."""
    entry_boundary = current_end - timedelta(days=1)
    prior_file = Path("simple_live") / f"{entry_boundary.strftime('%Y-%m-%d')}.json"

    if not prior_file.exists():
        return {
            "status": "NO_PRIOR_SIGNAL_FILE",
            "entry_boundary_utc": entry_boundary.isoformat(),
            "source_file": str(prior_file),
            "strategies": {},
        }

    prior = json.loads(prior_file.read_text(encoding="utf-8"))
    results = {
        "status": "OK",
        "entry_boundary_utc": entry_boundary.isoformat(),
        "expiry_boundary_utc": current_end.isoformat(),
        "source_file": str(prior_file),
        "strategies": {},
    }

    cache = {}

    for name, cfg in STRATEGIES.items():
        symbol = cfg["symbol"]
        sig = prior.get("symbols", {}).get(symbol, {})

        if sig.get("status") != "OK":
            results["strategies"][name] = {
                "status": "DATA_ERROR",
                "reason": "Prior signal data not OK",
            }
            results["status"] = "DATA_ERROR"
            continue

        if not sig.get("qualified", False):
            results["strategies"][name] = {"status": "NO_TRADE"}
            continue

        entry_price = sig.get("entry_price_2200_dubai")
        if entry_price is None:
            results["strategies"][name] = {
                "status": "DATA_ERROR",
                "reason": "Missing entry price",
            }
            results["status"] = "DATA_ERROR"
            continue

        try:
            if symbol not in cache:
                path_rows = fetch_range(symbol, entry_boundary, current_end)
                check = validate_exact_minutes(path_rows, entry_boundary, 1440)
                if not check["ok"]:
                    cache[symbol] = ("ERROR", check, None)
                else:
                    expiry_rows = fetch_range(
                        symbol, current_end, current_end + timedelta(minutes=1)
                    )
                    expiry_close = (
                        float(expiry_rows[0][1]) if expiry_rows else None
                    )
                    cache[symbol] = ("OK", check, (path_rows, expiry_close))

            state, check, payload = cache[symbol]

            if state != "OK" or payload[1] is None:
                results["strategies"][name] = {
                    "status": "DATA_ERROR",
                    **{k: v for k, v in check.items() if k != "ok"},
                    "reason": "Incomplete post-entry minute path or expiry price",
                }
                results["status"] = "DATA_ERROR"
                continue

            path_rows, expiry_price = payload
            settled = settle_trade(
                path_rows,
                float(entry_price),
                cfg["tp_pct"],
                cfg["sl_pct"],
                expiry_price,
            )
            settled.update({
                "symbol": symbol,
                "tp_pct": cfg["tp_pct"],
                "sl_pct": cfg["sl_pct"],
                "path_minutes": check["unique_minutes"],
                "missing_minutes": check["missing_minutes"],
                "duplicates": check["duplicates"],
            })
            results["strategies"][name] = settled

        except Exception as exc:
            results["strategies"][name] = {
                "status": "DATA_ERROR",
                "error": str(exc),
            }
            results["status"] = "DATA_ERROR"

    return results


def main():
    now = datetime.now(UTC)
    today_boundary = now.replace(hour=18, minute=0, second=0, microsecond=0)
    end = today_boundary if now >= today_boundary else today_boundary - timedelta(days=1)

    out_dir = Path("simple_live")
    out_dir.mkdir(exist_ok=True)

    # First settle trades entered at the previous boundary.
    settlement = settle_previous_signal(end)
    settlement_dir = Path("simple_settlements")
    settlement_dir.mkdir(exist_ok=True)
    settlement_path = settlement_dir / f"{end.strftime('%Y-%m-%d')}.json"
    settlement_path.write_text(
        json.dumps(settlement, indent=2), encoding="utf-8"
    )

    # Then generate today's new signal file.
    signal = build_signal_file(now, end)
    signal_path = out_dir / f"{end.strftime('%Y-%m-%d')}.json"
    signal_path.write_text(json.dumps(signal, indent=2), encoding="utf-8")

    print("SETTLEMENT")
    print(json.dumps(settlement, indent=2))
    print("\nSIGNAL")
    print(json.dumps(signal, indent=2))
    print(f"\nSaved: {settlement_path}")
    print(f"Saved: {signal_path}")


if __name__ == "__main__":
    main()
