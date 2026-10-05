"""
ENA FOUR-CONDITION STRATEGY — threshold fitting with a protected holdout.

NEW STRATEGY. Unrelated to R1/A1/B1 and unrelated to MSE.

Qualification (all four must pass) on the completed 22:00->22:00 Dubai candle:
    ret        in [0, X_ret]        day return
    clv        >= X_clv             (close - low) / (high - low)
    vol_ratio  >= X_vol             quote volume / median of prior 5 days
    dfh        <= X_dfh             (high - close) / high

Entry 22:05 Dubai. Exits follow the existing Simple convention so nothing new is
invented here: TP +4.0%, SL -2.5%, time exit 24h. Friction 0.10% per side.

METHOD. The grid is searched on FIT only. The best cell is then applied ONCE to
HOLDOUT, which the search never sees. Both numbers are reported. The holdout number
is the one that means anything.

    python ena_strategy_fit.py
"""
import json, time, urllib.request, itertools
from datetime import datetime, timezone, timedelta, date
from pathlib import Path
from statistics import median

SYMBOL = "ENAUSDT"
OUT = Path("research/ena_four_condition")
MIN = 60_000
CUT_H = 18                 # 22:00 Dubai
ENTRY_H, ENTRY_M = 18, 5   # 22:05 Dubai
TP, SL, FRICTION = 0.040, -0.025, 0.001

# Grid. Deliberately coarse: a fine grid fits noise.
G_RET = [0.02, 0.03, 0.04, 0.06, 0.08, 0.12]
G_CLV = [0.00, 0.30, 0.50, 0.65, 0.80]
G_VOL = [0.00, 0.60, 0.90, 1.20, 1.60]
G_DFH = [0.010, 0.020, 0.035, 0.050, 1.000]


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
    """The completed 22:00->22:00 Dubai candle ending at 18:00 UTC on date d."""
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
    """Entry 22:05 Dubai, TP/SL/24h time exit. SL wins same-minute ties."""
    s = ts(d, ENTRY_H, ENTRY_M)
    e = ts(d + timedelta(days=1), CUT_H)
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


def evaluate(days, x_ret, x_clv, x_vol, x_dfh):
    rets = []
    for rec in days:
        m, vr, ent = rec["m"], rec["vr"], rec["entry"]
        if vr is None or ent is None:
            continue
        if not (0.0 <= m["ret"] <= x_ret):
            continue
        if m["clv"] < x_clv or vr < x_vol or m["dfh"] > x_dfh:
            continue
        r, _ = simulate(rec["bars"], rec["date"], ent)
        if r is not None:
            rets.append(r)
    if not rets:
        return {"fills": 0, "total_return": 0.0, "profit_factor": 0.0,
                "max_drawdown": 0.0, "win_rate": None}
    eq, peak, mdd = 1.0, 1.0, 0.0
    for r in rets:
        eq *= (1 + r); peak = max(peak, eq); mdd = max(mdd, (peak - eq) / peak)
    wins = [r for r in rets if r > 0]
    gl = abs(sum(r for r in rets if r <= 0))
    return {"fills": len(rets), "total_return": eq - 1.0,
            "profit_factor": (sum(wins) / gl) if gl > 0 else float("inf"),
            "max_drawdown": mdd, "win_rate": len(wins) / len(rets),
            "largest_win": max(rets), "largest_loss": min(rets),
            "excl_best_1": (lambda x: x - 1)(prod(sorted(rets, reverse=True)[1:])),
            "returns": rets}


def prod(rs):
    e = 1.0
    for r in rs:
        e *= (1 + r)
    return e


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    print(f"[ena] fetching {SYMBOL} ...")
    today = datetime.now(timezone.utc).date()
    bars = fetch(SYMBOL, ts(date(2024, 1, 1), 0), ts(today, 0))
    if not bars:
        raise RuntimeError("no data")
    first = datetime.fromtimestamp(min(bars) / 1000, tz=timezone.utc).date()
    print(f"[ena] {len(bars)} bars, first {first}")

    days, d = [], first + timedelta(days=7)
    while d < today:
        m = day_metrics(bars, d)
        if m:
            prior = [p for p in (day_metrics(bars, d - timedelta(days=i)) for i in range(1, 6)) if p]
            vr = (m["qv"] / median([p["qv"] for p in prior])) if len(prior) == 5 else None
            ent = bars.get(ts(d, ENTRY_H, ENTRY_M))
            days.append({"date": d, "m": m, "vr": vr,
                         "entry": ent[0] if ent else None, "bars": bars})
        d += timedelta(days=1)

    split = days[int(len(days) * 0.6)]["date"]
    fit = [x for x in days if x["date"] < split]
    hold = [x for x in days if x["date"] >= split]
    print(f"[ena] {len(days)} days | FIT {fit[0]['date']}..{fit[-1]['date']} ({len(fit)})"
          f" | HOLDOUT {hold[0]['date']}..{hold[-1]['date']} ({len(hold)})")

    best, rows = None, []
    for xr, xc, xv, xd in itertools.product(G_RET, G_CLV, G_VOL, G_DFH):
        r = evaluate(fit, xr, xc, xv, xd)
        if r["fills"] < 20:
            continue
        rows.append({"x_ret": xr, "x_clv": xc, "x_vol": xv, "x_dfh": xd,
                     "fills": r["fills"], "total_return": round(r["total_return"], 6),
                     "profit_factor": round(r["profit_factor"], 4),
                     "max_drawdown": round(r["max_drawdown"], 6)})
        if best is None or r["total_return"] > best[1]["total_return"]:
            best = ((xr, xc, xv, xd), r)

    if best is None:
        print("[ena] no grid cell produced 20+ fills on FIT"); return
    (xr, xc, xv, xd), fr = best
    hr = evaluate(hold, xr, xc, xv, xd)      # applied ONCE, never searched

    print("\n" + "=" * 60)
    print(f"BEST ON FIT: ret<= {xr}  clv>= {xc}  vol>= {xv}  dfh<= {xd}")
    print("=" * 60)
    for lbl, r in (("FIT     ", fr), ("HOLDOUT ", hr)):
        pf = r["profit_factor"]
        print(f"  {lbl} fills {r['fills']:3}  return {r['total_return']:+.4f}  "
              f"PF {pf if pf == float('inf') else round(pf, 3)}  "
              f"MDD {r['max_drawdown']:.4f}  win {r.get('win_rate')}")
    if hr["fills"]:
        print(f"  HOLDOUT excluding best trade: {hr['excl_best_1']:+.4f}")
    print("\n  The HOLDOUT line is the one that means anything.")

    rows.sort(key=lambda x: -x["total_return"])
    (OUT / "ena_fit_result.json").write_text(json.dumps({
        "label": "NEW_STRATEGY_RESEARCH",
        "symbol": SYMBOL, "cutoff_dubai": "22:00", "entry_dubai": "22:05",
        "exits": {"tp": TP, "sl": SL, "time_exit_hours": 24, "friction_per_side": FRICTION},
        "fit_window": [str(fit[0]["date"]), str(fit[-1]["date"])],
        "holdout_window": [str(hold[0]["date"]), str(hold[-1]["date"])],
        "chosen_thresholds": {"x_ret": xr, "x_clv": xc, "x_vol": xv, "x_dfh": xd},
        "fit_result": {k: v for k, v in fr.items() if k != "returns"},
        "holdout_result": {k: v for k, v in hr.items() if k != "returns"},
        "top_20_fit_cells": rows[:20],
        "caveat": ("Thresholds were chosen to maximise FIT return. The FIT figure is "
                   "therefore not a prediction. HOLDOUT was evaluated once with the "
                   "chosen thresholds and never searched."),
    }, indent=2, default=str))
    print(f"\nwritten: {OUT}/ena_fit_result.json")


if __name__ == "__main__":
    main()
