"""
CANONICAL R1 / A1 / B1 SIGNAL ENGINE — frozen per the Convention Registry.
Runs once daily after 16:47 UTC. Writes one immutable record to signals/<date>.json.

Reads only:  data/HOLOUSDT-1m.json, data/PUMPUSDT-1m.json,
             data/BTCUSDT-1m.json, data/BTC-D.json

NEVER modify a record after it is written. NEVER change a threshold here.
"""
import json, sys
from datetime import datetime, timezone, timedelta, date
from pathlib import Path
from statistics import median

DATA = Path("data")
OUT = Path("signals"); OUT.mkdir(exist_ok=True)

MIN = 60_000
DAY_START_H = 18          # 22:00 Dubai
CUT_H, CUT_M = 16, 46     # last bar with close_time <= 16:47:00 UTC
M1_H, M1_M = 15, 46       # wall-clock 60 min before the cutoff bar
M3_H, M3_M = 13, 46       # wall-clock 180 min before
R3_H, R3_M = 13, 47       # first bar of the final 3h window

# BTC.D convention: PROVISIONAL, pending ruling. Daily observation for date X =
# the last CoinGecko snapshot whose source_updated_at falls within X (UTC).
BTCD_PROVISIONAL = True


def ms(y, h, m=0):
    return int(datetime.combine(y, datetime.min.time(), timezone.utc)
               .replace(hour=h, minute=m).timestamp() * 1000)


def load(sym):
    with (DATA / f"{sym}-1m.json").open() as f:
        rows = json.load(f)
    if rows and "open_time_ms" in rows[0]:
        return {r["open_time_ms"]: (float(r["open"]), float(r["high"]),
                float(r["low"]), float(r["close"]), float(r["quote_volume"]),
                float(r["taker_buy_quote_volume"])) for r in rows}
    return {r["t"]: (float(r["o"]), float(r["h"]), float(r["l"]),
            float(r["c"]), float(r["qv"]), float(r["tbq"])) for r in rows}


def window(bars, group_day):
    """Causal partial window: 18:00 UTC on group_day -> 16:46 UTC next day."""
    a = ms(group_day, DAY_START_H)
    z = ms(group_day + timedelta(days=1), CUT_H, CUT_M)
    need = list(range(a, z + MIN, MIN))
    missing = [t for t in need if t not in bars]
    if missing:
        return None, f"{len(missing)} missing minutes"
    return [bars[t] for t in need], None


def metrics(bars, group_day):
    w, err = window(bars, group_day)
    if err:
        return None, err
    ex = group_day + timedelta(days=1)
    for t in (ms(ex, M1_H, M1_M), ms(ex, M3_H, M3_M)):
        if t not in bars:
            return None, "missing wall-clock baseline"
    o = w[0][0]; c = w[-1][3]
    hi = max(r[1] for r in w); lo = min(r[2] for r in w)
    qv = sum(r[4] for r in w); tbq = sum(r[5] for r in w)
    if qv <= 0:
        return None, "zero quote volume"
    w3 = [bars[t] for t in range(ms(ex, R3_H, R3_M), ms(ex, CUT_H, CUT_M) + MIN, MIN)
          if t in bars]
    lo3 = min(r[2] for r in w3)
    return dict(
        partial_open=o, partial_high=hi, partial_low=lo, partial_close=c,
        ret=c / o - 1,
        clv=0.50 if hi == lo else (c - lo) / (hi - lo),      # registry D
        qv=qv, taker=tbq / qv,
        mom1=c / bars[ms(ex, M1_H, M1_M)][3] - 1,
        mom3=c / bars[ms(ex, M3_H, M3_M)][3] - 1,
        dfh=(hi - c) / hi,
        rng3=(max(r[1] for r in w3) - lo3) / lo3,
    ), None


def btcd_daily():
    """Provisional: last snapshot within each UTC calendar day."""
    with (DATA / "BTC-D.json").open() as f:
        snaps = json.load(f)
    out = {}
    for s in sorted(snaps, key=lambda x: x["source_updated_at_unix"]):
        d = datetime.fromtimestamp(s["source_updated_at_unix"], tz=timezone.utc).date()
        out[d] = float(s["btc_dominance_pct"])
    return out


def conditions(m, vol_ratio):
    return {
        "ret": 0.0 <= m["ret"] <= 0.06,
        "clv": m["clv"] >= 0.50,
        "vol": vol_ratio is not None and vol_ratio >= 0.60,
        "taker": m["taker"] >= 0.47,
        "mom3": m["mom3"] >= -0.005,
        "mom1": m["mom1"] >= -0.008,
        "dfh": m["dfh"] <= 0.025,
    }


def main():
    exec_date = (date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1
                 else datetime.now(timezone.utc).date())
    group = exec_date - timedelta(days=1)
    now = datetime.now(timezone.utc)
    cutoff = datetime.combine(exec_date, datetime.min.time(), timezone.utc).replace(
        hour=16, minute=47)

    rec = {"execution_date": exec_date.isoformat(), "signal_group": group.isoformat(),
           "generated_at_utc": now.isoformat(), "lock_time_utc": cutoff.isoformat(),
           "convention_label": "CANONICAL FORWARD OBSERVATION",
           "btcd_convention": "PROVISIONAL last-snapshot-in-UTC-day" if BTCD_PROVISIONAL else "frozen",
           "data_audit": {}}

    if now < cutoff:
        rec.update(locked_state="NOT OBSERVED", reason="run started before the 16:47 UTC cutoff")
        write(rec, exec_date); return

    try:
        bars = {s: load(s) for s in ("HOLOUSDT", "PUMPUSDT", "BTCUSDT")}
        dom = btcd_daily()
    except Exception as exc:
        rec.update(locked_state="NOT OBSERVED", reason=f"input unreadable: {exc}")
        write(rec, exec_date); return

    invalid = []
    M = {}
    for sym, key in (("HOLOUSDT", "holo"), ("PUMPUSDT", "pump"), ("BTCUSDT", "btc")):
        m, err = metrics(bars[sym], group)
        rec["data_audit"][key + "_window"] = err or "ok"
        if err:
            invalid.append(f"{key}:{err}")
        M[key] = m

    # five matched prior windows, coins only
    vr = {}
    for key, sym in (("holo", "HOLOUSDT"), ("pump", "PUMPUSDT")):
        prior = []
        for i in range(1, 6):
            pm, perr = metrics(bars[sym], group - timedelta(days=i))
            if perr:
                prior = None; break
            prior.append(pm["qv"])
        ok = prior is not None and M[key] is not None
        rec["data_audit"][key + "_5_matched"] = "ok" if prior is not None else "incomplete"
        if not ok:
            invalid.append(f"{key}:matched windows incomplete")
            vr[key] = None
        else:
            vr[key] = M[key]["qv"] / median(prior)

    for i, lbl in ((1, "btcd_d1"), (2, "btcd_d2")):
        have = (exec_date - timedelta(days=i)) in dom
        rec["data_audit"][lbl] = "ok" if have else "missing"
        if not have:
            invalid.append(f"{lbl}:missing")

    # UNIVERSE-LEVEL FAIL-CLOSED
    if invalid:
        rec.update(locked_state="NO TRADE - DATA", reason="; ".join(invalid))
        write(rec, exec_date); return

    b = M["btc"]
    rec.update(btc_ret=b["ret"], btc_mom3=b["mom3"], btc_rng3=b["rng3"])
    gate = not (b["ret"] <= -0.015 or b["mom3"] <= -0.008)
    rec["btc_gate_pass"] = gate

    qual = {}
    for key in ("holo", "pump"):
        m = M[key]; cd = conditions(m, vr[key])
        qual[key] = all(cd.values())
        rec[key] = {**{k: m[k] for k in ("partial_open", "partial_high", "partial_low",
                    "partial_close", "ret", "clv", "taker", "mom1", "mom3", "dfh")},
                    "vol_ratio": vr[key], "qualify": qual[key],
                    "rs": m["ret"] - b["ret"],
                    "failed": [k for k, v in cd.items() if not v]}

    if not any(qual.values()):
        rec.update(locked_state="NO TRADE", reason="no coin qualifies")
        write(rec, exec_date); return
    if not gate:
        rec.update(locked_state="NO TRADE", reason="BTC hard gate")
        write(rec, exec_date); return

    sym = ("HOLO" if rec["holo"]["rs"] >= rec["pump"]["rs"] else "PUMP") \
        if (qual["holo"] and qual["pump"]) else ("HOLO" if qual["holo"] else "PUMP")
    rs = rec[sym.lower()]["rs"]
    stress = 0.50 if ((b["mom3"] < -0.003 and b["rng3"] >= 0.012) or b["rng3"] >= 0.020) else 1.00
    rsm = 1.15 if 0.0 <= rs <= 0.02 else (0.75 if rs >= 0.05 else 1.00)
    d1, d2 = dom[exec_date - timedelta(days=1)], dom[exec_date - timedelta(days=2)]
    dm = 1.15 if d1 < d2 else 1.00

    rec.update(locked_state=sym, reason="", stress=stress, rs_mult=rsm,
               btcd_mult=dm, btcd_d1=d1, btcd_d2=d2,
               r1_exposure=min(5.00 * stress * rsm * dm, 5.00),
               a1_exposure=min(1.50 * stress * rsm * dm, 1.75),
               b1_exposure=min(1.25 * stress * rsm * dm, 1.75),
               reference_time_utc=f"{exec_date}T18:05:00+00:00",
               reference_basis="close of the 18:04 open-time candle")
    write(rec, exec_date)


def write(rec, exec_date):
    p = OUT / f"{exec_date.isoformat()}.json"
    if p.exists():
        print(f"REFUSING to overwrite existing immutable record {p}")
        return
    with p.open("w") as f:
        json.dump(rec, f, indent=2)
    print(f"{exec_date}  ->  {rec['locked_state']}  {rec.get('reason','')}")


if __name__ == "__main__":
    main()
