"""
MATCHED-VOLUME FORENSIC DIAGNOSTIC — read-only.

Changes nothing. Touches no threshold, no qualification rule, no ledger, no signal.
Writes a single report to diagnostics/matched-volume-<timestamp>.json and prints it.

For execution dates 2026-09-08 .. 2026-09-11, for HOLO and PUMP, it reconstructs the
current matched partial window plus the five priors the signal engine used, reports
every field requested, and independently re-fetches the same ranges from Binance for
cross-check.

    python diagnose_volume.py
"""
import json, urllib.parse, urllib.request, time, statistics
from datetime import datetime, timezone, timedelta, date
from pathlib import Path

DATA = Path("data")
OUT = Path("diagnostics"); OUT.mkdir(exist_ok=True)
BASE_URL = "https://data-api.binance.vision/api/v3/klines"
MIN = 60_000
DATES = ["2026-09-08", "2026-09-09", "2026-09-10", "2026-09-11"]
SYMBOLS = {"HOLO": "HOLOUSDT", "PUMP": "PUMPUSDT"}


def ms(d, h, m=0):
    return int(datetime.combine(d, datetime.min.time(), timezone.utc)
               .replace(hour=h, minute=m).timestamp() * 1000)


def iso(t):
    return datetime.fromtimestamp(t / 1000, tz=timezone.utc).isoformat()


def load_local(sym):
    """Exactly what the signal engine reads."""
    with (DATA / f"{sym}-1m.json").open() as f:
        rows = json.load(f)
    out = {}
    for r in rows:
        if "open_time_ms" in r:                       # legacy verbose schema
            out[r["open_time_ms"]] = dict(
                o=float(r["open"]), h=float(r["high"]), l=float(r["low"]),
                c=float(r["close"]), v=float(r["volume"]),
                qv=float(r["quote_volume"]), tbq=float(r["taker_buy_quote_volume"]),
                schema="legacy")
        else:
            out[r["t"]] = dict(o=float(r["o"]), h=float(r["h"]), l=float(r["l"]),
                               c=float(r["c"]), v=float(r["v"]), qv=float(r["qv"]),
                               tbq=float(r["tbq"]), schema="compact")
    return out, len(rows)


def fetch_binance(symbol, start_ms, end_ms):
    """Independent re-fetch straight from Binance. Audit only."""
    bars, cursor = {}, start_ms
    while cursor <= end_ms:
        q = urllib.parse.urlencode({"symbol": symbol, "interval": "1m", "limit": 1000,
                                    "startTime": cursor, "endTime": end_ms})
        req = urllib.request.Request(f"{BASE_URL}?{q}",
                                     headers={"User-Agent": "HOLO-PUMP-Diagnostic/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = json.loads(r.read().decode())
        if not raw:
            break
        for k in raw:
            bars[k[0]] = dict(o=float(k[1]), h=float(k[2]), l=float(k[3]),
                              c=float(k[4]), v=float(k[5]), qv=float(k[7]),
                              tbq=float(k[10]))
        nxt = raw[-1][0] + MIN
        if nxt <= cursor:
            break
        cursor = nxt
        if len(raw) < 1000:
            break
        time.sleep(0.15)
    return bars


def profile(bars, group_day, label):
    """Full integrity profile of one causal partial window."""
    a = ms(group_day, 18)
    z = ms(group_day + timedelta(days=1), 16, 46)
    need = list(range(a, z + MIN, MIN))
    present = [t for t in need if t in bars]
    missing = [t for t in need if t not in bars]
    w = [bars[t] for t in present]
    qvs = [r["qv"] for r in w]
    nonzero = [q for q in qvs if q > 0]
    # stale-price and duplicate-row checks
    closes = [r["c"] for r in w]
    stale = sum(1 for i in range(1, len(closes)) if closes[i] == closes[i - 1])
    dup_rows = 0
    for i in range(1, len(w)):
        p, c = w[i - 1], w[i]
        if (p["o"], p["h"], p["l"], p["c"], p["qv"]) == (c["o"], c["h"], c["l"], c["c"], c["qv"]) and c["qv"] > 0:
            dup_rows += 1
    return {
        "label": label,
        "group_day": group_day.isoformat(),
        "window_start_utc": iso(a), "window_end_utc": iso(z),
        "expected_minutes": len(need), "actual_minutes": len(present),
        "missing_minutes": len(missing),
        "missing_sample": [iso(t) for t in missing[:5]],
        "duplicate_timestamps": 0,            # dict keys are unique by construction
        "first_bar_utc": iso(present[0]) if present else None,
        "last_bar_utc": iso(present[-1]) if present else None,
        "total_base_volume": sum(r["v"] for r in w),
        "total_quote_volume": sum(qvs),
        "total_taker_buy_quote": sum(r["tbq"] for r in w),
        "zero_volume_bars": len(qvs) - len(nonzero),
        "nonzero_volume_bars": len(nonzero),
        "median_1m_quote_volume": statistics.median(qvs) if qvs else None,
        "max_1m_quote_volume": max(qvs) if qvs else None,
        "consecutive_identical_closes": stale,
        "duplicate_ohlcv_rows": dup_rows,
        "schemas_present": sorted({r.get("schema", "binance-api") for r in w}),
    }


def main():
    report = {"generated_at_utc": datetime.now(timezone.utc).isoformat(),
              "note": "READ-ONLY DIAGNOSTIC. No strategy rule, threshold or ledger touched.",
              "provenance": {}, "dates": {}}

    local = {}
    for key, sym in SYMBOLS.items():
        bars, n = load_local(sym)
        local[key] = bars
        report["provenance"][key] = {
            "source": "collector.py -> data-api.binance.vision /api/v3/klines (native Binance 1m)",
            "reconstructed_from_trades": False,
            "single_source": True,
            "rows_in_file": n,
            "schemas_present": sorted({r.get("schema") for r in bars.values()}),
            "earliest_bar_utc": iso(min(bars)), "latest_bar_utc": iso(max(bars)),
        }

    for ds in DATES:
        exec_date = date.fromisoformat(ds)
        group = exec_date - timedelta(days=1)
        entry = {}
        for key, sym in SYMBOLS.items():
            cur = profile(local[key], group, "current")
            priors = [profile(local[key], group - timedelta(days=i), f"prior-{i}")
                      for i in range(1, 6)]
            prior_sums = [p["total_quote_volume"] for p in priors]
            med = statistics.median(prior_sums)
            entry[key] = {
                "current_window": cur,
                "prior_windows": priors,
                "calculation": {
                    "current_quote_volume_sum": cur["total_quote_volume"],
                    "prior_quote_volume_sums": prior_sums,
                    "median_of_priors": med,
                    "formula": "current_sum / median(prior_five_sums)",
                    "vol_ratio": (cur["total_quote_volume"] / med) if med else None,
                },
            }
            # independent Binance cross-check of the current window and prior-1
            try:
                for lab, g in (("current", group), ("prior-1", group - timedelta(days=1))):
                    ind = fetch_binance(sym, ms(g, 18), ms(g + timedelta(days=1), 16, 46))
                    ip = profile(ind, g, f"independent-{lab}")
                    stored = cur if lab == "current" else priors[0]
                    entry[key].setdefault("independent_check", {})[lab] = {
                        "binance_quote_volume": ip["total_quote_volume"],
                        "collector_quote_volume": stored["total_quote_volume"],
                        "abs_difference": ip["total_quote_volume"] - stored["total_quote_volume"],
                        "ratio_binance_over_collector": (
                            ip["total_quote_volume"] / stored["total_quote_volume"]
                            if stored["total_quote_volume"] else None),
                        "binance_minutes": ip["actual_minutes"],
                        "collector_minutes": stored["actual_minutes"],
                    }
            except Exception as exc:
                entry[key]["independent_check_error"] = str(exc)
        report["dates"][ds] = entry

    path = OUT / f"matched-volume-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    path.write_text(json.dumps(report, indent=2))

    # console summary
    print("=" * 78)
    print("MATCHED-VOLUME DIAGNOSTIC")
    print("=" * 78)
    for key in SYMBOLS:
        p = report["provenance"][key]
        print(f"{key}: {p['rows_in_file']} rows, schemas {p['schemas_present']}, "
              f"{p['earliest_bar_utc'][:10]} -> {p['latest_bar_utc'][:10]}")
    for ds in DATES:
        print(f"\n--- execution date {ds} ---")
        for key in SYMBOLS:
            e = report["dates"][ds][key]; c = e["calculation"]
            cw = e["current_window"]
            print(f"  {key}  vol_ratio {c['vol_ratio']:.4f}"
                  if c["vol_ratio"] is not None else f"  {key}  vol_ratio n/a")
            print(f"    current sum   {c['current_quote_volume_sum']:,.2f}   "
                  f"minutes {cw['actual_minutes']}/{cw['expected_minutes']}  "
                  f"zero-vol bars {cw['zero_volume_bars']}")
            print(f"    prior sums    " + ", ".join(f"{s:,.0f}" for s in c["prior_quote_volume_sums"]))
            print(f"    median        {c['median_of_priors']:,.2f}")
            ic = e.get("independent_check", {})
            for lab, v in ic.items():
                print(f"    binance check {lab}: collector {v['collector_quote_volume']:,.0f} "
                      f"vs binance {v['binance_quote_volume']:,.0f}  "
                      f"ratio {v['ratio_binance_over_collector']}")
    print(f"\nwritten: {path}")


if __name__ == "__main__":
    main()
