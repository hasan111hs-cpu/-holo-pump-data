"""
UNIVERSE PORTFOLIO SEARCH — the CLV edge across ~40 coins instead of 3.

HYPOTHESIS. The per-trade CLV edge measured on ENA/PUMP/HOLO is small but real
(profit factor ~1.2). On three correlated coins it produces 50%+ drawdowns, because
losers arrive on the same days. Spread the SAME edge across a wide universe, holding
N positions concurrently, and the edge per trade is unchanged while drawdown falls.
A lower drawdown then permits larger size. That is the route to a higher return
WITHOUT needing a better signal.

TESTED:
    universe     ~40 Binance USDT pairs listed before 2024
    entry        CLV >= X on the completed 22:00-22:00 Dubai candle
    selection    each day, rank qualifying coins by CLV, take the top N
    sizing       equal weight, capital / N per position
    exits        ATR-scaled TP/SL and ATR trailing, 24h hold
    reported     portfolio return, drawdown, and the leverage the drawdown permits

SURVIVORSHIP BIAS. The universe is chosen from pairs that still trade today, which
tilts toward survivors. Deliberately included are several large 2024-2026 losers
(SAND, MANA, AXS, GALA, ALGO, EOS, VET, FTM) to blunt this, but the bias is not
eliminated and the result should be read with that in mind.

RESOLUTION. 15-minute bars, not 1-minute, to keep 40 coins tractable. TP/SL touches
are therefore resolved to the nearest 15 minutes; SL wins ties, so the bias is
conservative.

    python universe_search.py
"""
import json, time, urllib.request
from datetime import datetime, timezone, timedelta, date
from pathlib import Path

OUT = Path("research/universe_search")
BAR_MS = 15 * 60_000
CUT_H = 18                   # 22:00 Dubai
ENTRY_H, ENTRY_M = 18, 0     # entry at the 22:00 bar open (15m grid)
FRICTION = 0.001
START = date(2024, 1, 1)

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

TARGET_Q = 0.15


def ts(d, h, m=0):
    return int(datetime.combine(d, datetime.min.time(), timezone.utc)
               .replace(hour=h, minute=m).timestamp() * 1000)


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
            out[int(k[0])] = (float(k[1]), float(k[2]), float(k[3]), float(k[4]))
        cur = int(rows[-1][0]) + BAR_MS
        time.sleep(0.05)
        if len(rows) < 1000:
            break
    return out


def build(sym, bars):
    """Daily CLV + ATR, and the realised return of each exit config. One pass."""
    if not bars:
        return {}
    first = datetime.fromtimestamp(min(bars) / 1000, tz=timezone.utc).date()
    last = datetime.fromtimestamp(max(bars) / 1000, tz=timezone.utc).date()
    days, d = {}, first + timedelta(days=1)
    while d <= last:
        s, e = ts(d - timedelta(days=1), CUT_H), ts(d, CUT_H)
        w = [bars[t] for t in range(s, e, BAR_MS) if t in bars]
        if len(w) >= 88:                              # 96 bars in a full day
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
        m = days[dd]
        atr = m["atr"]
        ent = bars.get(ts(dd, ENTRY_H, ENTRY_M))
        if not ent or not atr:
            continue
        px = ent[0]
        seq = []
        t = ts(dd, ENTRY_H, ENTRY_M) + BAR_MS
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
                peak, r = 1.0, None
                for h, l, _ in seq:
                    stop = peak - a * atr
                    if l <= stop:
                        r = stop - 1 - 2 * FRICTION
                        break
                    peak = max(peak, h)
                res[lab] = r if r is not None else seq[-1][2] - 1 - 2 * FRICTION
            else:
                tp, sl = a * atr, b_ * atr
                r = None
                for h, l, _ in seq:
                    if l <= 1 + sl:
                        r = sl - 2 * FRICTION
                        break
                    if h >= 1 + tp:
                        r = tp - 2 * FRICTION
                        break
                res[lab] = r if r is not None else seq[-1][2] - 1 - 2 * FRICTION
        out[dd] = {"clv": m["clv"], "res": res}
    return out


def portfolio(data, clv_min, n_max, exit_lab):
    """Equal-weight across up to n_max positions per day, ranked by CLV."""
    all_days = sorted({d for v in data.values() for d in v})
    eq, peak, mdd, daily, ntrades = 1.0, 1.0, 0.0, [], 0
    for d in all_days:
        cands = [(v[d]["clv"], v[d]["res"][exit_lab])
                 for v in data.values() if d in v and v[d]["clv"] >= clv_min]
        if not cands:
            daily.append(0.0)
            continue
        cands.sort(reverse=True)
        picks = cands[:n_max]
        ntrades += len(picks)
        day_ret = sum(r for _, r in picks) / n_max      # idle capital earns nothing
        eq *= (1 + day_ret)
        peak = max(peak, eq)
        mdd = max(mdd, (peak - eq) / peak)
        daily.append(day_ret)
    years = len(all_days) / 365.25
    if eq <= 0 or years <= 0:
        return None
    ann = eq ** (1 / years) - 1
    wins = [x for x in daily if x > 0]
    gl = abs(sum(x for x in daily if x <= 0))
    return {"trades": ntrades, "trading_days": sum(1 for x in daily if x != 0),
            "years": round(years, 2), "total_return": round(eq - 1, 6),
            "annual_return": round(ann, 6),
            "quarterly_return": round((1 + ann) ** 0.25 - 1, 6),
            "max_drawdown": round(mdd, 6),
            "day_win_rate": round(len(wins) / max(1, sum(1 for x in daily if x != 0)), 4),
            "profit_factor": round(sum(wins) / gl, 4) if gl > 0 else "inf",
            "max_safe_leverage_25pct_dd": round(0.25 / mdd, 2) if mdd > 0 else None}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).date()
    data, skipped = {}, []
    for i, sym in enumerate(UNIVERSE):
        bars = fetch(sym, ts(START, 0), ts(today, 0))
        d = build(sym, bars)
        del bars
        if len(d) < 200:
            skipped.append(sym)
        else:
            data[sym] = d
        print(f"[uni] {i+1}/{len(UNIVERSE)} {sym:10} {len(d):4} usable days", flush=True)
    print(f"\n[uni] universe: {len(data)} coins usable, {len(skipped)} skipped {skipped}")

    rows = []
    for clv in CLV_MINS:
        for n in MAX_POS:
            for (lab, _, _, _) in EXITS:
                st = portfolio(data, clv, n, lab)
                if st and st["trades"] >= 100:
                    st.update(clv_min=clv, max_positions=n, exit=lab,
                              meets_target=st["quarterly_return"] >= TARGET_Q)
                    rows.append(st)
    rows.sort(key=lambda r: -r["quarterly_return"])
    hits = [r for r in rows if r["meets_target"]]

    print("\n" + "=" * 78)
    print(f"TARGET >= {TARGET_Q:.0%} per quarter, portfolio level, spot 1x")
    print(f"configurations: {len(rows)}   MEETING TARGET AT 1x: {len(hits)}")
    print("=" * 78)
    print(f"\n{'clv':>5}{'pos':>5}{'exit':>15}{'trades':>8}{'qtr':>8}{'yr':>9}"
          f"{'MDD':>8}{'PF':>7}{'safe lev':>10}")
    for r in rows[:15]:
        flag = "  ***" if r["meets_target"] else ""
        print(f"{r['clv_min']:>5}{r['max_positions']:>5}{r['exit']:>15}{r['trades']:>8}"
              f"{r['quarterly_return']*100:>7.1f}%{r['annual_return']*100:>8.1f}%"
              f"{r['max_drawdown']*100:>7.1f}%{str(r['profit_factor']):>7}"
              f"{str(r['max_safe_leverage_25pct_dd']):>10}{flag}")

    print("\nWITH LEVERAGE sized to a 25% drawdown budget:")
    for r in rows[:10]:
        lev = r["max_safe_leverage_25pct_dd"] or 1
        lev = min(lev, 3.0)
        lev_ann = (1 + r["annual_return"]) ** 1  # approximate: scale daily returns
        print(f"  clv>={r['clv_min']} pos {r['max_positions']:2} {r['exit']:14} "
              f"1x: {r['quarterly_return']*100:5.1f}%/q  MDD {r['max_drawdown']*100:4.1f}%  "
              f"-> at {lev:.1f}x approx {((1+r['annual_return'])**lev)**0.25*100-100:5.1f}%/q "
              f"with ~{r['max_drawdown']*lev*100:4.1f}% MDD")

    (OUT / "universe_result.json").write_text(json.dumps({
        "label": "UNIVERSE_PORTFOLIO_SEARCH",
        "hypothesis": ("Diversifying the same CLV edge across a wide universe reduces "
                       "drawdown without reducing per-trade edge, permitting larger size."),
        "universe_requested": UNIVERSE, "universe_used": sorted(data), "skipped": skipped,
        "bar_resolution": "15m", "entry": "22:00 Dubai bar open", "sizing": "equal weight, capital/N",
        "target_quarterly": TARGET_Q,
        "configurations": len(rows), "meeting_target_at_1x": len(hits),
        "top_30": rows[:30],
        "caveats": [
            "Universe selected from pairs still trading today: survivorship bias present.",
            "15-minute resolution: TP/SL touches resolved to the nearest 15 minutes.",
            "Leverage figures are approximations, not a substitute for a levered simulation.",
            "In-sample. Not predictions.",
        ],
    }, indent=2, default=str))
    print(f"\nwritten: {OUT}/universe_result.json")


if __name__ == "__main__":
    main()
