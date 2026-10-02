#!/usr/bin/env python3
"""Backtest of the six frozen Simple strategies on Binance Spot 1m data.

Variants:
  A_old_2200_nofee : entry = open of the 18:00 UTC minute, no fees
  B_2205_nofee     : entry = open of the 18:05 UTC minute, no fees
  C_2205_fee       : entry = open of the 18:05 UTC minute, 0.1% fee per side   (AGREED RULES)
All variants: TP and SL in the same minute counts as SL; time exit at the open of the
next 18:00 UTC minute; one position per strategy at a time; full equity per trade, compounded.
"""
import csv, hashlib, io, json, sys, urllib.error, urllib.request, zipfile
from datetime import datetime, timedelta, timezone

UTC = timezone.utc
BASE = "https://data.binance.vision/data/spot"
START = datetime(2025, 9, 1, tzinfo=UTC)
END = datetime(2026, 10, 1, tzinfo=UTC)          # exclusive: data through 2026-09-30
FEE = 0.001

STRATEGIES = {
    "SimpleP":  {"symbol": "PUMPUSDT", "threshold_pct": 6.5, "tp_pct": 4.0,  "sl_pct": 2.5},
    "SimpleP2": {"symbol": "PUMPUSDT", "threshold_pct": 6.5, "tp_pct": 2.5,  "sl_pct": 5.5},
    "SimpleE":  {"symbol": "ENAUSDT",  "threshold_pct": 1.0, "tp_pct": 4.0,  "sl_pct": 2.5},
    "SimpleE2": {"symbol": "ENAUSDT",  "threshold_pct": 1.0, "tp_pct": 6.75, "sl_pct": 6.75},
    "SimpleH":  {"symbol": "HOLOUSDT", "threshold_pct": 2.0, "tp_pct": 4.0,  "sl_pct": 2.5},
    "SimpleH2": {"symbol": "HOLOUSDT", "threshold_pct": 2.0, "tp_pct": 6.0,  "sl_pct": 5.0},
}
VARIANTS = {
    "A_old_2200_nofee": {"entry_offset_min": 0, "fee": 0.0},
    "B_2205_nofee":     {"entry_offset_min": 5, "fee": 0.0},
    "C_2205_fee":       {"entry_offset_min": 5, "fee": FEE},
}


def fetch_zip(url):
    try:
        with urllib.request.urlopen(url, timeout=180) as r:
            blob = r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    with urllib.request.urlopen(url + ".CHECKSUM", timeout=60) as r:
        expected = r.read().decode().split()[0].lower()
    if hashlib.sha256(blob).hexdigest() != expected:
        raise RuntimeError(f"checksum mismatch {url}")
    return blob


def load_symbol(symbol):
    """Return dict minute_ms -> (open, high, low, close)."""
    bars, months_ok, months_missing = {}, [], []
    y, m = START.year, START.month
    while datetime(y, m, 1, tzinfo=UTC) < END:
        tag = f"{y:04d}-{m:02d}"
        blob = fetch_zip(f"{BASE}/monthly/klines/{symbol}/1m/{symbol}-1m-{tag}.zip")
        if blob is None:
            months_missing.append(tag)
        else:
            months_ok.append(tag)
            with zipfile.ZipFile(io.BytesIO(blob)) as z:
                name = next(n for n in z.namelist() if n.endswith(".csv"))
                for row in csv.reader(io.StringIO(z.read(name).decode())):
                    try:
                        ts = int(row[0])
                    except (ValueError, IndexError):
                        continue
                    if ts > 10**14:
                        ts //= 1000
                    bars[ts] = (float(row[1]), float(row[2]), float(row[3]), float(row[4]))
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return bars, months_ok, months_missing


def ms(dt):
    return int(dt.timestamp() * 1000)


def run_symbol_day(bars, boundary, cfg, variant):
    """Evaluate one boundary. Returns None (no data / not qualified) or a trade dict."""
    o = bars.get(ms(boundary - timedelta(days=1)))
    c = bars.get(ms(boundary - timedelta(minutes=1)))
    if o is None or c is None:
        return {"skip": "signal_data_missing"}
    ret = (c[3] / o[0] - 1) * 100
    if ret < cfg["threshold_pct"]:
        return None
    entry_t = boundary + timedelta(minutes=variant["entry_offset_min"])
    expiry_t = boundary + timedelta(days=1)
    eb = bars.get(ms(entry_t))
    xb = bars.get(ms(expiry_t))
    if eb is None or xb is None:
        return {"skip": "trade_data_missing"}
    entry = eb[0]
    tp = entry * (1 + cfg["tp_pct"] / 100)
    sl = entry * (1 - cfg["sl_pct"] / 100)
    status, exit_price, exit_time, missing = "TIME_EXIT", xb[0], expiry_t, 0
    t = entry_t
    while t < expiry_t:
        b = bars.get(ms(t))
        if b is None:
            missing += 1
        else:
            hit_tp, hit_sl = b[1] >= tp, b[2] <= sl
            if hit_sl:                       # same-minute TP+SL counts as SL
                status, exit_price, exit_time = ("SL_AMBIGUOUS" if hit_tp else "SL"), sl, t
                break
            if hit_tp:
                status, exit_price, exit_time = "TP", tp, t
                break
        t += timedelta(minutes=1)
    f = variant["fee"]
    net = (exit_price * (1 - f)) / (entry * (1 + f)) - 1
    return {
        "boundary_utc": boundary.isoformat(), "signal_return_pct": round(ret, 4),
        "entry_time_utc": entry_t.isoformat(), "entry": entry,
        "status": status, "exit_time_utc": exit_time.isoformat(), "exit": exit_price,
        "gross_pct": round((exit_price / entry - 1) * 100, 4), "net_pct": round(net * 100, 4),
        "missing_minutes": missing,
    }


def summarise(trades):
    eq, peak, mdd = 10000.0, 10000.0, 0.0
    monthly = {}
    for t in trades:
        eq *= 1 + t["net_pct"] / 100
        peak = max(peak, eq)
        mdd = max(mdd, (peak - eq) / peak * 100)
        k = t["boundary_utc"][:7]
        monthly[k] = monthly.get(k, 1.0) * (1 + t["net_pct"] / 100)
    n = len(trades)
    wins = sum(1 for t in trades if t["net_pct"] > 0)
    counts = {}
    for t in trades:
        counts[t["status"]] = counts.get(t["status"], 0) + 1
    # longest losing streak
    streak = worst = 0
    for t in trades:
        streak = streak + 1 if t["net_pct"] <= 0 else 0
        worst = max(worst, streak)
    return {
        "trades": n, "wins": wins, "win_rate_pct": round(wins / n * 100, 2) if n else None,
        "exit_counts": counts,
        "avg_net_pct_per_trade": round(sum(t["net_pct"] for t in trades) / n, 4) if n else None,
        "final_equity": round(eq, 2), "total_return_pct": round((eq / 10000 - 1) * 100, 2),
        "max_drawdown_pct": round(mdd, 2), "longest_losing_streak": worst,
        "monthly_return_pct": {k: round((v - 1) * 100, 2) for k, v in sorted(monthly.items())},
    }


def main():
    out = {"generated_at_utc": datetime.now(UTC).isoformat(),
           "period_utc": [START.isoformat(), END.isoformat()], "data": {}, "results": {}, "trades": {}}
    data = {}
    for symbol in sorted({c["symbol"] for c in STRATEGIES.values()}):
        bars, ok, missing = load_symbol(symbol)
        data[symbol] = bars
        first, last = min(bars), max(bars)
        expected = (last - first) // 60000 + 1
        out["data"][symbol] = {
            "months_loaded": ok, "months_missing": missing, "rows": len(bars),
            "first_minute_utc": datetime.fromtimestamp(first / 1000, UTC).isoformat(),
            "last_minute_utc": datetime.fromtimestamp(last / 1000, UTC).isoformat(),
            "missing_minutes_in_span": expected - len(bars),
        }
        print(symbol, out["data"][symbol], flush=True)

    for name, cfg in STRATEGIES.items():
        bars = data[cfg["symbol"]]
        first = datetime.fromtimestamp(min(bars) / 1000, UTC)
        last = datetime.fromtimestamp(max(bars) / 1000, UTC)
        out["results"][name], out["trades"][name] = {}, {}
        for vname, variant in VARIANTS.items():
            trades, skips, days = [], {}, 0
            b = first.replace(hour=18, minute=0, second=0, microsecond=0) + timedelta(days=2)
            while b + timedelta(days=1) <= last:
                days += 1
                r = run_symbol_day(bars, b, cfg, variant)
                if r and "skip" in r:
                    skips[r["skip"]] = skips.get(r["skip"], 0) + 1
                elif r:
                    trades.append(r)
                b += timedelta(days=1)
            s = summarise(trades)
            s.update({"days_evaluated": days, "skipped": skips,
                      "first_boundary": first.isoformat(), "config": cfg})
            out["results"][name][vname] = s
            out["trades"][name][vname] = trades
            print(name, vname, {k: s[k] for k in ("trades", "win_rate_pct", "total_return_pct", "max_drawdown_pct")}, flush=True)

    with open("research/simple_backtest/results.json", "w") as fh:
        json.dump(out, fh, indent=1)


if __name__ == "__main__":
    sys.exit(main())
