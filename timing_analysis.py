"""
HOUR-OF-DAY TIMING ANALYSIS - PUMP / ENA / HOLO

QUESTION
    The 22:00 Dubai boundary was chosen for convenience. Is there a better hour,
    and what hour should an automated bot use?

TWO SEPARATE QUESTIONS, TWO METHODS. Reported separately, never conflated:

  PART A - Does the hour change the SIGNAL?
    A 24-way search, so it is treated as one. The frozen Simple thresholds are NOT
    re-fitted (PUMP 6.5%, ENA 1.0%, HOLO 2.0%); only the boundary hour varies.
    Criteria are PRE-REGISTERED below and fixed before the run. The decisive test is
    persistence: rank the 24 hours on the first half of history, rank them again on
    the second half, and measure whether the ranking survives. A 24-way search will
    always produce a "best" hour; only persistence distinguishes signal from noise.

  PART B - Does the hour change the COST of trading?
    Not a search. Hour-of-day liquidity and price-impact are stable structural
    properties of a market, verifiable by splitting the sample. The friction audit of
    10 Oct established that cost, not signal, decides these strategies, so this is
    the part expected to carry real value.

FROZEN RULES REPRODUCED (from the Simple specification, unchanged)
    prior H->H candle return >= X%  ->  buy at the next H open
    TP +4.0%, SL -2.5%, max hold 24h, fee 0.10% per side
    same-minute TP and SL resolves as SL (conservative)
    1-minute resolution, as the frozen engines use

PRE-REGISTERED CRITERIA FOR PART A (fixed before any real data was examined)
    min_fills_per_hour_per_window = 15
    BETTER_HOUR_EXISTS requires ALL of:
        (a) Spearman rank correlation between fit-window and holdout-window hourly
            rankings >= +0.30
        (b) the fit-window best hour has POSITIVE holdout total return
        (c) that hour's holdout return EXCEEDS the 22:00 baseline's holdout return
        (d) NEIGHBOUR COHERENCE: both adjacent hours (h-1, h+1) also have positive
            holdout return
    AMBIGUOUS      : (a) holds but any of (b), (c), (d) fails
    NO_HOUR_EFFECT : (a) fails
    If NO_HOUR_EFFECT, the hour must be chosen on Part B execution grounds alone.

CALIBRATION OF THESE CRITERIA AGAINST NOISE, done before touching real data
    On synthetic pure-noise bars the rank correlation reached +0.476 -- above the
    +0.30 floor -- while the fit-best hour went from +26.6% to -17.1% out of sample.
    Condition (a) is therefore WEAKLY CALIBRATED on its own, because windows anchored
    at adjacent hours share 23 of 24 hours of data: the hourly curve is smooth, so
    fit and holdout rankings correlate from shared structure rather than from any
    hour effect. The conjunction is what carries the weight, and condition (d) was
    added for this reason: a genuine structural timing effect must bleed into
    neighbouring overlapping windows, whereas a single razor-thin winning hour
    surrounded by losers is the signature of noise. On synthetic bars with a PLANTED
    hour effect the method recovered the correct hour decisively, so it has power.

SCOPE
    This does not touch the six frozen Simple strategies. It changes no specification,
    re-fits no threshold, and is not a proposal to alter the October forward test.

    python timing_analysis.py
"""
import json, time, urllib.request, csv, math
from collections import defaultdict
from datetime import datetime, timezone, timedelta, date
from pathlib import Path

OUT = Path("research/timing_analysis")
MIN_MS = 60_000
DUBAI_OFFSET = 4                      # UTC+4, no DST

COINS = {"PUMPUSDT": 0.065, "ENAUSDT": 0.010, "HOLOUSDT": 0.020}   # frozen X per coin
TP, SL, FEE = 0.040, -0.025, 0.001
MIN_FILLS = 15
RANK_CORR_FLOOR = 0.30
BASELINE_DUBAI_HOUR = 22
START = date(2024, 1, 1)
END = date(2026, 10, 10)


def ts(d, h, m=0):
    return int(datetime.combine(d, datetime.min.time(), timezone.utc)
               .replace(hour=h, minute=m).timestamp() * 1000)


def utc_hour_for_dubai(h):
    return (h - DUBAI_OFFSET) % 24


def fetch(sym, start_ms, end_ms):
    """1m klines: (open, high, low, close, quote_volume, n_trades)."""
    out, cur, miss = {}, start_ms, 0
    while cur < end_ms:
        url = (f"https://data-api.binance.vision/api/v3/klines?symbol={sym}"
               f"&interval=1m&startTime={cur}&endTime={end_ms}&limit=1000")
        rows = None
        for a in range(4):
            try:
                with urllib.request.urlopen(url, timeout=30) as r:
                    rows = json.loads(r.read())
                break
            except Exception:
                if a == 3:
                    miss += 1
                    rows = []
                else:
                    time.sleep(1.5 * (a + 1))
        if not rows:
            if miss > 3:
                break
            cur += 1000 * MIN_MS
            continue
        for k in rows:
            t = int(k[0])
            if t < end_ms:
                out[t] = (float(k[1]), float(k[2]), float(k[3]), float(k[4]),
                          float(k[7]), int(k[8]))
        cur = int(rows[-1][0]) + MIN_MS
        time.sleep(0.08)
        if len(rows) < 1000:
            break
    return out


def candle(bars, s_ms, e_ms):
    """Aggregate [s_ms, e_ms) into one candle. Returns None if too sparse."""
    o = c = None
    hi, lo, qv, n = -1e30, 1e30, 0.0, 0
    for t in range(s_ms, e_ms, MIN_MS):
        b = bars.get(t)
        if not b:
            continue
        if o is None:
            o = b[0]
        c = b[3]
        hi = max(hi, b[1]); lo = min(lo, b[2])
        qv += b[4]; n += 1
    if o is None or n < 1200 or o <= 0 or hi <= lo:      # 1440 minutes in a full day
        return None
    return {"open": o, "close": c, "high": hi, "low": lo, "qv": qv, "bars": n}


def simulate(bars, entry_ms, entry_px, horizon_ms):
    """Frozen Simple exit logic at 1m resolution. SL checked first (wins ties)."""
    tp_px, sl_px = entry_px * (1 + TP), entry_px * (1 + SL)
    last = None
    for t in range(entry_ms + MIN_MS, entry_ms + horizon_ms + MIN_MS, MIN_MS):
        b = bars.get(t)
        if not b:
            continue
        last = b[3]
        if b[2] <= sl_px:
            return SL - 2 * FEE, "SL"
        if b[1] >= tp_px:
            return TP - 2 * FEE, "TP"
    if last is None:
        return None, None
    return last / entry_px - 1 - 2 * FEE, "TIME"


def run_hour(bars, x, dubai_hour, lo_date, hi_date):
    """Frozen rules with the daily boundary anchored at `dubai_hour`."""
    u = utc_hour_for_dubai(dubai_hour)
    rets, exits = [], defaultdict(int)
    d = lo_date + timedelta(days=1)
    evaluated = qualified = 0
    while d <= hi_date:
        prev_s, prev_e = ts(d - timedelta(days=1), u), ts(d, u)
        cd = candle(bars, prev_s, prev_e)
        if cd:
            evaluated += 1
            if cd["close"] / cd["open"] - 1 >= x:
                ent = bars.get(prev_e)
                if ent:
                    qualified += 1
                    r, how = simulate(bars, prev_e, ent[0], 24 * 60 * MIN_MS)
                    if r is not None:
                        rets.append(r); exits[how] += 1
        d += timedelta(days=1)

    if not rets:
        return {"dubai_hour": dubai_hour, "utc_hour": u, "days_evaluated": evaluated,
                "qualified": qualified, "fills": 0, "total_return": None,
                "mean_trade": None, "profit_factor": None, "max_drawdown": None,
                "win_rate": None, "exits": {}}
    eq = peak = 1.0
    mdd = 0.0
    for r in rets:
        eq *= (1 + r); peak = max(peak, eq); mdd = max(mdd, (peak - eq) / peak)
    wins = [r for r in rets if r > 0]
    gl = abs(sum(r for r in rets if r <= 0))
    sd = (sum((r - sum(rets) / len(rets)) ** 2 for r in rets) / len(rets)) ** 0.5 \
        if len(rets) > 1 else 0.0
    return {"dubai_hour": dubai_hour, "utc_hour": u, "days_evaluated": evaluated,
            "qualified": qualified, "fills": len(rets),
            "total_return": round(eq - 1, 6),
            "mean_trade": round(sum(rets) / len(rets), 8),
            "stderr_trade": round(sd / len(rets) ** 0.5, 8) if rets else None,
            "profit_factor": (round(sum(wins) / gl, 4) if gl > 0 else None),
            "max_drawdown": round(mdd, 6),
            "win_rate": round(len(wins) / len(rets), 4),
            "exits": dict(exits)}


def spearman(a, b):
    """Rank correlation. a, b are equal-length lists of comparable values."""
    def ranks(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r
    ra, rb = ranks(a), ranks(b)
    n = len(a)
    ma, mb = sum(ra) / n, sum(rb) / n
    num = sum((ra[i] - ma) * (rb[i] - mb) for i in range(n))
    da = math.sqrt(sum((x - ma) ** 2 for x in ra))
    db = math.sqrt(sum((x - mb) ** 2 for x in rb))
    return (num / (da * db)) if da > 0 and db > 0 else 0.0


def execution_profile(bars, lo_ms=None, hi_ms=None):
    """PART B. Hour-of-day execution quality. Structural, not a search.
       Amihud illiquidity = mean(|1m return| / quote volume): price impact per dollar.
       Lower is better (cheaper to trade)."""
    acc = {h: {"qv": 0.0, "n": 0, "trades": 0, "absr": 0.0, "amihud": 0.0,
               "rbars": 0, "range": 0.0} for h in range(24)}
    for t, b in bars.items():
        if lo_ms is not None and not (lo_ms <= t < hi_ms):
            continue
        o, hi, lo, c, qv, nt = b
        dh = (datetime.fromtimestamp(t / 1000, tz=timezone.utc).hour
              + DUBAI_OFFSET) % 24
        a = acc[dh]
        a["qv"] += qv; a["trades"] += nt; a["n"] += 1
        if o > 0:
            r = abs(c / o - 1)
            a["absr"] += r
            a["range"] += (hi - lo) / o
            a["rbars"] += 1
            if qv > 0:
                a["amihud"] += r / qv
    out = {}
    for h, a in acc.items():
        if a["n"] == 0 or a["rbars"] == 0:
            continue
        out[h] = {"dubai_hour": h, "minutes_sampled": a["n"],
                  "mean_quote_volume_per_min": round(a["qv"] / a["n"], 2),
                  "mean_trades_per_min": round(a["trades"] / a["n"], 2),
                  "mean_abs_return_per_min": round(a["absr"] / a["rbars"], 8),
                  "mean_range_per_min": round(a["range"] / a["rbars"], 8),
                  "amihud_illiquidity_x1e6": round(a["amihud"] / a["rbars"] * 1e6, 6)}
    return out


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    report = {"label": "HOUR_OF_DAY_TIMING_ANALYSIS",
              "generated_utc": datetime.now(timezone.utc).isoformat(),
              "frozen_rules": {"tp": TP, "sl": SL, "fee_per_side": FEE,
                               "max_hold_hours": 24, "resolution": "1m",
                               "thresholds_frozen": COINS,
                               "thresholds_refitted": False},
              "pre_registered": {"min_fills_per_hour_per_window": MIN_FILLS,
                                 "rank_corr_floor": RANK_CORR_FLOOR,
                                 "baseline_dubai_hour": BASELINE_DUBAI_HOUR,
                                 "fixed_before_run": True},
              "timezone": "all hours are DUBAI time (UTC+4, no DST)",
              "coins": {}}

    for sym, x in COINS.items():
        print(f"\n{'='*74}\n[{sym}] fetching 1m history ...", flush=True)
        bars = fetch(sym, ts(START, 0), ts(END, 0))
        if len(bars) < 50_000:
            print(f"[{sym}] insufficient data ({len(bars)} bars) - SKIPPED")
            report["coins"][sym] = {"status": "INSUFFICIENT_DATA", "bars": len(bars)}
            del bars
            continue
        lo_ms, hi_ms = min(bars), max(bars)
        lo_d = datetime.fromtimestamp(lo_ms / 1000, tz=timezone.utc).date()
        hi_d = datetime.fromtimestamp(hi_ms / 1000, tz=timezone.utc).date()
        mid_d = lo_d + (hi_d - lo_d) / 2
        mid_ms = ts(mid_d, 0)
        print(f"[{sym}] {len(bars):,} bars  {lo_d} -> {hi_d}  split at {mid_d}")

        full, fit, hold = [], [], []
        for h in range(24):
            full.append(run_hour(bars, x, h, lo_d, hi_d))
            fit.append(run_hour(bars, x, h, lo_d, mid_d))
            hold.append(run_hour(bars, x, h, mid_d, hi_d))
            f = full[-1]
            rs = "     n/a" if f["total_return"] is None else f"{f['total_return']*100:+8.2f}%"
            ms = "  n/a" if f["max_drawdown"] is None else f"{f['max_drawdown']*100:5.1f}%"
            print(f"   {h:02d}:00 Dubai  fills {f['fills']:>4}  ret {rs}  "
                  f"PF {str(f['profit_factor']):>7}  MDD {ms}", flush=True)

        # --- PART A verdict: persistence of the hourly ranking ---
        ok = [i for i in range(24)
              if fit[i]["fills"] >= MIN_FILLS and hold[i]["fills"] >= MIN_FILLS]
        if len(ok) >= 8:
            rc = spearman([fit[i]["total_return"] for i in ok],
                          [hold[i]["total_return"] for i in ok])
            best_fit = max(ok, key=lambda i: fit[i]["total_return"])
            base_hold = hold[BASELINE_DUBAI_HOUR]["total_return"]
            bh = hold[best_fit]["total_return"]
            cond_a = rc >= RANK_CORR_FLOOR
            cond_b = bh is not None and bh > 0
            cond_c = (bh is not None and base_hold is not None and bh > base_hold)
            nb = [hold[(best_fit - 1) % 24]["total_return"],
                  hold[(best_fit + 1) % 24]["total_return"]]
            cond_d = all(v is not None and v > 0 for v in nb)
            verdict = ("BETTER_HOUR_EXISTS"
                       if (cond_a and cond_b and cond_c and cond_d) else
                       "AMBIGUOUS" if cond_a else "NO_HOUR_EFFECT")
            partA = {"hours_with_sufficient_fills": ok,
                     "fit_window": [str(lo_d), str(mid_d)],
                     "holdout_window": [str(mid_d), str(hi_d)],
                     "rank_correlation_fit_vs_holdout": round(rc, 4),
                     "fit_best_dubai_hour": best_fit,
                     "fit_best_fit_return": fit[best_fit]["total_return"],
                     "fit_best_holdout_return": bh,
                     "baseline_22_holdout_return": base_hold,
                     "neighbour_holdout_returns": {
                         f"{(best_fit-1)%24:02d}:00": nb[0],
                         f"{(best_fit+1)%24:02d}:00": nb[1]},
                     "conditions": {"a_rank_corr_ge_floor": cond_a,
                                    "b_holdout_positive": cond_b,
                                    "c_beats_baseline_in_holdout": cond_c,
                                    "d_neighbour_coherence": cond_d},
                     "calibration_note": (
                         "Condition (a) is weakly calibrated on its own: adjacent "
                         "hours share 23/24 of their data, and on synthetic pure "
                         "noise this statistic reached +0.476. The conjunction of "
                         "(a)-(d) is what carries evidential weight."),
                     "verdict": verdict}
        else:
            partA = {"verdict": "INCONCLUSIVE_INSUFFICIENT_FILLS",
                     "hours_with_sufficient_fills": ok}

        # dispersion vs sampling noise (descriptive; trades across hours overlap)
        mt = [f["mean_trade"] for f in full if f["mean_trade"] is not None]
        se = [f["stderr_trade"] for f in full if f.get("stderr_trade")]
        if len(mt) > 2:
            mu = sum(mt) / len(mt)
            between = (sum((v - mu) ** 2 for v in mt) / (len(mt) - 1)) ** 0.5
            med_se = sorted(se)[len(se) // 2] if se else None
            partA["dispersion"] = {
                "between_hour_sd_of_mean_trade": round(between, 8),
                "median_within_hour_stderr": round(med_se, 8) if med_se else None,
                "ratio": round(between / med_se, 3) if med_se else None,
                "note": ("Ratio near or below 1 means the spread across hours is no "
                         "larger than the sampling noise in each hour's own estimate. "
                         "Descriptive only: windows at different hours overlap, so the "
                         "estimates are correlated and this is not a formal test.")}

        # --- PART B: execution quality, with a stability check ---
        prof_full = execution_profile(bars)
        prof_h1 = execution_profile(bars, lo_ms, mid_ms)
        prof_h2 = execution_profile(bars, mid_ms, hi_ms + MIN_MS)
        common = sorted(set(prof_h1) & set(prof_h2))
        stab = {}
        if len(common) >= 8:
            stab = {"volume_rank_corr_half1_vs_half2":
                        round(spearman([prof_h1[h]["mean_quote_volume_per_min"] for h in common],
                                       [prof_h2[h]["mean_quote_volume_per_min"] for h in common]), 4),
                    "illiquidity_rank_corr_half1_vs_half2":
                        round(spearman([prof_h1[h]["amihud_illiquidity_x1e6"] for h in common],
                                       [prof_h2[h]["amihud_illiquidity_x1e6"] for h in common]), 4)}
        ranked = sorted(prof_full.values(), key=lambda r: r["amihud_illiquidity_x1e6"])
        best_exec = [r["dubai_hour"] for r in ranked[:5]]
        worst_exec = [r["dubai_hour"] for r in ranked[-5:]][::-1]

        print(f"\n[{sym}] PART A verdict: {partA['verdict']}")
        if "rank_correlation_fit_vs_holdout" in partA:
            print(f"   rank corr fit vs holdout : {partA['rank_correlation_fit_vs_holdout']}")
            print(f"   fit-best hour {partA['fit_best_dubai_hour']:02d}:00 -> holdout "
                  f"{partA['fit_best_holdout_return']}")
            print(f"   22:00 baseline holdout   : {partA['baseline_22_holdout_return']}")
        if partA.get("dispersion"):
            print(f"   dispersion ratio         : {partA['dispersion']['ratio']}")
        print(f"[{sym}] PART B cheapest hours to execute (Dubai): {best_exec}")
        print(f"[{sym}] PART B most expensive hours          : {worst_exec}")
        print(f"[{sym}] PART B stability: {stab}")

        report["coins"][sym] = {
            "status": "OK", "threshold_x": x, "bars": len(bars),
            "history": [str(lo_d), str(hi_d)], "split_date": str(mid_d),
            "part_A_signal_by_hour": {"full_sample": full, "fit": fit,
                                      "holdout": hold, **partA},
            "part_B_execution_by_hour": {
                "full_sample": prof_full, "stability": stab,
                "cheapest_5_hours_dubai": best_exec,
                "most_expensive_5_hours_dubai": worst_exec,
                "metric_note": ("amihud_illiquidity_x1e6 = mean(|1m return| / quote "
                                "volume) x 1e6. Lower is cheaper: less price movement "
                                "per dollar traded. Not a quoted spread.")},
        }
        del bars

        with (OUT / f"hourly_{sym}.csv").open("w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["dubai_hour", "utc_hour", "fills", "total_return", "mean_trade",
                        "profit_factor", "max_drawdown", "win_rate",
                        "fit_return", "holdout_return",
                        "mean_quote_volume_per_min", "mean_trades_per_min",
                        "amihud_illiquidity_x1e6"])
            for h in range(24):
                p = prof_full.get(h, {})
                w.writerow([h, utc_hour_for_dubai(h), full[h]["fills"],
                            full[h]["total_return"], full[h]["mean_trade"],
                            full[h]["profit_factor"], full[h]["max_drawdown"],
                            full[h]["win_rate"], fit[h]["total_return"],
                            hold[h]["total_return"],
                            p.get("mean_quote_volume_per_min"),
                            p.get("mean_trades_per_min"),
                            p.get("amihud_illiquidity_x1e6")])

    report["limitations"] = [
        "PART A is a 24-way search per coin and is in-sample over the fit window by "
        "construction. Only the holdout persistence test carries evidential weight.",
        "Windows anchored at different hours overlap heavily (adjacent hours share "
        "23/24 of their data), so the 24 hourly estimates are correlated and no "
        "formal significance test is valid across them.",
        "The rank-correlation floor was calibrated against synthetic pure noise "
        "BEFORE running on real data and reached +0.476 there, above the +0.30 floor. "
        "Condition (a) alone is therefore not sufficient evidence; the conjunction of "
        "(a)-(d), and condition (d) in particular, is what distinguishes a structural "
        "effect from shared-window artifact.",
        "Thresholds were NOT re-fitted per hour. Only the boundary hour varies.",
        "PART B measures realised price impact per dollar (Amihud), not quoted "
        "bid-ask spread, which 1m klines do not contain. It is a proxy.",
        "Execution assumed at the bar open with no spread or slippage, as the frozen "
        "engines assume. True costs are higher at every hour.",
        "Crypto trades continuously, so there is no exchange open or close; hour "
        "effects come from overlapping human session activity.",
        "No frozen specification was modified. This is analysis, not a change.",
    ]
    (OUT / "timing_analysis.json").write_text(json.dumps(report, indent=2, default=str))
    print(f"\nwritten: {OUT}/timing_analysis.json")
    print(f"written: {OUT}/hourly_<SYMBOL>.csv")


if __name__ == "__main__":
    main()
