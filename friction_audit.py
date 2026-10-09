"""
FRICTION DECOMPOSITION AND ACCOUNTING AUDIT
Authorised: methodology ruling 8 Oct 2026 — "Controlled Historical Reconstruction
for Accounting Audit Only".

PURPOSE
    Establish whether the reported universe portfolio performance is reproducible,
    determine gross-versus-net results under the ORIGINAL model, and correct the
    identified reporting defects. NOT a reopening of strategy research.

PROHIBITIONS HONOURED
    No optimisation. No threshold change. No new winner selected. No reversal test.
    No sealed data. No frozen prospective strategy touched. Hard cutoff 2026-10-07:
    data is fetched only to that boundary, so no newer observation can enter even
    though newer data now exists upstream.

CONFIGURATION RECOVERY (ruling 3C)
    top_30[0] of research/universe_search/universe_result.json carries clv_min,
    max_positions and exit. That is the authoritative source and is read FIRST.
    The 240-configuration replay is a FALLBACK used only if that read fails, and
    it selects nothing: it matches archived figures within predeclared tolerances.

RECONCILIATION GATE (ruling 5)
    Gross interpretation is WITHHELD unless the original net result reproduces.
    On failure the verdict is ACCOUNTING_RECONCILIATION_FAILURE and no scenario is
    classified. Nothing is silently repaired, recalibrated or substituted.

FAITHFULNESS
    Exit decisions in the original were friction-independent: thresholds compare
    gross price ratios (l <= 1+sl, h >= 1+tp, stop = peak - a*atr) and friction was
    subtracted afterwards. Gross is therefore exactly recoverable as net + 2*FEE.
    build() preserves the original logic exactly: SL checked before TP so SL wins
    ties; trailing stop uses the PRIOR bar's peak then updates; time exit at the
    close of the final bar in the window.

    python friction_audit.py
"""
import json, hashlib, time, urllib.request, csv, sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta, date
from pathlib import Path

OUT = Path("research/friction_audit")
ARCHIVE = Path("research/universe_search/universe_result.json")

BAR_MS = 15 * 60_000
CUT_H = 18                   # 22:00 Dubai
ENTRY_H, ENTRY_M = 18, 0
FEE = 0.001                  # 0.10% per side, ORIGINAL model
START = date(2024, 1, 1)
END = date(2026, 10, 7)      # HARD CUTOFF per ruling 3A. Not today.
START_CAPITAL = 10_000.0
MIN_TRADES = 100             # original filter, unchanged

UNIVERSE = [
    "BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT","ADAUSDT","DOGEUSDT","AVAXUSDT",
    "DOTUSDT","LINKUSDT","LTCUSDT","TRXUSDT","UNIUSDT","ATOMUSDT","ETCUSDT","NEARUSDT",
    "APTUSDT","FILUSDT","ARBUSDT","OPUSDT","INJUSDT","SUIUSDT","SEIUSDT","TIAUSDT",
    "RUNEUSDT","AAVEUSDT","SANDUSDT","MANAUSDT","AXSUSDT","GALAUSDT","ALGOUSDT",
    "EOSUSDT","XLMUSDT","VETUSDT","ICPUSDT","FTMUSDT","THETAUSDT","GRTUSDT",
    "CHZUSDT","ENAUSDT",
]

CLV_MINS = [0.50, 0.60, 0.65, 0.70, 0.75, 0.80]
MAX_POS = [3, 5, 8, 12, 20]
EXITS = [("atr_2.0_-1.5", 2.0, -1.5, False), ("atr_3.0_-2.0", 3.0, -2.0, False),
         ("atr_4.0_-2.5", 4.0, -2.5, False), ("atr_3.0_-1.5", 3.0, -1.5, False),
         ("atr_6.0_-3.0", 6.0, -3.0, False), ("atr_8.0_-4.0", 8.0, -4.0, False),
         ("trail_2.0", 2.0, None, True), ("trail_3.0", 3.0, None, True)]

# Reference figures under audit (ruling 5)
ARCHIVED = {"trades": 6238, "annual_return": -0.232, "profit_factor_day": 0.867,
            "max_drawdown": 0.657}
# Tolerances WITH UNITS (ruling 5: "clarify the numerical units")
TOL = {
    "trades":        {"value": 0.005, "unit": "relative fraction of archived trade count"},
    "annual_return": {"value": 0.004, "unit": "absolute, return fraction (0.004 = 0.40 pp)"},
    "profit_factor": {"value": 0.010, "unit": "absolute, dimensionless ratio units"},
    "max_drawdown":  {"value": 0.010, "unit": "absolute, return fraction (0.010 = 1.00 pp)"},
}
# Descriptive classification boundary, NOT a significance test (ruling 7)
GROSS_BAND = 0.0002


def ts(d, h, m=0):
    return int(datetime.combine(d, datetime.min.time(), timezone.utc)
               .replace(hour=h, minute=m).timestamp() * 1000)


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def recover_config():
    """Ruling 3C: read the archived configuration directly. Replay only if this fails."""
    if not ARCHIVE.exists():
        return None, {"recovered": False, "reason": f"{ARCHIVE} not present in checkout"}
    try:
        raw = ARCHIVE.read_bytes()
        d = json.loads(raw)
        top = d.get("top_30") or []
        if not top:
            return None, {"recovered": False, "reason": "top_30 absent or empty",
                          "archive_sha256": hashlib.sha256(raw).hexdigest()}
        r0 = top[0]
        miss = [k for k in ("clv_min", "max_positions", "exit") if k not in r0]
        prov = {"archive_sha256": hashlib.sha256(raw).hexdigest(),
                "archive_bytes": len(raw),
                "top_30_rows": len(top),
                "configurations_recorded": d.get("configurations"),
                "top0_archived_row": r0}
        if miss:
            prov.update(recovered=False, reason=f"top_30[0] missing {miss}")
            return None, prov
        prov.update(recovered=True)
        return (r0["clv_min"], r0["max_positions"], r0["exit"]), prov
    except Exception as e:
        return None, {"recovered": False, "reason": f"{type(e).__name__}: {e}"}


def fetch(sym, start_ms, end_ms):
    out, cur = {}, start_ms
    while cur < end_ms:
        url = (f"https://data-api.binance.vision/api/v3/klines?symbol={sym}"
               f"&interval=15m&startTime={cur}&endTime={end_ms}&limit=1000")
        rows = None
        for a in range(3):
            try:
                with urllib.request.urlopen(url, timeout=30) as r:
                    rows = json.loads(r.read())
                break
            except Exception:
                if a == 2:
                    return out
                time.sleep(1.5 * (a + 1))
        if not rows:
            break
        for k in rows:
            if int(k[0]) < end_ms:                 # hard cutoff enforced per bar
                out[int(k[0])] = (float(k[1]), float(k[2]), float(k[3]), float(k[4]))
        cur = int(rows[-1][0]) + BAR_MS
        time.sleep(0.05)
        if len(rows) < 1000:
            break
    return out


def bars_provenance(sym, bars):
    """Ruling 3A: record dataset provenance and checksums."""
    h = hashlib.sha256()
    for t in sorted(bars):
        o, hi, lo, c = bars[t]
        h.update(f"{t}:{o!r},{hi!r},{lo!r},{c!r}\n".encode())
    return {"symbol": sym, "bars": len(bars),
            "first_bar_utc": iso(min(bars)) if bars else None,
            "last_bar_utc": iso(max(bars)) if bars else None,
            "sha256": h.hexdigest()}


def build(sym, bars):
    """Per-day CLV, ATR, entry price/timestamp, and the GROSS outcome of each exit."""
    if not bars:
        return {}
    first = datetime.fromtimestamp(min(bars) / 1000, tz=timezone.utc).date()
    last = datetime.fromtimestamp(max(bars) / 1000, tz=timezone.utc).date()
    days, d = {}, first + timedelta(days=1)
    while d <= last:
        s, e = ts(d - timedelta(days=1), CUT_H), ts(d, CUT_H)
        w = [bars[t] for t in range(s, e, BAR_MS) if t in bars]
        if len(w) >= 88:
            o, c = w[0][0], w[-1][3]
            hi, lo = max(b[1] for b in w), min(b[2] for b in w)
            if hi > lo and o > 0:
                days[d] = {"clv": (c - lo) / (hi - lo), "range": (hi - lo) / o}
        d += timedelta(days=1)

    ks = sorted(days)
    for i, dd in enumerate(ks):
        prior = [days[k]["range"] for k in ks[max(0, i - 14):i]]
        days[dd]["atr"] = (sum(prior) / len(prior)) if len(prior) >= 10 else None

    out = {}
    for dd in ks:
        m, atr = days[dd], days[dd]["atr"]
        ent_ms = ts(dd, ENTRY_H, ENTRY_M)
        ent = bars.get(ent_ms)
        if not ent or not atr:
            continue
        px = ent[0]
        seq, t = [], ent_ms + BAR_MS
        end = ts(dd + timedelta(days=1), CUT_H) + BAR_MS
        while t < end:
            b = bars.get(t)
            if b:
                seq.append((b[1] / px, b[2] / px, b[3] / px))
            t += BAR_MS
        if len(seq) < 60:
            continue
        res = {}
        for (lab, a, b_, trail) in EXITS:
            if trail:
                peak, done = 1.0, None
                for i, (h, l, _) in enumerate(seq):
                    stop = peak - a * atr          # prior peak: causally correct
                    if l <= stop:
                        done = (stop - 1.0, "TRAIL", i + 1)
                        break
                    peak = max(peak, h)
                res[lab] = done or (seq[-1][2] - 1.0, "TIME", len(seq))
            else:
                tp, sl = a * atr, b_ * atr
                done = None
                for i, (h, l, _) in enumerate(seq):
                    if l <= 1 + sl:                # SL before TP: SL wins ties
                        done = (sl, "SL", i + 1)
                        break
                    if h >= 1 + tp:
                        done = (tp, "TP", i + 1)
                        break
                res[lab] = done or (seq[-1][2] - 1.0, "TIME", len(seq))
        out[dd] = {"clv": m["clv"], "entry_px": px, "entry_ms": ent_ms,
                   "atr": atr, "res": res}
    return out


def fees_of(cap, gross, fee, mode):
    """ORIGINAL: both fees on ENTRY notional (as the original harness charged them).
       EXACT:    exit fee on actual EXIT notional."""
    entry_fee = cap * fee
    exit_fee = cap * fee if mode == "ORIGINAL" else cap * fee * (1 + gross)
    return entry_fee, exit_fee


def portfolio(data, clv_min, n_max, exit_lab, fee, mode, ledger=False):
    all_days = sorted({d for v in data.values() for d in v})
    if not all_days:
        return None
    eq = geq = START_CAPITAL
    peak = gpeak = START_CAPITAL
    mdd = gmdd = 0.0
    daily, gdaily, series, trades = [], [], [], []
    ntrades, tot_entry_fee, tot_exit_fee = 0, 0.0, 0.0
    deployed_sum = 0.0

    for di, d in enumerate(all_days):
        cands = [(v[d]["clv"], sym) for sym, v in data.items()
                 if d in v and v[d]["clv"] >= clv_min]
        cands.sort(reverse=True)
        picks = cands[:n_max]
        dep = len(picks) / n_max
        deployed_sum += dep
        dr = gr = 0.0
        eq_open = eq
        for clv, sym in picks:
            g, kind, hold = data[sym][d]["res"][exit_lab]
            cap = eq_open / n_max
            ef, xf = fees_of(cap, g, fee, mode)
            gross_pnl = cap * g
            net_pnl = gross_pnl - ef - xf
            n = net_pnl / cap if cap else 0.0
            dr += n / n_max
            gr += g / n_max
            ntrades += 1
            tot_entry_fee += ef
            tot_exit_fee += xf
            if ledger:
                ems = data[sym][d]["entry_ms"]
                xms = ems + hold * BAR_MS
                epx = data[sym][d]["entry_px"]
                trades.append({
                    "trade_id": f"{d.isoformat()}_{sym}",
                    "portfolio_day_index": di,
                    "date": d.isoformat(), "symbol": sym, "clv": round(clv, 6),
                    "entry_ts_utc": iso(ems), "exit_ts_utc": iso(xms),
                    "entry_px": epx, "exit_px": epx * (1 + g),
                    "exit_reason": kind,
                    "hold_bars": hold, "hold_hours": round(hold * 0.25, 2),
                    # 8dp on currency so the ledger reconciles without rounding drift
                    "weight": 1 / n_max, "capital_allocated": round(cap, 8),
                    "gross_return": round(g, 12), "gross_pnl": round(gross_pnl, 8),
                    "entry_fee": round(ef, 8), "exit_fee": round(xf, 8),
                    "net_return": round(n, 12), "net_pnl": round(net_pnl, 8),
                })
        daily.append(dr); gdaily.append(gr)
        eq *= (1 + dr); peak = max(peak, eq); mdd = max(mdd, (peak - eq) / peak)
        geq *= (1 + gr); gpeak = max(gpeak, geq); gmdd = max(gmdd, (gpeak - geq) / gpeak)
        series.append({"date": d.isoformat(), "positions": len(picks),
                       "capital_deployed_frac": round(dep, 6),
                       "cash_frac": round(1 - dep, 6),
                       "net_day_return": round(dr, 12),
                       "gross_day_return": round(gr, 12),
                       "net_equity": round(eq, 6), "gross_equity": round(geq, 6)})

    years = len(all_days) / 365.25
    if eq <= 0 or years <= 0:
        return None

    def pf(xs):
        w = sum(x for x in xs if x > 0)
        l = abs(sum(x for x in xs if x <= 0))
        return round(w / l, 4) if l > 0 else None

    net_tot = eq / START_CAPITAL - 1
    gro_tot = geq / START_CAPITAL - 1
    ann = (1 + net_tot) ** (1 / years) - 1
    gann = (1 + gro_tot) ** (1 / years) - 1
    tdays = sum(1 for x in daily if x != 0)

    r = {"clv_min": clv_min, "max_positions": n_max, "exit": exit_lab,
         "fee_per_side": fee, "fee_mode": mode,
         "trades": ntrades, "calendar_days": len(all_days), "trading_days": tdays,
         "years": round(years, 2),
         "net_total_return": round(net_tot, 6), "net_annual_return": round(ann, 6),
         "net_quarterly_annualised": round((1 + ann) ** 0.25 - 1, 6),
         "net_max_drawdown": round(mdd, 6), "net_profit_factor_DAY": pf(daily),
         "gross_total_return": round(gro_tot, 6), "gross_annual_return": round(gann, 6),
         "gross_max_drawdown": round(gmdd, 6), "gross_profit_factor_DAY": pf(gdaily),
         "final_net_equity": round(eq, 2), "final_gross_equity": round(geq, 2),
         "total_entry_fees": round(tot_entry_fee, 2),
         "total_exit_fees": round(tot_exit_fee, 2),
         "total_fees": round(tot_entry_fee + tot_exit_fee, 2),
         "fee_drag_vs_start_capital": round((tot_entry_fee + tot_exit_fee) / START_CAPITAL, 4),
         "capital_utilisation_mean": round(deployed_sum / len(all_days), 4),
         "capital_idle_mean": round(1 - deployed_sum / len(all_days), 4),
         "turnover_x_capital": round(sum(1 / n_max for _ in range(ntrades)), 2)}

    if ledger:
        g = [t["gross_return"] for t in trades]
        n = [t["net_return"] for t in trades]
        r.update({
            "trade_profit_factor_GROSS": pf(g), "trade_profit_factor_NET": pf(n),
            "trade_expectancy_GROSS": round(sum(g) / len(g), 10),
            "trade_expectancy_NET": round(sum(n) / len(n), 10),
            "trade_win_rate_GROSS": round(sum(1 for x in g if x > 0) / len(g), 4),
            "trade_win_rate_NET": round(sum(1 for x in n if x > 0) / len(n), 4),
            "portfolio_day_expectancy_GROSS": round(sum(gdaily) / max(1, tdays), 10),
            "portfolio_day_expectancy_NET": round(sum(daily) / max(1, tdays), 10),
            "avg_hold_hours": round(sum(t["hold_hours"] for t in trades) / len(trades), 2),
        })
        mix = defaultdict(int)
        for t in trades:
            mix[t["exit_reason"]] += 1
        r["exit_mix"] = {k: round(v / len(trades), 4) for k, v in sorted(mix.items())}
        return r, trades, series
    return r


def robustness(trades, series):
    """Ruling 4D. Descriptive diagnostics only — never criteria for new parameters."""
    pa = defaultdict(lambda: {"trades": 0, "g": 0.0, "n": 0.0, "gp": 0.0, "np": 0.0})
    for t in trades:
        a = pa[t["symbol"]]
        a["trades"] += 1
        a["g"] += t["gross_return"]; a["n"] += t["net_return"]
        a["gp"] += t["gross_pnl"];   a["np"] += t["net_pnl"]
    per_asset = {k: {"trades": v["trades"],
                     "gross_mean_return": round(v["g"] / v["trades"], 8),
                     "net_mean_return": round(v["n"] / v["trades"], 8),
                     "gross_pnl": round(v["gp"], 2), "net_pnl": round(v["np"], 2)}
                 for k, v in sorted(pa.items())}
    g_pos = sum(1 for v in per_asset.values() if v["gross_mean_return"] > 0)
    n_pos = sum(1 for v in per_asset.values() if v["net_mean_return"] > 0)

    # OBSERVED quarterly returns: compound the daily series, not an annualisation
    qd = defaultdict(lambda: {"neq": 1.0, "geq": 1.0, "days": 0})
    for s in series:
        y, m = int(s["date"][:4]), int(s["date"][5:7])
        k = f"{y}Q{(m - 1) // 3 + 1}"
        qd[k]["neq"] *= (1 + s["net_day_return"])
        qd[k]["geq"] *= (1 + s["gross_day_return"])
        qd[k]["days"] += 1
    qt = defaultdict(int)
    for t in trades:
        y, m = int(t["date"][:4]), int(t["date"][5:7])
        qt[f"{y}Q{(m - 1) // 3 + 1}"] += 1
    per_quarter = {k: {"days": v["days"], "trades": qt.get(k, 0),
                       "observed_gross_return": round(v["geq"] - 1, 6),
                       "observed_net_return": round(v["neq"] - 1, 6)}
                   for k, v in sorted(qd.items())}
    qg_pos = sum(1 for v in per_quarter.values() if v["observed_gross_return"] > 0)
    qn_pos = sum(1 for v in per_quarter.values() if v["observed_net_return"] > 0)

    gs = sorted((t["gross_return"] for t in trades), reverse=True)
    ns = sorted((t["net_return"] for t in trades), reverse=True)
    excl_best = {str(k): {"gross_mean": round(sum(gs[k:]) / len(gs[k:]), 10),
                          "net_mean": round(sum(ns[k:]) / len(ns[k:]), 10)}
                 for k in (0, 1, 5, 10, 25, 50)}
    excl_worst = {str(k): {"gross_mean": round(sum(gs[:len(gs) - k]) / (len(gs) - k), 10),
                           "net_mean": round(sum(ns[:len(ns) - k]) / (len(ns) - k), 10)}
                  for k in (0, 1, 5, 10, 25, 50)}
    tg = sum(x for x in gs if x > 0) or 1.0
    tl = abs(sum(x for x in gs if x <= 0)) or 1.0
    conc = {"top_1pct_share_of_gross_profit":
                round(sum(gs[:max(1, len(gs) // 100)]) / tg, 4),
            "top_5pct_share_of_gross_profit":
                round(sum(gs[:max(1, len(gs) // 20)]) / tg, 4),
            "worst_1pct_share_of_gross_loss":
                round(abs(sum(gs[-max(1, len(gs) // 100):])) / tl, 4),
            "worst_5pct_share_of_gross_loss":
                round(abs(sum(gs[-max(1, len(gs) // 20):])) / tl, 4)}

    by_np = sorted(per_asset.items(), key=lambda kv: kv[1]["net_pnl"])
    by_q = sorted(per_quarter.items(), key=lambda kv: kv[1]["observed_net_return"])
    drivers = {
        "worst_5_assets_by_net_pnl": [{"symbol": k, **v} for k, v in by_np[:5]],
        "best_5_assets_by_net_pnl": [{"symbol": k, **v} for k, v in by_np[-5:]][::-1],
        "worst_3_quarters_by_net": [{"quarter": k, **v} for k, v in by_q[:3]],
        "best_3_quarters_by_net": [{"quarter": k, **v} for k, v in by_q[-3:]][::-1],
    }
    return {"per_asset": per_asset, "assets_total": len(per_asset),
            "assets_gross_positive": g_pos,
            "assets_gross_positive_pct": round(g_pos / len(per_asset), 4),
            "assets_net_positive": n_pos,
            "assets_net_positive_pct": round(n_pos / len(per_asset), 4),
            "per_quarter": per_quarter, "quarters_total": len(per_quarter),
            "quarters_gross_positive": qg_pos,
            "quarters_gross_positive_pct": round(qg_pos / len(per_quarter), 4),
            "quarters_net_positive": qn_pos,
            "excluding_best_n_trades": excl_best,
            "excluding_worst_n_trades": excl_worst,
            "concentration": conc, "disproportionate_drivers": drivers,
            "note": "Descriptive only. Not criteria for parameter selection."}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    print(f"[audit] HARD CUTOFF {START} -> {END}. No observation beyond {END}.")

    cfg, prov = recover_config()
    if cfg:
        print(f"[audit] config RECOVERED from archive: clv>={cfg[0]} pos={cfg[1]} exit={cfg[2]}")
    else:
        print(f"[audit] archive recovery FAILED: {prov.get('reason')}")
        print("[audit] falling back to 240-config replay for MATCHING (selects nothing)")

    data, skipped, bprov = {}, [], []
    for i, sym in enumerate(UNIVERSE):
        bars = fetch(sym, ts(START, 0), ts(END, 0))
        bprov.append(bars_provenance(sym, bars))
        d = build(sym, bars)
        del bars
        (skipped.append(sym) if len(d) < 200 else data.__setitem__(sym, d))
        print(f"[audit] {i+1}/{len(UNIVERSE)} {sym:10} {len(d):4} usable days", flush=True)
    if not data:
        print("[audit] FATAL: no usable data"); sys.exit(1)
    print(f"\n[audit] {len(data)} coins usable, {len(skipped)} skipped {skipped}")

    replay = None
    if not cfg:
        rows = []
        for clv in CLV_MINS:
            for n in MAX_POS:
                for (lab, _, _, _) in EXITS:
                    st = portfolio(data, clv, n, lab, FEE, "ORIGINAL")
                    if st and st["trades"] >= MIN_TRADES:
                        rows.append(st)
        rows.sort(key=lambda r: -r["net_quarterly_annualised"])   # original sort key

        def dist(r):
            return (abs(r["trades"] - ARCHIVED["trades"]) / ARCHIVED["trades"]
                    + abs(r["net_annual_return"] - ARCHIVED["annual_return"])
                    + abs((r["net_profit_factor_DAY"] or 0) - ARCHIVED["profit_factor_day"])
                    + abs(r["net_max_drawdown"] - ARCHIVED["max_drawdown"]))
        ranked = sorted(rows, key=dist)
        m = ranked[0]
        ambiguous = len(ranked) > 1 and (dist(ranked[1]) - dist(m)) < 0.01
        cfg = (m["clv_min"], m["max_positions"], m["exit"])
        replay = {"configurations_reproduced": len(rows),
                  "archived_configuration_count": 240,
                  "matched_rank_by_original_sort": rows.index(m) + 1,
                  "match_distance": round(dist(m), 6),
                  "runner_up_distance": round(dist(ranked[1]), 6) if len(ranked) > 1 else None,
                  "ambiguous_match": ambiguous,
                  "top_30_reproduced": rows[:30]}
        print(f"[audit] replay matched: clv>={cfg[0]} pos={cfg[1]} exit={cfg[2]} "
              f"(ambiguous={ambiguous})")

    # ---------- ORIGINAL mode: the reconciliation run ----------
    orig, trades, series = portfolio(data, *cfg, FEE, "ORIGINAL", ledger=True)

    diffs = {
        "trades": {"archived": ARCHIVED["trades"], "reproduced": orig["trades"],
                   "difference": orig["trades"] - ARCHIVED["trades"],
                   "relative": round(abs(orig["trades"] - ARCHIVED["trades"])
                                     / ARCHIVED["trades"], 6),
                   "tolerance": TOL["trades"],
                   "within": abs(orig["trades"] - ARCHIVED["trades"]) / ARCHIVED["trades"]
                             <= TOL["trades"]["value"]},
        "annual_return": {"archived": ARCHIVED["annual_return"],
                          "reproduced": orig["net_annual_return"],
                          "difference": round(orig["net_annual_return"]
                                              - ARCHIVED["annual_return"], 6),
                          "tolerance": TOL["annual_return"],
                          "within": abs(orig["net_annual_return"]
                                        - ARCHIVED["annual_return"])
                                    <= TOL["annual_return"]["value"]},
        "profit_factor_day": {"archived": ARCHIVED["profit_factor_day"],
                              "reproduced": orig["net_profit_factor_DAY"],
                              "difference": round((orig["net_profit_factor_DAY"] or 0)
                                                  - ARCHIVED["profit_factor_day"], 6),
                              "tolerance": TOL["profit_factor"],
                              "within": abs((orig["net_profit_factor_DAY"] or 0)
                                            - ARCHIVED["profit_factor_day"])
                                        <= TOL["profit_factor"]["value"]},
        "max_drawdown": {"archived": ARCHIVED["max_drawdown"],
                         "reproduced": orig["net_max_drawdown"],
                         "difference": round(orig["net_max_drawdown"]
                                             - ARCHIVED["max_drawdown"], 6),
                         "tolerance": TOL["max_drawdown"],
                         "within": abs(orig["net_max_drawdown"]
                                       - ARCHIVED["max_drawdown"])
                                   <= TOL["max_drawdown"]["value"]},
    }
    reconciled = all(v["within"] for v in diffs.values())

    # Ledger must reconcile to the daily series (ruling 4B)
    byday = defaultdict(float)
    for t in trades:
        byday[t["date"]] += t["net_return"] * t["weight"]
    e1 = 1.0
    for d in sorted(byday):
        e1 *= (1 + byday[d])
    e2 = 1.0
    for s in series:
        e2 *= (1 + s["net_day_return"])
    ledger_gap = abs(e1 - e2)
    ledger_ok = ledger_gap < 1e-9

    print("\n" + "=" * 78)
    print("RECONCILIATION GATE".center(78))
    print("=" * 78)
    for k, v in diffs.items():
        print(f"  {k:<20} archived {str(v['archived']):>10}  reproduced "
              f"{str(v['reproduced']):>10}  diff {str(v['difference']):>10}  "
              f"{'PASS' if v['within'] else 'FAIL'}")
    print(f"  {'ledger<->series':<20} gap {ledger_gap:.3e}  "
          f"{'PASS' if ledger_ok else 'FAIL'}")
    print(f"\n  RECONCILED: {reconciled and ledger_ok}")

    # ---------- EXACT variant: separate, never overwriting ORIGINAL ----------
    exact = portfolio(data, *cfg, FEE, "EXACT")

    sens = {}
    for lab, f in [("zero_fee", 0.0), ("hypothetical_0.05pct", 0.0005),
                   ("hypothetical_0.075pct", 0.00075),
                   ("ORIGINAL_MODEL_0.10pct", 0.001),
                   ("plus_spread_0.15pct", 0.0015),
                   ("plus_spread_0.20pct", 0.002)]:
        s = portfolio(data, *cfg, f, "ORIGINAL")
        sens[lab] = {"fee_per_side": f, "hypothetical": f != 0.001,
                     "annual_return": s["net_annual_return"],
                     "total_return": s["net_total_return"],
                     "profit_factor_day": s["net_profit_factor_DAY"],
                     "max_drawdown": s["net_max_drawdown"],
                     "total_fees": s["total_fees"]}

    rob = robustness(trades, series)

    gx = orig["trade_expectancy_GROSS"]
    if not (reconciled and ledger_ok):
        verdict = "ACCOUNTING_RECONCILIATION_FAILURE"
        scenario = "NOT_CLASSIFIED__RECONCILIATION_GATE_FAILED"
    else:
        verdict = "RECONCILED"
        scenario = ("SCENARIO_A_GROSS_POSITIVE_NET_NEGATIVE" if gx > GROSS_BAND else
                    "SCENARIO_C_GROSS_NEGATIVE" if gx < -GROSS_BAND else
                    "SCENARIO_B_GROSS_APPROXIMATELY_ZERO")

    table = [
        ("Total return", orig["gross_total_return"], orig["net_total_return"],
         round(orig["gross_total_return"] - orig["net_total_return"], 6)),
        ("Annualised return", orig["gross_annual_return"], orig["net_annual_return"],
         "difference is not the fee rate: costs compound daily across the path"),
        ("Profit factor - trade level", orig["trade_profit_factor_GROSS"],
         orig["trade_profit_factor_NET"], "non-additive (ratio)"),
        ("Profit factor - portfolio-day", orig["gross_profit_factor_DAY"],
         orig["net_profit_factor_DAY"], "non-additive (ratio)"),
        ("Average return per trade", orig["trade_expectancy_GROSS"],
         orig["trade_expectancy_NET"],
         round(orig["trade_expectancy_GROSS"] - orig["trade_expectancy_NET"], 10)),
        ("Average return per portfolio-day", orig["portfolio_day_expectancy_GROSS"],
         orig["portfolio_day_expectancy_NET"],
         round(orig["portfolio_day_expectancy_GROSS"]
               - orig["portfolio_day_expectancy_NET"], 10)),
        ("Maximum drawdown", orig["gross_max_drawdown"], orig["net_max_drawdown"],
         "non-additive (path dependent)"),
    ]
    print("\n" + "=" * 78)
    print("FRICTION DECOMPOSITION".center(78))
    print("=" * 78)
    print(f"{'Metric':<34}{'Gross':>13}{'Net':>13}{'Fee impact':>18}")
    print("-" * 78)
    for m, g, n, d in table:
        ds = d if isinstance(d, str) else f"{d:+.8f}"
        print(f"{m:<34}{str(g):>13}{str(n):>13}{ds[:18]:>18}")
    print("-" * 78)
    print(f"  entry fees {orig['total_entry_fees']:>12,.2f}   "
          f"exit fees {orig['total_exit_fees']:>12,.2f}   "
          f"total {orig['total_fees']:>12,.2f}")
    print(f"  fee drag vs start capital : {orig['fee_drag_vs_start_capital']*100:.1f}%")
    print(f"  capital utilisation mean  : {orig['capital_utilisation_mean']*100:.1f}%")
    print(f"  avg hold                  : {orig['avg_hold_hours']} h")
    print(f"  exit mix                  : {orig['exit_mix']}")
    print(f"  assets gross-positive     : {rob['assets_gross_positive']}/{rob['assets_total']}")
    print(f"  quarters gross-positive   : {rob['quarters_gross_positive']}/{rob['quarters_total']}")
    print(f"\n  VERDICT  : {verdict}")
    print(f"  SCENARIO : {scenario}")
    if verdict == "ACCOUNTING_RECONCILIATION_FAILURE":
        print("\n  Gross interpretation WITHHELD per ruling 5. Nothing repaired or"
              "\n  recalibrated. One candidate explanation requiring a ruling: the"
              "\n  original effective cutoff may differ from 2026-10-07 by one day,"
              "\n  which would shift calendar_days, trades and years. Not retried here.")

    payload = {
        "label": "FRICTION_DECOMPOSITION_AND_ACCOUNTING_AUDIT",
        "authorised": "Methodology ruling 8 Oct 2026 - accounting audit only",
        "code_version": "friction_audit.py r2 (full ruling scope)",
        "run_utc": datetime.now(timezone.utc).isoformat(),
        "data_boundary": {"start": str(START), "hard_cutoff": str(END),
                          "bars_beyond_cutoff_included": False},
        "integrity": {"new_historical_period": False, "sealed_dataset_used": False,
                      "thresholds_changed": False, "winner_reselected": False,
                      "frozen_specs_modified": False, "reversal_test_run": False},
        "configuration": {"clv_min": cfg[0], "max_positions": cfg[1], "exit": cfg[2],
                          "source": "archive top_30[0]" if replay is None
                                    else "replay match (archive unavailable)",
                          "archive_provenance": prov, "replay": replay},
        "data_provenance": bprov,
        "universe_used": sorted(data), "skipped": skipped,
        "reconciliation": {"reference_figures": ARCHIVED, "tolerances": TOL,
                           "metric_differences": diffs,
                           "ledger_to_series_gap": ledger_gap,
                           "ledger_reconciles": ledger_ok,
                           "verdict": verdict},
        "original_mode": orig,
        "exact_accounting_variant": exact,
        "accounting_variant_delta": {
            "net_annual_return_ORIGINAL": orig["net_annual_return"],
            "net_annual_return_EXACT": exact["net_annual_return"],
            "difference": round(exact["net_annual_return"]
                                - orig["net_annual_return"], 8),
            "total_exit_fees_ORIGINAL": orig["total_exit_fees"],
            "total_exit_fees_EXACT": exact["total_exit_fees"],
            "attribution": ("ORIGINAL charged the exit fee on entry notional; EXACT "
                            "charges it on realised exit notional. EXACT does not "
                            "overwrite ORIGINAL."),
        },
        "decomposition_table": [{"metric": m, "gross": g, "net": n, "fee_impact": d}
                                for m, g, n, d in table],
        "fee_sensitivity": sens,
        "robustness": rob,
        "scenario_classification": scenario,
        "classification_boundary": {
            "gross_per_trade_band": GROSS_BAND,
            "nature": "practical descriptive boundary, NOT a statistical significance test"},
        "execution_limitations": [
            "Modelled friction is EXCHANGE FEES ONLY.",
            "Bid-ask spread: NOT modelled. Slippage: NOT modelled. Market impact: NOT "
            "modelled. Partial fills and liquidity constraints: NOT modelled.",
            "Entry assumed filled at the exact open of the 22:00 15m bar, "
            "simultaneously across up to n_max symbols.",
            "Exit prices for TP/SL/TRAIL are threshold prices, not observed fills.",
            "15-minute resolution: touches resolved to the nearest 15 minutes; SL wins "
            "ties, so exit timing is conservative.",
            "Time exit extends to the 22:00-22:15 bar close, 15 minutes past the next "
            "day's entry: consecutive baskets overlap by one bar (~1% of the hold). "
            "Retained because the ruling requires original overlap handling.",
            "TRUE FRICTION IS THEREFORE UNDERSTATED; real-world results would be worse "
            "than the net figures reported here.",
            "Universe drawn from pairs still trading at the cutoff: survivorship bias "
            "present and not eliminated.",
            "In-sample reconstruction throughout. Not a prediction or a forecast.",
        ],
    }
    (OUT / "friction_audit.json").write_text(json.dumps(payload, indent=2, default=str))

    with (OUT / "trade_ledger.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(trades[0].keys()))
        w.writeheader(); w.writerows(trades)
    with (OUT / "equity_curve.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(series[0].keys()))
        w.writeheader(); w.writerows(series)

    print(f"\nwritten: {OUT}/friction_audit.json")
    print(f"written: {OUT}/trade_ledger.csv   ({len(trades)} trades)")
    print(f"written: {OUT}/equity_curve.csv   ({len(series)} portfolio-days)")


if __name__ == "__main__":
    main()
