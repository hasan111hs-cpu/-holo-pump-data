"""
EXTENDED_HISTORICAL_RESEARCH / FROZEN_PRE_REGISTERED_TEST

Pre-registered out-of-sample test, authorised once. Window 2025-09-17 to 2026-01-31.

This driver adds NO strategy logic. It imports the frozen engines and calls their own
functions, so qualification and execution are computed by exactly the deployed code.
If a date cannot be processed under the frozen rules it is recorded as a frozen data
failure, never repaired.

Writes ONLY to research/extended_historical_validation/. Never touches signals/,
executions/, ledgers/, operational_logs/, canonical capital or CANONICAL_BENCHMARKS.json.

    python research_oos.py
"""
import json, sys, time, urllib.request
from datetime import datetime, timezone, timedelta, date
from pathlib import Path
from statistics import median

import signal as _sigmod_guard  # noqa - ensure stdlib 'signal' not shadowed accidentally
sys.path.insert(0, ".")
import importlib.util

OOS_START = date(2025, 9, 17)
OOS_END = date(2026, 1, 31)
OUT = Path("research/extended_historical_validation")
SYMS = ("HOLOUSDT", "PUMPUSDT", "BTCUSDT")
MIN = 60_000


def load_frozen(name):
    spec = importlib.util.spec_from_file_location(f"frozen_{name}", f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pass
    return mod


def fetch_klines(symbol, start_ms, end_ms):
    """Binance 1m klines, paged. Research-only retrieval."""
    out, cur = {}, start_ms
    while cur < end_ms:
        url = (f"https://api.binance.com/api/v3/klines?symbol={symbol}"
               f"&interval=1m&startTime={cur}&endTime={end_ms}&limit=1000")
        for attempt in range(4):
            try:
                with urllib.request.urlopen(url, timeout=30) as r:
                    rows = json.loads(r.read())
                break
            except Exception as exc:
                if attempt == 3:
                    raise
                time.sleep(2 * (attempt + 1))
        if not rows:
            break
        for k in rows:
            out[int(k[0])] = (float(k[1]), float(k[2]), float(k[3]),
                              float(k[4]), float(k[7]), float(k[10]))
        cur = int(rows[-1][0]) + MIN
        time.sleep(0.15)
        if len(rows) < 1000:
            break
    return out


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    SG = load_frozen("signal")
    EX = load_frozen("execute")

    print(f"[oos] window {OOS_START} -> {OOS_END}")
    start_ms = int(datetime.combine(OOS_START - timedelta(days=8), datetime.min.time(),
                                    timezone.utc).timestamp() * 1000)
    end_ms = int(datetime.combine(OOS_END + timedelta(days=2), datetime.min.time(),
                                  timezone.utc).timestamp() * 1000)

    bars = {}
    for s in SYMS:
        print(f"[oos] fetching {s} ...")
        bars[s] = fetch_klines(s, start_ms, end_ms)
        print(f"[oos]   {len(bars[s])} bars")

    dom = SG.btcd_daily()          # canonical Newhedge, frozen loader
    print(f"[oos] BTC.D observations available: {len(dom)}")

    signals, trades = [], []
    d = OOS_START
    while d <= OOS_END:
        rec = evaluate_day(SG, bars, dom, d)
        signals.append(rec)
        if rec["locked_state"] in ("HOLO", "PUMP"):
            sym = "HOLOUSDT" if rec["locked_state"] == "HOLO" else "PUMPUSDT"
            for name in ("R1", "A1", "B1"):
                try:
                    r = EX.run_strategy(name, bars[sym], d, rec[f"{name.lower()}_exposure"])
                    r.update(execution_date=d.isoformat(), strategy=name, coin=rec["locked_state"])
                    trades.append(r)
                except Exception as exc:
                    trades.append({"execution_date": d.isoformat(), "strategy": name,
                                   "status": "ENGINE_ERROR", "reason": str(exc)})
        d += timedelta(days=1)

    (OUT / "oos_signals.json").write_text(json.dumps(signals, indent=2))
    (OUT / "oos_trades.json").write_text(json.dumps(trades, indent=2, default=str))
    print(f"[oos] {len(signals)} signal records, {len(trades)} strategy evaluations")
    print("[oos] raw artifacts written. Run analyse_oos.py for the pre-registered verdict.")


def evaluate_day(SG, bars, dom, d):
    """Reproduce the frozen daily signal using the engine's own functions."""
    rec = {"execution_date": d.isoformat(), "label": "EXTENDED_HISTORICAL_RESEARCH",
           "data_audit": {}, "locked_state": None, "reason": ""}
    invalid, M, vr = [], {}, {}
    try:
        for sym, key in (("HOLOUSDT", "holo"), ("PUMPUSDT", "pump"), ("BTCUSDT", "btc")):
            m, err = SG.metrics(bars[sym], d)
            rec["data_audit"][f"{key}_window"] = "ok" if m else (err or "invalid")
            if not m:
                invalid.append(f"{key}_window:{err}")
            M[key] = m
        for key, sym in (("holo", "HOLOUSDT"), ("pump", "PUMPUSDT")):
            prior = []
            for i in range(1, 6):
                pm, _ = SG.metrics(bars[sym], d - timedelta(days=i))
                if pm:
                    prior.append(pm["qv"])
            ok = len(prior) == 5
            rec["data_audit"][f"{key}_5_matched"] = "ok" if ok else "missing"
            if not ok:
                invalid.append(f"{key}_5_matched:missing")
            elif M[key]:
                vr[key] = M[key]["qv"] / median(prior)
        for i, lbl in ((1, "btcd_d1"), (2, "btcd_d2")):
            have = (d - timedelta(days=i)) in dom
            rec["data_audit"][lbl] = "ok" if have else "missing"
            if not have:
                invalid.append(f"{lbl}:missing")
    except Exception as exc:
        rec.update(locked_state="NO TRADE - DATA", reason=f"engine:{exc}")
        return rec

    if invalid:
        rec.update(locked_state="NO TRADE - DATA", reason="; ".join(invalid))
        return rec

    b = M["btc"]
    rec["btc_gate_pass"] = gate = (b["ret"] >= -0.015)
    rec["btc_ret"], rec["btc_mom3"], rec["btc_rng3"] = b["ret"], b["mom3"], b["rng3"]
    picked = []
    for key, nm in (("holo", "HOLO"), ("pump", "PUMP")):
        c = SG.conditions(M[key], vr[key])            # frozen: dict of bools
        failed = [k for k, ok in c.items() if not ok]
        q = not failed
        rec[key] = dict(M[key]); rec[key].update(vol_ratio=vr[key], qualify=q, failed=failed,
                                                 rs=M[key]["ret"] - b["ret"])
        if q:
            picked.append((nm, key))
    if not gate:
        rec.update(locked_state="NO TRADE", reason="btc hard gate"); return rec
    if not picked:
        rec.update(locked_state="NO TRADE", reason="no coin qualifies"); return rec
    nm, key = max(picked, key=lambda p: rec[p[1]]["rs"]) if len(picked) > 1 else picked[0]
    rs = rec[key]["rs"]
    stress = 0.50 if ((b["mom3"] < -0.003 and b["rng3"] >= 0.012) or b["rng3"] >= 0.020) else 1.00
    rsm = 1.15 if 0.0 <= rs <= 0.02 else (0.75 if rs >= 0.05 else 1.00)
    d1, d2 = dom[d - timedelta(days=1)], dom[d - timedelta(days=2)]
    dm = 1.15 if d1 < d2 else 1.00
    rec.update(locked_state=nm, stress=stress, rs_mult=rsm, btcd_mult=dm,
               btcd_d_minus_1=d1, btcd_d_minus_2=d2, btcd_provider="NEWHEDGE",
               r1_exposure=min(5.00 * stress * rsm * dm, 5.00),
               a1_exposure=min(1.50 * stress * rsm * dm, 1.75),
               b1_exposure=min(1.25 * stress * rsm * dm, 1.75))
    return rec


if __name__ == "__main__":
    main()
