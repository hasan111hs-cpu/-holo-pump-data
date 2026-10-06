"""
CROSS-ASSET TEST — ENA four-condition thresholds applied unchanged to PUMP.

This is NOT a fitting exercise. The thresholds below were selected on ENA FIT data
before any PUMP data was examined. Nothing is searched here: one configuration,
one evaluation, one number.

PRE-REGISTERED CRITERIA, fixed before this ran:
    minimum 15 fills for any verdict
    PASS       positive return AND profit factor >= 1.30
    AMBIGUOUS  positive return AND profit factor 1.00-1.30
    FAIL       negative return OR profit factor < 1.00

    python pump_crossasset_test.py
"""
import json, time, urllib.request
from datetime import datetime, timezone, timedelta, date
from pathlib import Path
from statistics import median

SYMBOL = "PUMPUSDT"
OUT = Path("research/ena_four_condition")
MIN = 60_000
CUT_H = 18                 # 22:00 Dubai
ENTRY_H, ENTRY_M = 18, 5   # 22:05 Dubai
TP, SL, FRICTION = 0.040, -0.025, 0.001

# Fixed on ENA. Not re-fitted, not adjusted, not re-searched.
X_RET, X_CLV, X_VOL, X_DFH = 0.03, 0.30, 0.00, 0.05

MIN_FILLS, PF_PASS, PF_AMBIG = 15, 1.30, 1.00


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
        time.sleep(0.12)
        if len(rows) < 1000:
            break
    return out


def ts(d, h, m=0):
    return int(datetime.combine(d, datetime.min.time(), timezone.utc)
               .replace(hour=h, minute=m).timestamp() * 1000)


def day_metrics(bars, d):
    s, e = ts(d - timedelta(days=1), CUT_H), ts(d, CUT_H)
    w = [bars[t] for t in range(s, e, MIN) if t in bars]
    if len(w) < 1300:
        return None
    o, c = w[0][0], w[-1][3]
    hi, lo = max(b[1] for b in w), min(b[2] for b in w)
    if hi <= lo or o <= 0 or hi <= 0:
        return None
    return {"open": o, "close": c, "high": hi, "low": lo,
            "ret": c / o - 1, "clv": (c - lo) / (hi - lo),
            "dfh": (hi - c) / hi, "qv": sum(b[4] for b in w)}


def simulate(bars, d, entry_px):
    s, e = ts(d, ENTRY_H, ENTRY_M), ts(d + timedelta(days=1), CUT_H)
    tp_px, sl_px = entry_px * (1 + TP), entry_px * (1 + SL)
    last = None
    for t in range(s + MIN, e + MIN, MIN):
        b = bars.get(t)
        if not b:
            continue
        last = b[3]
        if b[2] <= sl_px:
            return SL - FRICTION * 2, "SL"
        if b[1] >= tp_px:
            return TP - FRICTION * 2, "TP"
    if last is None:
        return None, None
    return last / entry_px - 1 - FRICTION * 2, "TIME"


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).date()
    print(f"[pump] fetching {SYMBOL} ...")
    bars = fetch(SYMBOL, ts(date(2025, 9, 1), 0), ts(today, 0))
    if not bars:
        raise RuntimeError("no data")
    first = datetime.fromtimestamp(min(bars) / 1000, tz=timezone.utc).date()
    print(f"[pump] {len(bars)} bars, first {first}")

    rets, trades, evaluated, qualified = [], [], 0, 0
    d = first + timedelta(days=7)
    while d < today:
        m = day_metrics(bars, d)
        if m:
            evaluated += 1
            prior = [p for p in (day_metrics(bars, d - timedelta(days=i)) for i in range(1, 6)) if p]
            vr = (m["qv"] / median([p["qv"] for p in prior])) if len(prior) == 5 else None
            ent = bars.get(ts(d, ENTRY_H, ENTRY_M))
            if (vr is not None and ent is not None
                    and 0.0 <= m["ret"] <= X_RET and m["clv"] >= X_CLV
                    and vr >= X_VOL and m["dfh"] <= X_DFH):
                qualified += 1
                r, how = simulate(bars, d, ent[0])
                if r is not None:
                    rets.append(r)
                    trades.append({"date": d.isoformat(), "net_return": round(r, 6),
                                   "exit": how, "day_ret": round(m["ret"], 6),
                                   "clv": round(m["clv"], 4), "dfh": round(m["dfh"], 6)})
        d += timedelta(days=1)

    eq, peak, mdd = 1.0, 1.0, 0.0
    for r in rets:
        eq *= (1 + r); peak = max(peak, eq); mdd = max(mdd, (peak - eq) / peak)
    wins = [r for r in rets if r > 0]
    gl = abs(sum(r for r in rets if r <= 0))
    pf = (sum(wins) / gl) if gl > 0 else (float("inf") if wins else 0.0)
    total = eq - 1.0

    if len(rets) < MIN_FILLS:
        verdict = "INCONCLUSIVE - INSUFFICIENT FILLS"
        why = [f"fills {len(rets)} < {MIN_FILLS}; no other metric may override this"]
    elif total < 0 or pf < PF_AMBIG:
        verdict = "FAIL"
        why = ([f"total return {total:+.4f} < 0"] if total < 0 else []) + \
              ([f"profit factor {pf:.3f} < {PF_AMBIG}"] if pf < PF_AMBIG else [])
    elif pf >= PF_PASS:
        verdict = "PASS"
        why = [f"return {total:+.4f} > 0", f"PF {pf:.3f} >= {PF_PASS}"]
    else:
        verdict = "AMBIGUOUS"
        why = [f"PF {pf:.3f} in [{PF_AMBIG}, {PF_PASS}) with positive return"]

    srt = sorted(rets, reverse=True)
    def prod(xs):
        e = 1.0
        for x in xs:
            e *= (1 + x)
        return e

    print("\n" + "=" * 62)
    print("CROSS-ASSET TEST — ENA thresholds applied unchanged to PUMP")
    print("=" * 62)
    print(f"  thresholds   ret<= {X_RET}  clv>= {X_CLV}  vol>= {X_VOL}  dfh<= {X_DFH}")
    print(f"  days evaluated {evaluated}   qualified {qualified}   fills {len(rets)}")
    if rets:
        print(f"  wins {len(wins)}/{len(rets)} = {len(wins)/len(rets)*100:.1f}%")
        print(f"  total return {total:+.4f}   PF {pf if pf==float('inf') else round(pf,3)}"
              f"   MDD {mdd:.4f}")
        print(f"  excluding best 1 / 2 / 3: {prod(srt[1:])-1:+.4f} / "
              f"{prod(srt[2:])-1:+.4f} / {prod(srt[3:])-1:+.4f}")
    print(f"\n  VERDICT: {verdict}")
    for w in why:
        print(f"     - {w}")

    (OUT / "pump_crossasset_result.json").write_text(json.dumps({
        "label": "CROSS_ASSET_TEST",
        "symbol": SYMBOL,
        "thresholds_source": "fitted on ENA FIT window; applied unchanged, never re-searched",
        "thresholds": {"x_ret": X_RET, "x_clv": X_CLV, "x_vol": X_VOL, "x_dfh": X_DFH},
        "exits": {"tp": TP, "sl": SL, "time_exit_hours": 24, "friction_per_side": FRICTION},
        "pre_registered_criteria": {
            "min_fills": MIN_FILLS, "pass_pf": PF_PASS, "ambiguous_pf_floor": PF_AMBIG,
            "fixed_before_run": True},
        "window": [str(first), str(today)],
        "days_evaluated": evaluated, "days_qualified": qualified, "fills": len(rets),
        "wins": len(wins), "win_rate": round(len(wins)/len(rets), 4) if rets else None,
        "total_return": round(total, 6),
        "profit_factor": (round(pf, 4) if pf != float("inf") else "inf"),
        "max_drawdown": round(mdd, 6),
        "excl_best_1": round(prod(srt[1:]) - 1, 6) if len(srt) > 1 else None,
        "excl_best_2": round(prod(srt[2:]) - 1, 6) if len(srt) > 2 else None,
        "excl_best_3": round(prod(srt[3:]) - 1, 6) if len(srt) > 3 else None,
        "verdict": verdict, "verdict_reasons": why,
        "trades": trades,
        "note": ("Cross-asset test. Thresholds were fixed on ENA before any PUMP data was "
                 "examined, so this is a genuine out-of-sample evaluation on a new asset. "
                 "No search, no re-fitting. The ENA holdout for these thresholds was FAIL "
                 "(-25.5%, PF 0.774)."),
    }, indent=2))
    print(f"\nwritten: {OUT}/pump_crossasset_result.json")


if __name__ == "__main__":
    main()
