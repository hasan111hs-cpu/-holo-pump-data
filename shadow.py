"""
SHADOW / COUNTERFACTUAL TABLE — research and diagnostics only.

AUXILIARY PERSISTENCE (registry §7): retries, then warns. A failure here must never
invalidate a canonical observation or interrupt the strategy path. Every exit path
returns cleanly; nothing raises into the pipeline.

SEGREGATION (registry §5): writes only to shadow/. Never touches signals/, executions/,
ledgers/, operational_logs/, canonical capital, thresholds or observation counts.

CAUSALITY (registry §6): the qualification decision is READ from the immutable signal
artifact, never recomputed. Only the execution mechanics use later authoritative 1m
data, which is permitted because those steps are deterministic given a locked signal.

    python shadow.py            # process all unshadowed signal records
    python shadow.py 2026-09-13 # one date
"""
import json, sys, traceback
from datetime import datetime, timezone, timedelta, date
from pathlib import Path

SIG = Path("signals")
SHADOW = Path("shadow")
DATA = Path("data")

# Frozen qualification thresholds, for distance reporting only. Never applied.
THRESHOLDS = {
    "ret":   {"min": 0.0, "max": 0.06, "desc": "partial-day return in [0%, +6%]"},
    "clv":   {"min": 0.50, "desc": "CLV >= 0.50"},
    "vol":   {"min": 0.60, "desc": "matched volume ratio >= 0.60", "field": "vol_ratio"},
    "taker": {"min": 0.47, "desc": "taker-buy ratio >= 47%"},
    "mom3":  {"min": -0.005, "desc": "3h momentum >= -0.50%"},
    "mom1":  {"min": -0.008, "desc": "1h momentum >= -0.80%"},
    "dfh":   {"max": 0.025, "desc": "distance from high <= 2.5%"},
}
SYMBOL = {"HOLO": "HOLOUSDT", "PUMP": "PUMPUSDT"}
BASE = {"R1": 5.00, "A1": 1.50, "B1": 1.25}
CAP = {"R1": 5.00, "A1": 1.75, "B1": 1.75}


def distance(gate, metrics):
    """How far a failed gate sat from its threshold. Signed, in the metric's own units."""
    t = THRESHOLDS[gate]
    v = metrics.get(t.get("field", gate))
    if v is None:
        return None
    out = {"value": v, "threshold": t["desc"]}
    if "min" in t and v < t["min"]:
        out.update(bound="min", limit=t["min"], distance=round(v - t["min"], 8))
    elif "max" in t and v > t["max"]:
        out.update(bound="max", limit=t["max"], distance=round(v - t["max"], 8))
    else:
        out.update(bound=None, distance=0.0)
    return out


def multipliers(rec, coin_key):
    """Recompute the frozen exposure multipliers from the locked signal's own values."""
    b_mom3, b_rng3 = rec.get("btc_mom3"), rec.get("btc_rng3")
    stress = 0.50 if (b_mom3 is not None and b_rng3 is not None and
                      ((b_mom3 < -0.003 and b_rng3 >= 0.012) or b_rng3 >= 0.020)) else 1.00
    rs = (rec.get(coin_key) or {}).get("rs")
    rs_mult = 1.00 if rs is None else (1.15 if 0.0 <= rs <= 0.02 else (0.75 if rs >= 0.05 else 1.00))
    btcd = rec.get("btcd_mult")
    if btcd is None:
        btcd = btcd_from_source(rec.get("execution_date"))
    return stress, rs_mult, (btcd if btcd is not None else 1.00), (btcd is None)


def btcd_from_source(exec_date):
    """D-1 vs D-2 from the collector's dominance history. Diagnostic use only."""
    try:
        snaps = json.loads((DATA / "BTC-D.json").read_text())
        daily = {}
        for s in sorted(snaps, key=lambda x: x["source_updated_at_unix"]):
            d = datetime.fromtimestamp(s["source_updated_at_unix"], tz=timezone.utc).date()
            daily[d] = float(s["btc_dominance_pct"])
        ed = date.fromisoformat(exec_date)
        d1, d2 = daily.get(ed - timedelta(days=1)), daily.get(ed - timedelta(days=2))
        if d1 is None or d2 is None:
            return None
        return 1.15 if d1 < d2 else 1.00
    except Exception:
        return None


def mechanics(coin, exec_date):
    """Deterministic execution outcome for each strategy. Registry §6 permits later
    authoritative 1m data here; the qualification decision above does not use it."""
    try:
        import execute as EX                      # frozen canonical engine, read-only
    except Exception as exc:
        return {"error": f"cannot import execute.py: {exc}"}
    end_ms = EX.ts_ms(exec_date + timedelta(days=1), 17, 59)
    if datetime.now(timezone.utc).timestamp() * 1000 < end_ms + EX.MIN:
        return {"status": "DEFERRED", "reason": "trading window has not closed"}
    try:
        bars = EX.fetch(SYMBOL[coin], EX.ts_ms(exec_date, 18, 0), end_ms)
    except Exception as exc:
        return {"status": "UNAVAILABLE", "reason": str(exc)}
    out = {}
    for name in ("R1", "A1", "B1"):
        try:
            r = EX.run_strategy(name, bars, exec_date, 1.0)   # unit exposure; scaled below
            out[name] = {k: r.get(k) for k in
                         ("status", "reason", "entry_ts", "entry_px", "exit_ts", "exit_px",
                          "exit_reason", "gross_return", "flush_threshold", "reclaim_threshold")}
        except Exception as exc:
            out[name] = {"status": "ERROR", "reason": str(exc)}
    return out


def build(exec_date):
    p = SIG / f"{exec_date.isoformat()}.json"
    if not p.exists():
        return None
    rec = json.loads(p.read_text())
    state = rec.get("locked_state")

    shadow = {
        "label": "COUNTERFACTUAL / SHADOW",
        "execution_date": exec_date.isoformat(),
        "canonical_locked_state": state,
        "canonical_reason": rec.get("reason"),
        "btc_hard_gate_pass": rec.get("btc_gate_pass"),
        "btc_ret": rec.get("btc_ret"), "btc_mom3": rec.get("btc_mom3"),
        "btc_rng3": rec.get("btc_rng3"),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "canonical_capital_effect": "none",
        "note": ("Research only. Qualification read from the immutable signal artifact; "
                 "execution mechanics reconstructed from later authoritative 1m data. "
                 "Never promoted to canonical history."),
        "coins": {},
    }

    for coin, key in (("HOLO", "holo"), ("PUMP", "pump")):
        m = rec.get(key)
        if not isinstance(m, dict):
            continue
        failed = m.get("failed") or []
        stress, rs_mult, btcd_mult, btcd_estimated = multipliers(rec, key)
        entry = {
            "qualified": m.get("qualify"),
            "failed_gates": failed,
            "metrics": {k: m.get(k) for k in
                        ("ret", "clv", "vol_ratio", "taker", "mom1", "mom3", "dfh", "rs")},
            "distance_from_failed_thresholds": {g: distance(g, m) for g in failed},
            "btc_stress_multiplier": stress,
            "rs_multiplier": rs_mult,
            "btcd_multiplier": btcd_mult,
            "btcd_multiplier_estimated": btcd_estimated,
            "hypothetical_exposure": {
                s: round(min(BASE[s] * stress * rs_mult * btcd_mult, CAP[s]), 6)
                for s in ("R1", "A1", "B1")},
        }
        # counterfactual mechanics for any coin that did not actually trade
        if state != coin:
            mech = mechanics(coin, exec_date)
            entry["counterfactual_execution"] = mech
            if isinstance(mech, dict) and "R1" in mech:
                for s in ("R1", "A1", "B1"):
                    r = mech.get(s) or {}
                    g = r.get("gross_return")
                    if g is not None:
                        expo = entry["hypothetical_exposure"][s]
                        r["hypothetical_net_return"] = round(expo * (g - 0.002), 8)
                        r["hypothetical_exposure"] = expo
        shadow["coins"][coin] = entry

    if state == "NO TRADE" and rec.get("btc_gate_pass") is False:
        shadow["btc_gated_counterfactual"] = (
            "BTC hard gate failed; both coins also failed qualification independently"
            if all(not (rec.get(k) or {}).get("qualify") for k in ("holo", "pump"))
            else "BTC hard gate blocked an otherwise qualifying coin")
    return shadow


def main():
    SHADOW.mkdir(exist_ok=True)
    targets = ([date.fromisoformat(sys.argv[1])] if len(sys.argv) > 1
               else sorted(date.fromisoformat(p.stem) for p in SIG.glob("*.json")))
    written = 0
    for d in targets:
        out = SHADOW / f"{d.isoformat()}.json"
        try:
            existing = json.loads(out.read_text()) if out.exists() else None
            # refresh only if mechanics were previously deferred
            if existing and not any(
                    (c.get("counterfactual_execution") or {}).get("status") == "DEFERRED"
                    for c in (existing.get("coins") or {}).values()):
                continue
            s = build(d)
            if s is None:
                continue
            out.write_text(json.dumps(s, indent=2))
            written += 1
            states = {c: v.get("qualified") for c, v in (s.get("coins") or {}).items()}
            print(f"[shadow] {d}: {s['canonical_locked_state']} | qualified {states}")
        except Exception as exc:
            # AUXILIARY: warn, never raise into the pipeline
            print(f"[shadow] WARNING: {d} failed: {exc}")
            traceback.print_exc(limit=1)
    print(f"[shadow] {written} record(s) written")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[shadow] WARNING: shadow table unavailable this run: {exc}")
