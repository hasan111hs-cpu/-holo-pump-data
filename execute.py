"""
CANONICAL R1 / A1 / B1 EXECUTION ENGINE — frozen per the Convention Registry.

Runs the day AFTER a locked signal, once the full trading day (18:05 -> 18:00 UTC)
has completed. Reads signals/<date>.json, appends an execution block, and updates
three independent paper ledgers.

REGISTRY CONVENTIONS IMPLEMENTED:
  O  reference price   = CLOSE of the 18:04 open-time candle on the execution date
  P  flush             = bar LOW <= flush threshold
  Q  same-bar          = LOW <= flush AND that same bar's CLOSE >= reclaim
  R  reclaim           = completed candle CLOSE >= reclaim threshold, after flush armed
  S  entry price       = the confirming candle's CLOSE (never the threshold, never the high)
  T  TP/SL basis       = raw entry close, before friction
  U  exit detection    = TP on bar HIGH, SL on bar LOW
  V  same-bar TP/SL    = adverse first
  W  time exit         = CLOSE of the 17:59 open-time candle before the 18:00 boundary
  X  friction          = 0.10% per side, applied once, scaled by exposure
  Y  compounding       = sequential, realized only

NEVER modify a record after it is written. NEVER change a threshold here.
"""
import json, sys, urllib.parse, urllib.request, time
from datetime import datetime, timezone, timedelta, date
from pathlib import Path

SIG = Path("signals")
LEDGER = Path("ledgers"); LEDGER.mkdir(exist_ok=True)
BASE_URL = "https://data-api.binance.vision/api/v3/klines"
MIN = 60_000
FRICTION_PER_SIDE = 0.0010

SPEC = {
    "R1": dict(flush=-0.0150, reclaim=0.0020, sl=-0.0150, tp=0.0600, expo="r1_exposure"),
    "A1": dict(flush=-0.0100, reclaim=0.0010, sl=-0.0120, tp1=0.0250, tp2=0.0550, expo="a1_exposure"),
    "B1": dict(flush=-0.0100, reclaim=0.0020, sl=-0.0150, tp=0.0350, expo="b1_exposure"),
}
SYMBOL = {"HOLO": "HOLOUSDT", "PUMP": "PUMPUSDT"}


def ts_ms(d, h, m):
    return int(datetime.combine(d, datetime.min.time(), timezone.utc)
               .replace(hour=h, minute=m).timestamp() * 1000)


def fetch(symbol, start_ms, end_ms):
    """Pull 1m klines inclusive of both bounds. Returns {open_time_ms: (o,h,l,c)}."""
    bars, cursor = {}, start_ms
    while cursor <= end_ms:
        q = urllib.parse.urlencode({"symbol": symbol, "interval": "1m", "limit": 1000,
                                    "startTime": cursor, "endTime": end_ms})
        req = urllib.request.Request(f"{BASE_URL}?{q}",
                                     headers={"User-Agent": "HOLO-PUMP-Quant-Lab/3.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = json.loads(r.read().decode())
        if not raw:
            break
        for k in raw:
            bars[k[0]] = (float(k[1]), float(k[2]), float(k[3]), float(k[4]))
        nxt = raw[-1][0] + MIN
        if nxt <= cursor:
            break
        cursor = nxt
        if len(raw) < 1000:
            break
        time.sleep(0.15)
    return bars


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def run_strategy(name, bars, exec_date, exposure):
    s = SPEC[name]
    ref_ms = ts_ms(exec_date, 18, 4)                 # registry O
    start_ms = ts_ms(exec_date, 18, 5)
    end_ms = ts_ms(exec_date + timedelta(days=1), 17, 59)   # registry W

    if ref_ms not in bars:
        return {"status": "NOT OBSERVED", "reason": "reference bar 18:04 unavailable"}
    ref = bars[ref_ms][3]

    seq = [t for t in sorted(bars) if start_ms <= t <= end_ms]
    if not seq:
        return {"status": "NOT OBSERVED", "reason": "no execution bars"}
    expected = (end_ms - start_ms) // MIN + 1
    if len(seq) != expected:
        return {"status": "NO EXECUTION - DATA",
                "reason": f"{expected - len(seq)} missing minutes in the trading window"}

    flush_thr = ref * (1 + s["flush"])
    reclaim_thr = flush_thr * (1 + s["reclaim"])

    # --- flush then close-confirmed reclaim (registry P, Q, R, S) ---
    armed = flush_ms = entry_ms = entry_px = None
    for t in seq:
        o, h, l, c = bars[t]
        if not armed:
            if l <= flush_thr:
                armed, flush_ms = True, t
                if c >= reclaim_thr:                 # same bar: LOW + CLOSE only
                    entry_ms, entry_px = t, c
                    break
            continue
        if c >= reclaim_thr:
            entry_ms, entry_px = t, c
            break

    out = {"reference_ts": iso(ref_ms), "reference_px": ref,
           "flush_threshold": flush_thr, "reclaim_threshold": reclaim_thr,
           "flush_ts": iso(flush_ms) if flush_ms else None,
           "exposure": exposure}

    if flush_ms is None:
        return {**out, "status": "NO FILL", "reason": "no flush"}
    if entry_ms is None:
        return {**out, "status": "NO FILL", "reason": "flush armed, no reclaim"}

    out.update(entry_ts=iso(entry_ms), entry_px=entry_px)
    after = [t for t in seq if t >= entry_ms]

    if name == "A1":
        sl_px = entry_px * (1 + s["sl"])
        tp1_px = entry_px * (1 + s["tp1"])
        tp2_px = entry_px * (1 + s["tp2"])
        out.update(sl_px=sl_px, tp1_px=tp1_px, tp2_px=tp2_px)
        hit_sl = hit_tp1 = None
        for t in after:
            _, h, l, _ = bars[t]
            if l <= sl_px:                            # registry V: adverse first
                hit_sl = t; break
            if h >= tp1_px:
                hit_tp1 = t; break
        if hit_sl is not None:
            gross, reason, exit_ms, exit_px = s["sl"], "SL", hit_sl, sl_px
        elif hit_tp1 is None:
            exit_ms = after[-1]; exit_px = bars[exit_ms][3]
            gross, reason = exit_px / entry_px - 1, "TIME"
        else:
            rest = [t for t in after if t > hit_tp1]
            be = tp2 = None
            for t in rest:
                _, h, l, _ = bars[t]
                if l <= entry_px:                     # breakeven stop on the runner
                    be = t; break
                if h >= tp2_px:
                    tp2 = t; break
            if be is not None:
                back, reason, exit_ms, exit_px = 0.0, "TP1+BE", be, entry_px
            elif tp2 is not None:
                back, reason, exit_ms, exit_px = s["tp2"], "TP1+TP2", tp2, tp2_px
            else:
                exit_ms = rest[-1] if rest else hit_tp1
                exit_px = bars[exit_ms][3]
                back, reason = exit_px / entry_px - 1, "TP1+TIME"
            gross = 0.5 * s["tp1"] + 0.5 * back
    else:
        sl_px = entry_px * (1 + s["sl"])
        tp_px = entry_px * (1 + s["tp"])
        out.update(sl_px=sl_px, tp_px=tp_px)
        gross = reason = exit_ms = exit_px = None
        for t in after:
            _, h, l, _ = bars[t]
            if l <= sl_px:
                gross, reason, exit_ms, exit_px = s["sl"], "SL", t, sl_px; break
            if h >= tp_px:
                gross, reason, exit_ms, exit_px = s["tp"], "TP", t, tp_px; break
        if reason is None:
            exit_ms = after[-1]; exit_px = bars[exit_ms][3]
            gross, reason = exit_px / entry_px - 1, "TIME"

    net = exposure * (gross - 2 * FRICTION_PER_SIDE)      # registry X
    return {**out, "status": "FILL", "exit_ts": iso(exit_ms), "exit_px": exit_px,
            "exit_reason": reason, "gross_return": gross, "net_return": net}


def ledger_path(name):
    return LEDGER / f"{name}.json"


def load_ledger(name):
    p = ledger_path(name)
    if p.exists():
        return json.loads(p.read_text())
    return {"strategy": name, "starting_capital": 10000.0,
            "capital": 10000.0, "observations": 0, "fills": 0, "trades": []}


def main():
    exec_date = (date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1
                 else datetime.now(timezone.utc).date() - timedelta(days=1))
    path = SIG / f"{exec_date.isoformat()}.json"
    if not path.exists():
        print(f"No signal record for {exec_date}"); return
    rec = json.loads(path.read_text())

    if "execution" in rec:
        print(f"{exec_date} already has an execution block — refusing to overwrite")
        return

    state = rec.get("locked_state")
    if state not in ("HOLO", "PUMP"):
        rec["execution"] = {"status": "NO POSITION", "locked_state": state}
        for name in SPEC:
            led = load_ledger(name)
            led["observations"] += 1
            ledger_path(name).write_text(json.dumps(led, indent=2))
        path.write_text(json.dumps(rec, indent=2))
        print(f"{exec_date}  {state}  -> no position, ledgers unchanged")
        return

    # trading window must be complete before execution may be computed
    end_ms = ts_ms(exec_date + timedelta(days=1), 17, 59)
    if datetime.now(timezone.utc).timestamp() * 1000 < end_ms + MIN:
        print(f"{exec_date} trading window has not closed yet — try after "
              f"{iso(end_ms + MIN)}")
        return

    symbol = SYMBOL[state]
    bars = fetch(symbol, ts_ms(exec_date, 18, 0), end_ms)
    print(f"{exec_date}  {state} ({symbol})  {len(bars)} bars fetched")

    block = {"coin": state, "symbol": symbol, "strategies": {}}
    for name in SPEC:
        expo = float(rec[SPEC[name]["expo"]])
        r = run_strategy(name, bars, exec_date, expo)
        block["strategies"][name] = r

        led = load_ledger(name)
        led["observations"] += 1
        if r["status"] == "FILL":
            before = led["capital"]
            led["capital"] = before * (1 + r["net_return"])
            led["fills"] += 1
            led["trades"].append({
                "execution_date": exec_date.isoformat(), "coin": state,
                "entry_ts": r["entry_ts"], "entry_px": r["entry_px"],
                "exit_ts": r["exit_ts"], "exit_px": r["exit_px"],
                "exit_reason": r["exit_reason"], "exposure": r["exposure"],
                "gross_return": r["gross_return"], "net_return": r["net_return"],
                "capital_before": round(before, 2), "capital_after": round(led["capital"], 2),
            })
        ledger_path(name).write_text(json.dumps(led, indent=2))
        print(f"  {name}: {r['status']} {r.get('exit_reason','')} "
              f"net {r.get('net_return', 0)*100:+.2f}%  capital ${led['capital']:,.2f}")

    rec["execution"] = block
    path.write_text(json.dumps(rec, indent=2))


if __name__ == "__main__":
    main()
