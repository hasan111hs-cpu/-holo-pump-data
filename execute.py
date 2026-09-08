"""
CANONICAL R1 / A1 / B1 EXECUTION ENGINE — frozen per the Convention Registry.

Reads an IMMUTABLE signal record, writes an IMMUTABLE execution record, then
applies an idempotent ledger settlement.

    signals/<date>.json      written once by signal.py, never modified
    executions/<date>.json   written once by execute.py, never modified
    ledgers/<strategy>.json  mutated only via unique settlement ids

REGISTRY CONVENTIONS
  O  reference price   = CLOSE of the 18:04 open-time candle
  P  flush             = bar LOW <= flush threshold
  Q  same-bar          = LOW <= flush AND that same bar's CLOSE >= reclaim
  R  reclaim           = completed candle CLOSE >= reclaim threshold, after flush armed
  S  entry price       = the confirming candle's CLOSE
  T  TP/SL basis       = raw entry close, before friction
  U  exit detection    = TP on bar HIGH, SL on bar LOW
  V  same-bar TP/SL    = adverse first
  W  time exit         = CLOSE of the 17:59 open-time candle before the 18:00 boundary
  X  friction          = 0.10% per side, once, scaled by exposure
  Y  compounding       = sequential, realized only

  Z1 ENTRY-CANDLE CAUSALITY (added)
     Entry occurs at the CLOSE of the confirming candle, so that candle's HIGH and
     LOW happened BEFORE the position existed. Exit evaluation therefore begins with
     the NEXT completed candle. Applies to R1, A1 and B1.

  Z2 A1 TP1 SAME-MINUTE CONVENTION (added)
     Once TP1 is reached inside a candle, the runner's breakeven stop is live for the
     remainder of that same candle, but 1m OHLC cannot resolve intrabar ordering.
     Adverse-first: if the TP1 candle's LOW <= entry price, the runner is closed at
     breakeven within that candle. TP2 is NOT recognised inside the TP1 candle, since
     recognising the favourable event while ignoring the adverse one would invert the
     frozen adverse-first philosophy.
"""
import json, sys, urllib.parse, urllib.request, time
from datetime import datetime, timezone, timedelta, date
from pathlib import Path

SIG = Path("signals")
EXEC = Path("executions"); EXEC.mkdir(exist_ok=True)
LEDGER = Path("ledgers"); LEDGER.mkdir(exist_ok=True)
BASE_URL = "https://data-api.binance.vision/api/v3/klines"
MIN = 60_000
FRICTION_PER_SIDE = 0.0010
RETRIES = 3

SPEC = {
    "R1": dict(flush=-0.0150, reclaim=0.0020, sl=-0.0150, tp=0.0600, expo="r1_exposure"),
    "A1": dict(flush=-0.0100, reclaim=0.0010, sl=-0.0120, tp1=0.0250, tp2=0.0550, expo="a1_exposure"),
    "B1": dict(flush=-0.0100, reclaim=0.0020, sl=-0.0150, tp=0.0350, expo="b1_exposure"),
}
SYMBOL = {"HOLO": "HOLOUSDT", "PUMP": "PUMPUSDT"}

# states that ARE canonical prospective observations
COUNTED = {"NO TRADE", "NO TRADE - DATA"}


class ObserverFailure(Exception):
    """The evaluation process itself did not complete. Never becomes permanent."""


def ts_ms(d, h, m):
    return int(datetime.combine(d, datetime.min.time(), timezone.utc)
               .replace(hour=h, minute=m).timestamp() * 1000)


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def fetch(symbol, start_ms, end_ms):
    bars, cursor = {}, start_ms
    while cursor <= end_ms:
        q = urllib.parse.urlencode({"symbol": symbol, "interval": "1m", "limit": 1000,
                                    "startTime": cursor, "endTime": end_ms})
        req = urllib.request.Request(f"{BASE_URL}?{q}",
                                     headers={"User-Agent": "HOLO-PUMP-Quant-Lab/4.0"})
        raw = None
        for attempt in range(RETRIES):
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    raw = json.loads(r.read().decode())
                break
            except Exception as exc:
                if attempt == RETRIES - 1:
                    raise ObserverFailure(f"{symbol}: {exc}")
                time.sleep(2 ** attempt)
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


def run_strategy(name, bars, exec_date, exposure):
    s = SPEC[name]
    ref_ms = ts_ms(exec_date, 18, 4)
    start_ms = ts_ms(exec_date, 18, 5)
    end_ms = ts_ms(exec_date + timedelta(days=1), 17, 59)

    if ref_ms not in bars:
        return {"status": "NO EXECUTION - DATA",
                "reason": "mandatory 18:04 reference bar absent from source"}
    ref = bars[ref_ms][3]

    seq = [t for t in sorted(bars) if start_ms <= t <= end_ms]
    expected = (end_ms - start_ms) // MIN + 1
    if len(seq) != expected:
        return {"status": "NO EXECUTION - DATA",
                "reason": f"{expected - len(seq)} missing minutes in the execution window"}

    flush_thr = ref * (1 + s["flush"])
    reclaim_thr = flush_thr * (1 + s["reclaim"])

    armed = flush_ms = entry_ms = entry_px = None
    for t in seq:
        _, h, l, c = bars[t]
        if not armed:
            if l <= flush_thr:
                armed, flush_ms = True, t
                if c >= reclaim_thr:
                    entry_ms, entry_px = t, c
                    break
            continue
        if c >= reclaim_thr:
            entry_ms, entry_px = t, c
            break

    out = {"reference_ts": iso(ref_ms), "reference_px": ref,
           "flush_threshold": flush_thr, "reclaim_threshold": reclaim_thr,
           "flush_ts": iso(flush_ms) if flush_ms else None, "exposure": exposure}

    if flush_ms is None:
        return {**out, "status": "NO FILL", "reason": "no flush"}
    if entry_ms is None:
        return {**out, "status": "NO FILL", "reason": "flush armed, no reclaim"}

    out.update(entry_ts=iso(entry_ms), entry_px=entry_px)

    # Z1: exit evaluation starts with the NEXT candle. The entry candle's high/low
    # occurred before the position existed.
    after = [t for t in seq if t > entry_ms]
    if not after:
        return {**out, "status": "FILL", "exit_ts": iso(entry_ms), "exit_px": entry_px,
                "exit_reason": "TIME", "gross_return": 0.0,
                "net_return": exposure * (0.0 - 2 * FRICTION_PER_SIDE)}

    if name == "A1":
        sl_px, tp1_px, tp2_px = (entry_px * (1 + s["sl"]), entry_px * (1 + s["tp1"]),
                                 entry_px * (1 + s["tp2"]))
        out.update(sl_px=sl_px, tp1_px=tp1_px, tp2_px=tp2_px)
        hit_sl = hit_tp1 = None
        for t in after:
            _, h, l, _ = bars[t]
            if l <= sl_px:
                hit_sl = t; break
            if h >= tp1_px:
                hit_tp1 = t; break
        if hit_sl is not None:
            gross, reason, exit_ms, exit_px = s["sl"], "SL", hit_sl, sl_px
        elif hit_tp1 is None:
            exit_ms = after[-1]; exit_px = bars[exit_ms][3]
            gross, reason = exit_px / entry_px - 1, "TIME"
        else:
            # Z2: adverse-first inside the TP1 candle itself
            if bars[hit_tp1][2] <= entry_px:
                back, reason, exit_ms, exit_px = 0.0, "TP1+BE(same-minute)", hit_tp1, entry_px
            else:
                rest = [t for t in after if t > hit_tp1]
                be = tp2 = None
                for t in rest:
                    _, h, l, _ = bars[t]
                    if l <= entry_px:
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
        sl_px, tp_px = entry_px * (1 + s["sl"]), entry_px * (1 + s["tp"])
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

    return {**out, "status": "FILL", "exit_ts": iso(exit_ms), "exit_px": exit_px,
            "exit_reason": reason, "gross_return": gross,
            "net_return": exposure * (gross - 2 * FRICTION_PER_SIDE)}


def load_ledger(name):
    p = LEDGER / f"{name}.json"
    if p.exists():
        led = json.loads(p.read_text())
        led.setdefault("settlements", [])
        return led
    return {"strategy": name, "starting_capital": 10000.0, "capital": 10000.0,
            "observations": 0, "fills": 0, "settlements": [], "trades": []}


def apply_settlement(name, exec_date, result, coin):
    """Idempotent: a settlement id is applied at most once, ever."""
    sid = f"{exec_date.isoformat()}:{name}"
    led = load_ledger(name)
    if sid in led["settlements"]:
        print(f"  {name}: settlement {sid} already applied — skipped")
        return led
    status = result.get("status")
    if status in ("FILL", "NO FILL", "NO EXECUTION - DATA", "NO POSITION"):
        led["observations"] += 1
    if status == "FILL":
        before = led["capital"]
        led["capital"] = before * (1 + result["net_return"])
        led["fills"] += 1
        led["trades"].append({
            "execution_date": exec_date.isoformat(), "coin": coin,
            "entry_ts": result["entry_ts"], "entry_px": result["entry_px"],
            "exit_ts": result["exit_ts"], "exit_px": result["exit_px"],
            "exit_reason": result["exit_reason"], "exposure": result["exposure"],
            "gross_return": result["gross_return"], "net_return": result["net_return"],
            "capital_before": round(before, 2), "capital_after": round(led["capital"], 2)})
    led["settlements"].append(sid)
    (LEDGER / f"{name}.json").write_text(json.dumps(led, indent=2))
    return led


def settle(exec_date):
    sig_path = SIG / f"{exec_date.isoformat()}.json"
    exe_path = EXEC / f"{exec_date.isoformat()}.json"
    if exe_path.exists():
        return False                                   # execution records are immutable
    rec = json.loads(sig_path.read_text())
    state = rec.get("locked_state")

    # NOT OBSERVED is outside the strategy state machine: no execution record,
    # no observation, and the day stays retriable.
    if state == "NOT OBSERVED":
        print(f"{exec_date}  NOT OBSERVED signal — excluded from the sample, no settlement")
        return False

    if state in COUNTED:
        block = {"execution_date": exec_date.isoformat(), "locked_state": state,
                 "status": "NO POSITION", "settled_at_utc": datetime.now(timezone.utc).isoformat(),
                 "strategies": {n: {"status": "NO POSITION"} for n in SPEC}}
        for name in SPEC:
            apply_settlement(name, exec_date, {"status": "NO POSITION"}, None)
        exe_path.write_text(json.dumps(block, indent=2))
        print(f"{exec_date}  {state}  -> no position, observation counted")
        return True

    if state not in ("HOLO", "PUMP"):
        print(f"{exec_date}  unrecognised locked_state '{state}' — skipped")
        return False

    end_ms = ts_ms(exec_date + timedelta(days=1), 17, 59)
    if datetime.now(timezone.utc).timestamp() * 1000 < end_ms + MIN:
        print(f"{exec_date}  trading window still open -> deferred")
        return False

    symbol = SYMBOL[state]
    try:
        bars = fetch(symbol, ts_ms(exec_date, 18, 0), end_ms)
    except ObserverFailure as exc:
        print(f"{exec_date}  NOT OBSERVED (observer failure: {exc}) -> will retry")
        return False

    print(f"{exec_date}  {state} ({symbol})  {len(bars)} bars fetched")
    block = {"execution_date": exec_date.isoformat(), "coin": state, "symbol": symbol,
             "settled_at_utc": datetime.now(timezone.utc).isoformat(), "strategies": {}}
    for name in SPEC:
        r = run_strategy(name, bars, exec_date, float(rec[SPEC[name]["expo"]]))
        block["strategies"][name] = r
        led = apply_settlement(name, exec_date, r, state)
        print(f"  {name}: {r['status']} {r.get('exit_reason','')} "
              f"net {r.get('net_return', 0)*100:+.2f}%  capital ${led['capital']:,.2f}")
    exe_path.write_text(json.dumps(block, indent=2))
    return True


def main():
    if len(sys.argv) > 1:
        settle(date.fromisoformat(sys.argv[1])); return
    pending = sorted(p.stem for p in SIG.glob("*.json")
                     if not (EXEC / p.name).exists())
    if not pending:
        print("No unsettled signal records"); return
    for d in pending:
        settle(date.fromisoformat(d))


if __name__ == "__main__":
    main()
