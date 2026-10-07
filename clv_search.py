"""
CLV-ONLY STRATEGY SEARCH — target 15% per quarter.

ENTRY: one condition only. The completed 22:00->22:00 Dubai candle closed with
       CLV >= X, where CLV = (close - low) / (high - low).
       No return filter. No volume filter. No distance-from-extreme filter.

SEARCHED: CLV threshold, exit geometry (fixed, ATR-scaled, ATR trailing),
          and holding period (24h / 48h / 72h).

TARGET: >= 15% per quarter = 74.9% per year compounded, required on EVERY asset
        independently. Anything below that on any asset is discarded.

Entry 22:05 Dubai. Spot, 1x. Friction 0.10% per side. SL wins same-minute ties.

HONEST NOTE. An in-sample return threshold measures search effort, not edge. Any
target can be reached by searching long enough. These figures are NOT predictions.
Only forward results carry evidential weight.

    python clv_search.py
"""
import json, time, bisect, urllib.request
from datetime import datetime, timezone, timedelta, date
from pathlib import Path

OUT = Path("research/clv_search")
SYMS = {"ENAUSDT": date(2024, 1, 1), "PUMPUSDT": date(2025, 9, 1), "HOLOUSDT": date(2025, 9, 1)}
MIN = 60_000
CUT_H = 18                  # 22:00 Dubai
ENTRY_H, ENTRY_M = 18, 5    # 22:05 Dubai
FRICTION = 0.001

CLV_MINS = [0.50, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90]
HOLDS = [1, 2, 3]           # calendar days held

FIXED = [(0.04, -0.025), (0.05, -0.05), (0.06, -0.03), (0.08, -0.04),
         (0.10, -0.05), (0.12, -0.06), (0.15, -0.07), (0.20, -0.08)]
ATR_X = [(2.0, -1.5), (3.0, -2.0), (4.0, -2.5), (5.0, -3.0),
         (3.0, -1.5), (4.0, -2.0), (6.0, -3.0), (8.0, -4.0)]
TRAILS = [1.5, 2.0, 2.5, 3.0]          # ATR multiples, trailing from the running high

TARGET_QUARTERLY = 0.15
TARGET_ANNUAL = (1 + TARGET_QUARTERLY) ** 4 - 1
MIN_FILLS = 20


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
            out[int(k[0])] = (float(k[1]), float(k[2]), float(k[3]), float(k[4]))
        cur = int(rows[-1][0]) + MIN
        time.sleep(0.10)
        if len(rows) < 1000:
            break
    return out


def ts(d, h, m=0):
    return int(datetime.combine(d, datetime.min.time(), timezone.utc)
               .replace(hour=h, minute=m).timestamp() * 1000)


def build(bars, first, last):
    """Per execution date: CLV, ATR, and a forward profile covering up to 3 days.

    cmax / cmin are monotonic, so fixed and ATR exits resolve by binary search.
    Trailing exits need a sequential walk, so each trail multiple is resolved once
    here and stored as (hit_index, exit_ratio).
    """
    days = {}
    d = first + timedelta(days=1)
    while d <= last:
        s, e = ts(d - timedelta(days=1), CUT_H), ts(d, CUT_H)
        w = [bars[t] for t in range(s, e, MIN) if t in bars]
        if len(w) >= 1300:
            o, c = w[0][0], w[-1][3]
            hi, lo = max(b[1] for b in w), min(b[2] for b in w)
            if hi > lo and o > 0:
                days[d] = {"clv": (c - lo) / (hi - lo), "range": (hi - lo) / o}
        d += timedelta(days=1)

    ks = sorted(days)
    for i, dd in enumerate(ks):
        prior = [days[k]["range"] for k in ks[max(0, i - 14):i]]
        days[dd]["atr"] = (sum(prior) / len(prior)) if len(prior) >= 10 else None

    for dd in ks:
        m = days[dd]
        ent = bars.get(ts(dd, ENTRY_H, ENTRY_M))
        if not ent:
            m["ok"] = False
            continue
        px = ent[0]
        start = ts(dd, ENTRY_H, ENTRY_M) + MIN
        end = ts(dd + timedelta(days=max(HOLDS)), CUT_H) + MIN

        seq = []                                   # (high_ratio, low_ratio, close_ratio)
        bound_ms = {h: ts(dd + timedelta(days=h), CUT_H) for h in HOLDS}
        bounds = {h: 0 for h in HOLDS}
        t = start
        while t < end:
            b = bars.get(t)
            if b:
                seq.append((b[1] / px, b[2] / px, b[3] / px))
                for h in HOLDS:
                    if t <= bound_ms[h]:
                        bounds[h] = len(seq)
            t += MIN
        if bounds[1] < 600:
            m["ok"] = False
            continue

        cmax, cmin, mx, mn = [], [], -1e18, 1e18
        for h, l, _ in seq:
            mx = max(mx, h); mn = min(mn, l)
            cmax.append(mx); cmin.append(mn)

        finals = {h: (seq[bounds[h] - 1][2] if bounds[h] > 0 else None) for h in HOLDS}

        trail = {}
        atr = m.get("atr")
        if atr:
            for tm in TRAILS:
                peak, hit = 1.0, None
                for i, (h, l, _) in enumerate(seq):
                    stop = peak - tm * atr
                    if l <= stop:
                        hit = (i, stop)
                        break
                    peak = max(peak, h)
                trail[tm] = hit
        m.update(ok=True, cmax=cmax, cmin=cmin, bounds=bounds,
                 finals=finals, trail=trail)
    return days


def resolve(m, hold, tp, sl):
    """Fixed or ATR exit. First touch within the hold window; SL wins ties."""
    n = m["bounds"][hold]
    if n <= 0:
        return None
    cmax, cmin = m["cmax"], m["cmin"]
    i_tp = bisect.bisect_left(cmax, 1 + tp, 0, n)
    lo = [-x for x in cmin[:n]]
    i_sl = bisect.bisect_left(lo, -(1 + sl))
    if i_sl < n and (i_sl <= i_tp or i_tp >= n):
        return sl - 2 * FRICTION
    if i_tp < n:
        return tp - 2 * FRICTION
    f = m["finals"][hold]
    return None if f is None else f - 1 - 2 * FRICTION


def resolve_trail(m, hold, tm):
    """ATR trailing stop, else exit at the hold boundary close."""
    hit = m["trail"].get(tm)
    n = m["bounds"][hold]
    if n <= 0:
        return None
    if hit is not None and hit[0] < n:
        return hit[1] - 1 - 2 * FRICTION
    f = m["finals"][hold]
    return None if f is None else f - 1 - 2 * FRICTION


def stats(rets, years):
    rets = [r for r in rets if r is not None]
    if not rets:
        return None
    eq, peak, mdd = 1.0, 1.0, 0.0
    for r in rets:
        eq *= (1 + r); peak = max(peak, eq); mdd = max(mdd, (peak - eq) / peak)
    wins = [r for r in rets if r > 0]
    gl = abs(sum(r for r in rets if r <= 0))
    ann = (eq ** (1 / years) - 1) if eq > 0 and years > 0 else -1.0
    return {"fills": len(rets), "win_rate": round(len(wins) / len(rets), 4),
            "total_return": round(eq - 1.0, 6), "annual_return": round(ann, 6),
            "quarterly_return": round((1 + ann) ** 0.25 - 1, 6) if ann > -1 else -1,
            "max_drawdown": round(mdd, 6),
            "profit_factor": (round(sum(wins) / gl, 4) if gl > 0 else "inf"),
            "median_trade": round(sorted(rets)[len(rets) // 2], 6)}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).date()
    per_asset, years = {}, {}

    for sym, start in SYMS.items():
        print(f"[clv] fetching {sym} ...", flush=True)
        bars = fetch(sym, ts(start, 0), ts(today, 0))
        first = datetime.fromtimestamp(min(bars) / 1000, tz=timezone.utc).date()
        days = build(bars, first + timedelta(days=7), today - timedelta(days=4))
        del bars
        ks = [d for d in sorted(days) if days[d].get("ok")]
        years[sym] = max((ks[-1] - ks[0]).days / 365.25, 0.1)
        print(f"[clv]   {len(ks)} usable days, {years[sym]:.2f} years", flush=True)

        res = {}
        for clv in CLV_MINS:
            sel = [days[d] for d in ks if days[d]["clv"] >= clv]
            for hold in HOLDS:
                for (tp, sl) in FIXED:
                    res[(clv, hold, f"fixed_{tp}_{sl}")] = stats(
                        [resolve(m, hold, tp, sl) for m in sel], years[sym])
                for (tp, sl) in ATR_X:
                    res[(clv, hold, f"atr_{tp}_{sl}")] = stats(
                        [resolve(m, hold, tp * m["atr"], sl * m["atr"])
                         for m in sel if m.get("atr")], years[sym])
                for tm in TRAILS:
                    res[(clv, hold, f"trail_{tm}")] = stats(
                        [resolve_trail(m, hold, tm) for m in sel if m.get("atr")],
                        years[sym])
        per_asset[sym] = res
        del days
        print(f"[clv]   {len(res)} configurations evaluated", flush=True)

    keys = set.intersection(*(set(v) for v in per_asset.values()))
    rows = []
    for k in sorted(keys):
        sts = {s: per_asset[s][k] for s in SYMS}
        if any(st is None or st["fills"] < MIN_FILLS for st in sts.values()):
            continue
        wq = min(st["quarterly_return"] for st in sts.values())
        rows.append({"clv_min": k[0], "hold_days": k[1], "exit": k[2],
                     "worst_quarterly": round(wq, 6),
                     "worst_annual": round(min(st["annual_return"] for st in sts.values()), 6),
                     "meets_target_all_assets": wq >= TARGET_QUARTERLY,
                     "per_asset": sts})
    rows.sort(key=lambda r: -r["worst_quarterly"])
    hits = [r for r in rows if r["meets_target_all_assets"]]

    print("\n" + "=" * 72)
    print(f"TARGET  >= {TARGET_QUARTERLY:.0%} per quarter ({TARGET_ANNUAL:.1%}/yr) "
          f"on EVERY asset")
    print(f"evaluated {len(rows)} configurations with {MIN_FILLS}+ fills on all three")
    print(f"MEETING TARGET ON ALL THREE: {len(hits)}")
    print("=" * 72)
    for r in rows[:12]:
        flag = "   *** MEETS TARGET" if r["meets_target_all_assets"] else ""
        print(f"\n  clv>={r['clv_min']}  hold {r['hold_days']}d  {r['exit']:18}"
              f"worst quarter {r['worst_quarterly']*100:+6.1f}%{flag}")
        for s, st in r["per_asset"].items():
            print(f"     {s:9} n{st['fills']:4}  win {st['win_rate']*100:5.1f}%  "
                  f"PF {st['profit_factor']:>6}  qtr {st['quarterly_return']*100:+6.1f}%  "
                  f"yr {st['annual_return']*100:+8.1f}%  MDD {st['max_drawdown']*100:4.1f}%  "
                  f"med {st['median_trade']*100:+5.2f}%")

    (OUT / "clv_result.json").write_text(json.dumps({
        "label": "CLV_ONLY_SEARCH",
        "entry_rule": ("CLV >= X on the completed 22:00-22:00 Dubai candle. "
                       "No other entry condition."),
        "target": {"quarterly": TARGET_QUARTERLY, "annual_equivalent": TARGET_ANNUAL,
                   "required_on": "every asset independently"},
        "years_of_data": {k: round(v, 3) for k, v in years.items()},
        "configurations_evaluated": len(rows),
        "meeting_target": len(hits),
        "top_30": rows[:30],
        "caveat": ("An in-sample return threshold measures how hard the search looked, "
                   "not whether an edge exists. Not predictions."),
    }, indent=2, default=str))
    print(f"\nwritten: {OUT}/clv_result.json")


if __name__ == "__main__":
    main()
