"""
STRATEGY DESIGN SEARCH — maximal in-sample fit across HOLO, PUMP and ENA.

PURPOSE. Hasan has asked for the best-fitting strategy the past can produce, to be
FROZEN and then forward-tested on October 2026 and beyond. The in-sample figures this
produces are NOT predictions and must never be reported as evidence. The forward test
is the evidence. That is what makes maximal fitting legitimate here.

ANTI-OVERFITTING CONSTRAINT. A configuration is only eligible if it is profitable on
ALL THREE assets independently, with a minimum fill count on each. A rule that works on
one coin is almost certainly noise; requiring three roughly independent series to agree
is the strongest available constraint that costs no data.

SEARCHED:
    direction      buy strength (green day) or buy weakness (red day)
    return band    lower and upper bound on the completed day's return
    clv            close location within the day's range
    volume ratio   vs median of prior 5 days
    extreme        distance from the day's high (strength) or low (weakness)
    exits          fixed percentage AND volatility-scaled (multiples of 14-day ATR)

Decision 22:00 Dubai on the completed 22:00->22:00 candle. Entry 22:05. Spot, 1x.
Friction 0.10% per side. SL wins same-minute ties. Time exit at the next 22:00.

    python design_search.py
"""
import json, time, bisect, itertools, urllib.request
from datetime import datetime, timezone, timedelta, date
from pathlib import Path
from statistics import median

OUT = Path("research/design_search")
SYMS = {"ENAUSDT": date(2024, 1, 1), "PUMPUSDT": date(2025, 9, 1), "HOLOUSDT": date(2025, 9, 1)}
MIN = 60_000
CUT_H = 18                  # 22:00 Dubai
ENTRY_H, ENTRY_M = 18, 5    # 22:05 Dubai
FRICTION = 0.001

# ---- search space ------------------------------------------------------------
DIRECTIONS = ("strength", "weakness")
RET_BANDS = {
    "strength": [(0.00, 0.02), (0.00, 0.03), (0.00, 0.05), (0.00, 0.08),
                 (0.01, 0.05), (0.02, 0.08), (0.00, 1.00)],
    "weakness": [(-0.02, 0.00), (-0.04, 0.00), (-0.06, 0.00), (-0.10, 0.00),
                 (-0.06, -0.02), (-0.12, -0.04), (-1.00, 0.00)],
}
CLV_BANDS = [(0.00, 1.00), (0.50, 1.00), (0.65, 1.00), (0.00, 0.35), (0.00, 0.50)]
VOL_MINS = [0.00, 0.80, 1.20, 1.80]
EXTREME_MAX = [0.02, 0.04, 0.08, 1.00]     # dist from high (strength) / low (weakness)

# exits: (label, tp, sl, atr_scaled)   tp/sl are percentages or ATR multiples
EXITS = [
    ("fixed_4.0_2.5", 0.040, -0.025, False),
    ("fixed_6.0_3.0", 0.060, -0.030, False),
    ("fixed_8.0_4.0", 0.080, -0.040, False),
    ("fixed_5.0_5.0", 0.050, -0.050, False),
    ("atr_1.5_1.0",   1.50, -1.00, True),
    ("atr_2.0_1.5",   2.00, -1.50, True),
    ("atr_3.0_2.0",   3.00, -2.00, True),
    ("atr_2.0_1.0",   2.00, -1.00, True),
]

MIN_FILLS_PER_ASSET = 15


def fetch(sym, start_ms, end_ms):
    out, cur = {}, start_ms
    while cur < end_ms:
        url = (f"https://data-api.binance.vision/api/v3/klines?symbol={sym}"
               f"&interval=1m&startTime={cur}&endTime={end_ms}&limit=1000")
        for a in range(4):
            try:
                with urllib.request.urlopen(url, timeout=30) as r:
                    rows = json.loads(r.read())
                break
            except Exception:
                if a == 3:
                    raise
                time.sleep(2 * (a + 1))
        if not rows:
            break
        for k in rows:
            out[int(k[0])] = (float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[7]))
        cur = int(rows[-1][0]) + MIN
        time.sleep(0.10)
        if len(rows) < 1000:
            break
    return out


def ts(d, h, m=0):
    return int(datetime.combine(d, datetime.min.time(), timezone.utc)
               .replace(hour=h, minute=m).timestamp() * 1000)


def build_days(bars, first, last):
    """Per execution date: the completed-candle metrics, plus a precomputed
    monotonic profile of the forward window so any TP/SL can be resolved by
    binary search rather than re-walking 1,380 bars per configuration."""
    days = {}
    d = first + timedelta(days=1)
    while d <= last:
        s, e = ts(d - timedelta(days=1), CUT_H), ts(d, CUT_H)
        w = [bars[t] for t in range(s, e, MIN) if t in bars]
        if len(w) >= 1300:
            o, c = w[0][0], w[-1][3]
            hi, lo = max(b[1] for b in w), min(b[2] for b in w)
            if hi > lo and o > 0 and hi > 0 and lo > 0:
                days[d] = {"open": o, "close": c, "high": hi, "low": lo,
                           "ret": c / o - 1, "clv": (c - lo) / (hi - lo),
                           "dfh": (hi - c) / hi, "dfl": (c - lo) / lo,
                           "range": (hi - lo) / o, "qv": sum(b[4] for b in w)}
        d += timedelta(days=1)

    for d, m in days.items():
        ent = bars.get(ts(d, ENTRY_H, ENTRY_M))
        if not ent:
            m["entry"] = None
            continue
        px = ent[0]
        m["entry"] = px
        s2, e2 = ts(d, ENTRY_H, ENTRY_M) + MIN, ts(d + timedelta(days=1), CUT_H) + MIN
        fwd = [bars[t] for t in range(s2, e2, MIN) if t in bars]
        if len(fwd) < 600:
            m["entry"] = None
            continue
        cmax, cmin, mx, mn = [], [], -1e18, 1e18
        for b in fwd:
            mx = max(mx, b[1] / px); mn = min(mn, b[2] / px)
            cmax.append(mx); cmin.append(mn)
        m["cmax"], m["cmin"] = cmax, cmin           # monotonic: bisect-able
        m["final"] = fwd[-1][3] / px

    # 14-day ATR proxy from the daily true ranges already computed
    ks = sorted(days)
    for i, d in enumerate(ks):
        prior = [days[k]["range"] for k in ks[max(0, i - 14):i]]
        days[d]["atr"] = (sum(prior) / len(prior)) if len(prior) >= 10 else None
        pv = [days[k]["qv"] for k in ks[max(0, i - 5):i]]
        days[d]["vr"] = (days[d]["qv"] / median(pv)) if len(pv) == 5 else None
    return days


def resolve(m, tp, sl):
    """First-touch outcome. SL wins ties. Returns net return after friction."""
    cmax, cmin = m["cmax"], m["cmin"]
    i_tp = bisect.bisect_left(cmax, 1 + tp)
    neg = [-x for x in cmin]                       # cmin is non-increasing
    i_sl = bisect.bisect_left(neg, -(1 + sl))
    n = len(cmax)
    if i_sl < n and (i_sl <= i_tp or i_tp >= n):
        return sl - 2 * FRICTION
    if i_tp < n:
        return tp - 2 * FRICTION
    return m["final"] - 1 - 2 * FRICTION


def evaluate(days, direction, lo, hi, clv_lo, clv_hi, vmin, xmax, tp, sl, atr_scaled):
    rets = []
    for d in sorted(days):
        m = days[d]
        if m.get("entry") is None or m.get("vr") is None:
            continue
        if not (lo <= m["ret"] <= hi):
            continue
        if not (clv_lo <= m["clv"] <= clv_hi):
            continue
        if m["vr"] < vmin:
            continue
        ext = m["dfh"] if direction == "strength" else m["dfl"]
        if ext > xmax:
            continue
        if atr_scaled:
            a = m.get("atr")
            if not a:
                continue
            t, s = tp * a, sl * a
            if t > 0.40 or s < -0.25:              # cap absurd levels in wild regimes
                continue
        else:
            t, s = tp, sl
        rets.append(resolve(m, t, s))
    return rets


def stats(rets):
    if not rets:
        return None
    eq, peak, mdd = 1.0, 1.0, 0.0
    for r in rets:
        eq *= (1 + r); peak = max(peak, eq); mdd = max(mdd, (peak - eq) / peak)
    wins = [r for r in rets if r > 0]
    gl = abs(sum(r for r in rets if r <= 0))
    return {"fills": len(rets), "wins": len(wins), "win_rate": len(wins) / len(rets),
            "total_return": eq - 1.0, "max_drawdown": mdd,
            "profit_factor": (sum(wins) / gl) if gl > 0 else float("inf"),
            "median_trade": sorted(rets)[len(rets) // 2],
            "rets": rets}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).date()
    data = {}
    for sym, start in SYMS.items():
        print(f"[design] fetching {sym} ...", flush=True)
        bars = fetch(sym, ts(start, 0), ts(today, 0))
        first = datetime.fromtimestamp(min(bars) / 1000, tz=timezone.utc).date()
        data[sym] = build_days(bars, first + timedelta(days=7), today - timedelta(days=1))
        print(f"[design]   {len(bars)} bars, {len(data[sym])} usable days "
              f"({first} onward)", flush=True)
        del bars

    combos = []
    for direction in DIRECTIONS:
        for (lo, hi) in RET_BANDS[direction]:
            for (clv_lo, clv_hi) in CLV_BANDS:
                for vmin in VOL_MINS:
                    for xmax in EXTREME_MAX:
                        for (elab, tp, sl, sc) in EXITS:
                            combos.append((direction, lo, hi, clv_lo, clv_hi,
                                           vmin, xmax, elab, tp, sl, sc))
    print(f"[design] evaluating {len(combos)} configurations x 3 assets", flush=True)

    results = []
    for i, c in enumerate(combos):
        direction, lo, hi, clv_lo, clv_hi, vmin, xmax, elab, tp, sl, sc = c
        per, ok = {}, True
        for sym in SYMS:
            st = stats(evaluate(data[sym], direction, lo, hi, clv_lo, clv_hi,
                                vmin, xmax, tp, sl, sc))
            # ELIGIBILITY: profitable on every asset, with enough fills on every asset
            if st is None or st["fills"] < MIN_FILLS_PER_ASSET or st["total_return"] <= 0:
                ok = False
                break
            per[sym] = st
        if not ok:
            continue
        allr = [r for sym in SYMS for r in per[sym]["rets"]]
        comb = stats(allr)
        results.append({
            "direction": direction, "ret_lo": lo, "ret_hi": hi,
            "clv_lo": clv_lo, "clv_hi": clv_hi, "vol_min": vmin, "extreme_max": xmax,
            "exit": elab, "tp": tp, "sl": sl, "atr_scaled": sc,
            "combined": {k: v for k, v in comb.items() if k != "rets"},
            "per_asset": {s: {k: v for k, v in st.items() if k != "rets"}
                          for s, st in per.items()},
            "worst_asset_return": min(st["total_return"] for st in per.values()),
            "worst_asset_pf": min(st["profit_factor"] for st in per.values()),
        })
        if (i + 1) % 500 == 0:
            print(f"[design]   {i+1}/{len(combos)} scanned, {len(results)} eligible",
                  flush=True)

    print(f"[design] {len(results)} configurations profitable on ALL THREE assets")
    if not results:
        print("[design] nothing passed the all-asset constraint.")
        (OUT / "design_result.json").write_text(json.dumps(
            {"eligible": 0, "note": "No configuration was profitable on all three assets."},
            indent=2))
        return

    # Rank by the WORST asset's profit factor: rewards consistency, not one lucky series.
    results.sort(key=lambda r: -r["worst_asset_pf"])
    best = results[0]

    print("\n" + "=" * 68)
    print("BEST CONFIGURATION — ranked by weakest asset's profit factor")
    print("=" * 68)
    print(f"  direction  : buy {best['direction']}")
    print(f"  day return : {best['ret_lo']:+.2%} to {best['ret_hi']:+.2%}")
    print(f"  clv        : {best['clv_lo']} to {best['clv_hi']}")
    print(f"  volume     : >= {best['vol_min']} x 5-day median")
    print(f"  extreme    : <= {best['extreme_max']:.1%} from the day's "
          f"{'high' if best['direction']=='strength' else 'low'}")
    print(f"  exit       : {best['exit']}")
    c = best["combined"]
    print(f"\n  COMBINED   fills {c['fills']}  win {c['win_rate']*100:.1f}%  "
          f"PF {c['profit_factor']:.3f}  return {c['total_return']*100:+.1f}%  "
          f"MDD {c['max_drawdown']*100:.1f}%")
    for s, st in best["per_asset"].items():
        print(f"  {s:10} fills {st['fills']:3}  win {st['win_rate']*100:5.1f}%  "
              f"PF {st['profit_factor']:.3f}  return {st['total_return']*100:+8.1f}%  "
              f"MDD {st['max_drawdown']*100:.1f}%")

    (OUT / "design_result.json").write_text(json.dumps({
        "label": "IN_SAMPLE_DESIGN_SEARCH",
        "purpose": ("Maximal in-sample fit, to be FROZEN and forward-tested. "
                    "In-sample figures are NOT predictions and are not evidence."),
        "configurations_searched": len(combos),
        "eligible_all_three_assets": len(results),
        "ranking_criterion": "highest profit factor on the WEAKEST of the three assets",
        "best": best,
        "top_25": results[:25],
        "caveat": ("Selected by searching thousands of configurations on data already "
                   "seen. The all-asset constraint reduces but does not eliminate "
                   "overfitting. Only forward results carry evidential weight."),
    }, indent=2, default=str))
    print(f"\nwritten: {OUT}/design_result.json")


if __name__ == "__main__":
    main()
