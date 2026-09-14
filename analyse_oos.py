"""
EXTENDED_HISTORICAL_RESEARCH / FROZEN_PRE_REGISTERED_TEST — verdict analyser.

Applies the pre-registered criteria EXACTLY as frozen before the run. No threshold is
computed from the data; every bound below was fixed in advance and is hard-coded here
so the verdict cannot be influenced by seeing the results.

    python analyse_oos.py
"""
import json
from collections import defaultdict
from datetime import date
from pathlib import Path

D = Path("research/extended_historical_validation")

# ---- PRE-REGISTERED CRITERIA — frozen before the run, not derived from results ----
MIN_FILLS = 12
PF_PASS = 1.50
PF_FAIL = 1.00
MDD_PASS = 0.20
MDD_FAIL = 0.35
CONC_1 = 0.40          # largest win / gross profits
CONC_2 = 0.60          # top two wins / gross profits


def net(t):
    g = t.get("gross_return")
    if g is None:
        return None
    return t.get("exposure", 1.0) * (g - 0.002)


def analyse(trades, name):
    fills = [t for t in trades if t.get("strategy") == name
             and t.get("status") not in (None, "NO FILL", "ENGINE_ERROR")
             and t.get("gross_return") is not None]
    rets = [net(t) for t in fills]
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    pf = (gross_profit / gross_loss) if gross_loss > 0 else (float("inf") if wins else 0.0)

    equity, peak, mdd = 1.0, 1.0, 0.0
    for r in rets:
        equity *= (1 + r)
        peak = max(peak, equity)
        mdd = max(mdd, (peak - equity) / peak)

    srt = sorted(rets, reverse=True)
    excl = {f"excl_best_{k}": (
        (lambda e: e - 1.0)(eval_equity(srt[k:])) if len(srt) > k else None)
        for k in (1, 2, 3)}

    return {
        "strategy": name,
        "fills": len(fills),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(fills), 4) if fills else None,
        "profit_factor": round(pf, 4) if pf != float("inf") else "inf",
        "total_return": round(eval_equity(rets) - 1.0, 6),
        "max_drawdown": round(mdd, 6),
        "largest_win": round(max(wins), 6) if wins else None,
        "largest_loss": round(min(losses), 6) if losses else None,
        "largest_win_over_gross_profits": round(max(wins) / gross_profit, 4) if wins and gross_profit else None,
        "top2_wins_over_gross_profits": round(sum(srt[:2]) / gross_profit, 4) if len(wins) >= 2 and gross_profit else None,
        "median_trade": round(sorted(rets)[len(rets)//2], 6) if rets else None,
        "mean_trade": round(sum(rets)/len(rets), 6) if rets else None,
        **{k: (round(v, 6) if v is not None else None) for k, v in excl.items()},
        "_rets": rets, "_fills": fills,
    }


def eval_equity(rets):
    e = 1.0
    for r in rets:
        e *= (1 + r)
    return e


def verdict(a):
    """Pre-registered decision rule. Order matters: the fill floor dominates."""
    if a["fills"] < MIN_FILLS:
        return "INCONCLUSIVE - INSUFFICIENT FILLS", [
            f"fills {a['fills']} < {MIN_FILLS}; no other metric may override this"]
    pf = a["profit_factor"]
    pf = float("inf") if pf == "inf" else pf
    r, mdd = a["total_return"], a["max_drawdown"]
    c1, c2 = a["largest_win_over_gross_profits"], a["top2_wins_over_gross_profits"]
    conc_fail = (c1 is not None and c1 > CONC_1) or (c2 is not None and c2 > CONC_2)

    reasons = []
    if r < 0:
        reasons.append(f"total return {r:+.4f} < 0")
    if pf < PF_FAIL:
        reasons.append(f"profit factor {pf:.3f} < {PF_FAIL}")
    if mdd > MDD_FAIL:
        reasons.append(f"max drawdown {mdd:.3f} > {MDD_FAIL}")
    if reasons:
        return "FAIL", reasons

    if pf >= PF_PASS and r > 0 and mdd <= MDD_PASS and not conc_fail:
        return "PASS", [f"PF {pf:.3f} >= {PF_PASS}", f"return {r:+.4f} > 0",
                        f"MDD {mdd:.3f} <= {MDD_PASS}", "concentration passes"]

    amb = []
    if PF_FAIL <= pf < PF_PASS and r > 0:
        amb.append(f"PF {pf:.3f} in [{PF_FAIL}, {PF_PASS}) with positive return")
    if MDD_PASS < mdd <= MDD_FAIL:
        amb.append(f"MDD {mdd:.3f} in ({MDD_PASS}, {MDD_FAIL}]")
    if conc_fail:
        amb.append(f"concentration fail: largest {c1}, top2 {c2}")
    return "AMBIGUOUS", amb or ["did not meet all PASS conditions"]


def main():
    signals = json.loads((D / "oos_signals.json").read_text())
    trades = json.loads((D / "oos_trades.json").read_text())

    locks = [s for s in signals if s.get("locked_state") in ("HOLO", "PUMP")]
    no_trade = [s for s in signals if s.get("locked_state") == "NO TRADE"]
    no_data = [s for s in signals if s.get("locked_state") == "NO TRADE - DATA"]

    print("=" * 66)
    print("EXTENDED_HISTORICAL_RESEARCH / FROZEN_PRE_REGISTERED_TEST")
    print("OOS window 2025-09-17 to 2026-01-31")
    print("=" * 66)
    print(f"  execution dates evaluated : {len(signals)}")
    print(f"  locks                     : {len(locks)}  "
          f"(HOLO {sum(1 for s in locks if s['locked_state']=='HOLO')}, "
          f"PUMP {sum(1 for s in locks if s['locked_state']=='PUMP')})")
    print(f"  NO TRADE                  : {len(no_trade)}")
    print(f"  NO TRADE - DATA           : {len(no_data)}")

    out = {"window": "2025-09-17..2026-01-31", "label": "EXTENDED_HISTORICAL_RESEARCH",
           "locks": len(locks), "no_trade": len(no_trade), "no_data": len(no_data),
           "strategies": {}}

    for name in ("R1", "A1", "B1"):
        a = analyse(trades, name)
        v, why = verdict(a)
        a["verdict"], a["verdict_reasons"] = v, why
        fl = a.pop("_fills"); a.pop("_rets")

        by_coin = defaultdict(float)
        for t in fl:
            by_coin[t.get("coin", "?")] += net(t) or 0.0
        a["holo_contribution"] = round(by_coin.get("HOLO", 0.0), 6)
        a["pump_contribution"] = round(by_coin.get("PUMP", 0.0), 6)
        monthly = defaultdict(float)
        for t in fl:
            monthly[t["execution_date"][:7]] += net(t) or 0.0
        a["monthly"] = {k: round(v, 6) for k, v in sorted(monthly.items())}

        print(f"\n--- {name} " + "-" * 52)
        print(f"  fills {a['fills']}  wins {a['wins']}  losses {a['losses']}  "
              f"fill rate {a['fills']/len(locks) if locks else 0:.3f}")
        print(f"  profit factor {a['profit_factor']}   total return {a['total_return']:+.4f}   "
              f"max drawdown {a['max_drawdown']:.4f}")
        print(f"  largest win / gross profits : {a['largest_win_over_gross_profits']}")
        print(f"  top 2 wins / gross profits  : {a['top2_wins_over_gross_profits']}")
        print(f"  excl best 1 / 2 / 3         : {a['excl_best_1']} / {a['excl_best_2']} / {a['excl_best_3']}")
        print(f"  HOLO {a['holo_contribution']:+.4f}   PUMP {a['pump_contribution']:+.4f}")
        print(f"  VERDICT: {v}")
        for r in why:
            print(f"     - {r}")
        out["strategies"][name] = a

    r1 = out["strategies"]["R1"]
    print("\n" + "=" * 66)
    print(f"R1 MINIMUM FILL REQUIREMENT ({MIN_FILLS}): "
          f"{'MET' if r1['fills'] >= MIN_FILLS else 'NOT MET'}  ({r1['fills']} fills)")
    print(f"FORMAL PRE-REGISTERED PRIMARY VERDICT: {r1['verdict']}")
    print("=" * 66)

    (D / "oos_verdict.json").write_text(json.dumps(out, indent=2))
    print("\nwritten: research/extended_historical_validation/oos_verdict.json")


if __name__ == "__main__":
    main()
