#!/usr/bin/env python3
"""
Simple Book - decisions, entries, settlement, ledger and nightly report for the six
frozen Simple strategies. Strategy rules are NOT changed here; this is record-keeping.

Layers (all under simple_book/, named by the UTC date of the 18:00 UTC boundary):
  decisions/<date>.json    immutable. BUY / NO TRADE / DATA_ERROR per strategy, taken only
                           from a valid simple_fast/<date>.json.
  entries/<date>.json      immutable. For each BUY: entry = OPEN of the 18:05 UTC minute
                           (22:05 Dubai), a price a person could act on after the 22:02 report.
  settlements/<date>.json  immutable. What happened to the positions entered on <date>:
                           TP, SL or time exit at the next 18:00 UTC. Only ENTERED positions
                           can produce P&L; NO TRADE / DATA_ERROR can never be settled.
  ledger.json              rebuilt from the immutable files on every run.
Report:
  simple_reports/<date>.json, <date>.md and latest.json - one file with all six strategies.

Costs: 0.1% fee per side. TP and SL touched in the same 1-minute candle counts as SL.

Commands:
  simple_book.py nightly   after the signal job: decide, settle anything due, ledger, report
  simple_book.py entry     at/after 18:05 UTC: finalise decision, capture entries, report
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

UTC = timezone.utc
DUBAI = timezone(timedelta(hours=4))
BASE_URL = "https://data-api.binance.vision/api/v3/klines"
ROOT = Path(os.environ.get("SIMPLE_ROOT", "."))
BOOK = ROOT / "simple_book"
REPORTS = ROOT / "simple_reports"

SCHEMA_VERSION = 1
START_EQUITY = 10000.0
FEE_PER_SIDE = 0.001            # 0.1% buy + 0.1% sell
ENTRY_OFFSET = timedelta(minutes=5)   # entry at 18:05 UTC = 22:05 Dubai
BOUNDARY_HOUR_UTC = 18
EARLY_TOLERANCE = timedelta(minutes=10)

# Frozen strategy definitions - DO NOT CHANGE.
STRATEGIES = {
    "SimpleP":  {"symbol": "PUMPUSDT", "gate_pct": 6.5, "tp_pct": 4.0,  "sl_pct": 2.5},
    "SimpleP2": {"symbol": "PUMPUSDT", "gate_pct": 6.5, "tp_pct": 2.5,  "sl_pct": 5.5},
    "SimpleE":  {"symbol": "ENAUSDT",  "gate_pct": 1.0, "tp_pct": 4.0,  "sl_pct": 2.5},
    "SimpleE2": {"symbol": "ENAUSDT",  "gate_pct": 1.0, "tp_pct": 6.75, "sl_pct": 6.75},
    "SimpleH":  {"symbol": "HOLOUSDT", "gate_pct": 2.0, "tp_pct": 4.0,  "sl_pct": 2.5},
    "SimpleH2": {"symbol": "HOLOUSDT", "gate_pct": 2.0, "tp_pct": 6.0,  "sl_pct": 5.0},
}


# ------------------------------------------------------------------ helpers
def utcnow():
    return datetime.now(UTC)


def iso(dt):
    return dt.isoformat()


def log(msg):
    print(f"[{utcnow().strftime('%H:%M:%S')}Z] {msg}", flush=True)


def day(boundary):
    return boundary.strftime("%Y-%m-%d")


def boundary_of(date_str):
    return datetime.strptime(date_str, "%Y-%m-%d").replace(hour=BOUNDARY_HOUR_UTC, tzinfo=UTC)


def boundary_for(now):
    today = now.replace(hour=BOUNDARY_HOUR_UTC, minute=0, second=0, microsecond=0)
    return today if now >= today - EARLY_TOLERANCE else today - timedelta(days=1)


def load(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def write_new(path, obj):
    """Immutable write: create only if absent."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(path, "x", encoding="utf-8") as fh:
            json.dump(obj, fh, indent=2)
            fh.write("\n")
        log(f"written {path}")
        return True
    except FileExistsError:
        return False


def write_if_changed(path, obj, ignore=("generated_at_utc",)):
    """Rewrite a derived file only when its content (ignoring timestamps) changed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    old = load(path)
    strip = lambda d: {k: v for k, v in (d or {}).items() if k not in ignore}
    if old is not None and strip(old) == strip(obj):
        return False
    path.write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8")
    log(f"updated {path}")
    return True


def http_json(url, retries=6):
    last = None
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(url, timeout=15) as resp:
                return json.loads(resp.read().decode())
        except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as exc:
            last = exc
            log(f"HTTP attempt {attempt}/{retries} failed: {exc!r}")
            time.sleep(min(2 ** (attempt - 1), 8))
    raise RuntimeError(f"Binance request failed: {last!r}")


def klines(symbol, start, end_exclusive):
    """1m klines with open time in [start, end_exclusive). Returns {open_ms: (o, h, l, c)}."""
    out = {}
    cur = int(start.timestamp() * 1000)
    end_ms = int(end_exclusive.timestamp() * 1000) - 1
    while cur <= end_ms:
        q = urllib.parse.urlencode({"symbol": symbol, "interval": "1m",
                                    "startTime": cur, "endTime": end_ms, "limit": 1000})
        rows = http_json(f"{BASE_URL}?{q}")
        if not rows:
            break
        for r in rows:
            out[int(r[0])] = (float(r[1]), float(r[2]), float(r[3]), float(r[4]))
        cur = int(rows[-1][0]) + 60_000
    return out


def minute_bar(symbol, t, wait_seconds=0):
    """The 1m bar opening at t (its OPEN is fixed as soon as the minute starts)."""
    deadline = utcnow() + timedelta(seconds=wait_seconds)
    while True:
        bars = klines(symbol, t, t + timedelta(minutes=1))
        bar = bars.get(int(t.timestamp() * 1000))
        if bar is not None or utcnow() >= deadline:
            return bar
        time.sleep(2)


# ----------------------------------------------------------------- decision
def signal_check(sig, boundary):
    if sig is None:
        return False, "signal file missing or unreadable"
    checks = [
        (sig.get("schema_version") == 2, "schema_version is not 2"),
        (sig.get("boundary_utc") == iso(boundary), "boundary_utc does not match"),
        (sig.get("status") == "OK", "status is not OK"),
        (sig.get("live_eligible") is True, "live_eligible is not true (late or incomplete)"),
        (sig.get("immutable") is True, "immutable is not true"),
    ]
    for ok, reason in checks:
        if not ok:
            return False, reason
    return True, "valid"


def decide(boundary, final):
    """Write the immutable six-strategy decision record. Returns it (or None if not ready)."""
    path = BOOK / "decisions" / f"{day(boundary)}.json"
    existing = load(path)
    if existing:
        return existing
    sig_path = ROOT / "simple_fast" / f"{day(boundary)}.json"
    sig = load(sig_path)
    valid, reason = signal_check(sig, boundary)
    if not valid and not final:
        log(f"signal not valid yet ({reason}); decision deferred")
        return None
    strategies = {}
    for name, cfg in STRATEGIES.items():
        row = {"symbol": cfg["symbol"], "gate_pct": cfg["gate_pct"],
               "tp_pct": cfg["tp_pct"], "sl_pct": cfg["sl_pct"]}
        info = (sig or {}).get("symbols", {}).get(cfg["symbol"], {}) if valid else {}
        if not valid:
            row.update({"decision": "DATA_ERROR", "reason": f"No valid signal file: {reason}"})
        elif info.get("status") != "OK" or info.get("decision") not in ("BUY", "NO TRADE"):
            row.update({"decision": "DATA_ERROR", "reason": "Coin data not OK in signal file"})
        else:
            ret = info["return_pct"]
            row.update({
                "decision": info["decision"], "return_pct": ret,
                "reason": (f"24h return {ret:+.2f}% is at or above the {cfg['gate_pct']:+.1f}% gate"
                           if info["decision"] == "BUY"
                           else f"24h return {ret:+.2f}% is below the {cfg['gate_pct']:+.1f}% gate"),
            })
        strategies[name] = row
    doc = {
        "schema_version": SCHEMA_VERSION, "record": "DECISION", "immutable": True,
        "boundary_utc": iso(boundary), "boundary_dubai": iso(boundary.astimezone(DUBAI)),
        "decided_at_utc": iso(utcnow()),
        "signal_file": str(sig_path.relative_to(ROOT)), "signal_valid": valid, "signal_check": reason,
        "signal_generated_at_utc": (sig or {}).get("generated_at_utc"),
        "signal_latency_seconds": (sig or {}).get("latency_seconds_after_boundary"),
        "strategies": strategies,
    }
    write_new(path, doc)
    return load(path)


# -------------------------------------------------------------------- entry
def capture_entries(boundary, decisions):
    path = BOOK / "entries" / f"{day(boundary)}.json"
    existing = load(path)
    if existing:
        return existing
    entry_time = boundary + ENTRY_OFFSET
    expiry = boundary + timedelta(days=1)
    bars, strategies = {}, {}
    for name, cfg in STRATEGIES.items():
        dec = decisions["strategies"][name]
        row = {"symbol": cfg["symbol"], "decision": dec["decision"]}
        if dec["decision"] != "BUY":
            row["status"] = "NO_POSITION"
        else:
            sym = cfg["symbol"]
            if sym not in bars:
                try:
                    bars[sym] = minute_bar(sym, entry_time, wait_seconds=90)
                except Exception as exc:  # noqa: BLE001
                    log(f"{sym}: entry fetch failed {exc!r}")
                    bars[sym] = None
            bar = bars[sym]
            if bar is None:
                row.update({"status": "ENTRY_DATA_ERROR",
                            "reason": "22:05 Dubai price unavailable; no position opened"})
            else:
                entry = bar[0]
                row.update({
                    "status": "ENTERED", "entry_price": entry, "entry_time_utc": iso(entry_time),
                    "entry_time_dubai": iso(entry_time.astimezone(DUBAI)),
                    "tp_price": entry * (1 + cfg["tp_pct"] / 100),
                    "sl_price": entry * (1 - cfg["sl_pct"] / 100),
                    "expiry_utc": iso(expiry),
                })
        strategies[name] = row
    now = utcnow()
    doc = {
        "schema_version": SCHEMA_VERSION, "record": "ENTRY", "immutable": True,
        "boundary_utc": iso(boundary),
        "entry_rule": "OPEN of the Binance Spot 1m candle at 18:05 UTC (22:05 Dubai)",
        "fee_per_side": FEE_PER_SIDE,
        "captured_at_utc": iso(now),
        "captured_late": now > entry_time + timedelta(minutes=3),
        "strategies": strategies,
    }
    write_new(path, doc)
    return load(path)


# --------------------------------------------------------------- settlement
def settle_position(row, cfg, bars, expiry_bar, entry_time, expiry):
    entry, tp, sl = row["entry_price"], row["tp_price"], row["sl_price"]
    status, exit_price, exit_time, ambiguous, missing = "TIME_EXIT", expiry_bar[0], expiry, False, 0
    t = entry_time
    while t < expiry:
        b = bars.get(int(t.timestamp() * 1000))
        if b is None:
            missing += 1
        else:
            hit_tp, hit_sl = b[1] >= tp, b[2] <= sl
            if hit_sl:                                  # same-minute TP+SL counts as SL
                status, exit_price, exit_time, ambiguous = "SL", sl, t, hit_tp
                break
            if hit_tp:
                status, exit_price, exit_time = "TP", tp, t
                break
        t += timedelta(minutes=1)
    gross = exit_price / entry - 1
    net = (exit_price * (1 - FEE_PER_SIDE)) / (entry * (1 + FEE_PER_SIDE)) - 1
    return {
        "status": status, "ambiguous_same_minute": ambiguous,
        "entry_price": entry, "entry_time_utc": row["entry_time_utc"],
        "exit_price": exit_price, "exit_time_utc": iso(exit_time),
        "exit_time_dubai": iso(exit_time.astimezone(DUBAI)),
        "gross_return_pct": round(gross * 100, 4), "net_return_pct": round(net * 100, 4),
        "fee_per_side": FEE_PER_SIDE, "missing_minutes": missing,
    }


def settle_all(now):
    """Settle every entries file whose 24h window has ended and that is not settled yet."""
    for epath in sorted((BOOK / "entries").glob("*.json")):
        d = epath.stem
        spath = BOOK / "settlements" / f"{d}.json"
        if spath.exists():
            continue
        boundary = boundary_of(d)
        expiry = boundary + timedelta(days=1)
        if now < expiry:
            continue
        entries = load(epath)
        entry_time = boundary + ENTRY_OFFSET
        cache, strategies, ok = {}, {}, True
        for name, cfg in STRATEGIES.items():
            row = entries["strategies"][name]
            if row["status"] != "ENTERED":
                strategies[name] = {"status": "NO_POSITION", "decision": row["decision"]}
                continue
            sym = cfg["symbol"]
            if sym not in cache:
                try:
                    bars = klines(sym, entry_time, expiry)
                    xbar = minute_bar(sym, expiry, wait_seconds=60)
                    expected = int((expiry - entry_time).total_seconds() // 60)
                    cache[sym] = (bars, xbar) if xbar and len(bars) >= expected * 0.98 else None
                except Exception as exc:  # noqa: BLE001
                    log(f"{sym}: settlement fetch failed {exc!r}")
                    cache[sym] = None
            if cache[sym] is None:
                ok = False
                break
            bars, xbar = cache[sym]
            strategies[name] = settle_position(row, cfg, bars, xbar, entry_time, expiry)
            strategies[name]["symbol"] = sym
        if not ok:
            log(f"settlement for {d} postponed: price path incomplete (will retry next run)")
            continue
        write_new(spath, {
            "schema_version": SCHEMA_VERSION, "record": "SETTLEMENT", "immutable": True,
            "entry_boundary_utc": iso(boundary), "expiry_boundary_utc": iso(expiry),
            "settled_at_utc": iso(utcnow()),
            "rules": "TP if high >= TP; SL if low <= SL; same minute = SL; else exit at OPEN of the "
                     "next 18:00 UTC minute; net = exit*(1-fee)/(entry*(1+fee))-1",
            "strategies": strategies,
        })


# ------------------------------------------------------------------- ledger
def rebuild_ledger():
    book = {n: {"symbol": c["symbol"], "equity": START_EQUITY, "trades": 0, "wins": 0, "losses": 0,
                "open_position": None, "history": []} for n, c in STRATEGIES.items()}
    for spath in sorted((BOOK / "settlements").glob("*.json")):
        s = load(spath)
        for name, r in s["strategies"].items():
            if r["status"] not in ("TP", "SL", "TIME_EXIT"):
                continue
            acct = book[name]
            before = acct["equity"]
            pnl = before * r["net_return_pct"] / 100
            acct["equity"] = before + pnl
            acct["trades"] += 1
            acct["wins" if r["net_return_pct"] > 0 else "losses"] += 1
            acct["history"].append({
                "entry_date": spath.stem, "status": r["status"],
                "entry_price": r["entry_price"], "exit_price": r["exit_price"],
                "net_return_pct": r["net_return_pct"], "net_pnl": round(pnl, 2),
                "equity_after": round(acct["equity"], 2),
            })
    for epath in sorted((BOOK / "entries").glob("*.json")):
        if (BOOK / "settlements" / epath.name).exists():
            continue
        e = load(epath)
        for name, r in e["strategies"].items():
            if r["status"] == "ENTERED":
                book[name]["open_position"] = {
                    "entry_date": epath.stem, "entry_price": r["entry_price"],
                    "entry_time_utc": r["entry_time_utc"], "tp_price": r["tp_price"],
                    "sl_price": r["sl_price"], "expiry_utc": r["expiry_utc"]}
    for acct in book.values():
        acct["equity"] = round(acct["equity"], 2)
        acct["cumulative_net_pnl"] = round(acct["equity"] - START_EQUITY, 2)
        acct["cumulative_return_pct"] = round((acct["equity"] / START_EQUITY - 1) * 100, 4)
        acct["win_rate_pct"] = round(acct["wins"] / acct["trades"] * 100, 2) if acct["trades"] else None
    ledger = {"schema_version": SCHEMA_VERSION, "record": "LEDGER", "generated_at_utc": iso(utcnow()),
              "start_equity": START_EQUITY, "fee_per_side": FEE_PER_SIDE, "strategies": book}
    write_if_changed(BOOK / "ledger.json", ledger)
    return ledger


# ------------------------------------------------------------------- report
def build_report(boundary, ledger):
    d = day(boundary)
    decisions = load(BOOK / "decisions" / f"{d}.json")
    entries = load(BOOK / "entries" / f"{d}.json")
    prev_d = day(boundary - timedelta(days=1))
    prev_settle = load(BOOK / "settlements" / f"{prev_d}.json")
    if decisions is None:
        stage = "WAITING_FOR_SIGNAL"
    elif entries is None and any(s["decision"] == "BUY" for s in decisions["strategies"].values()):
        stage = "DECISIONS_MADE_ENTRY_PENDING"
    else:
        stage = "FINAL"
    rows = {}
    for name, cfg in STRATEGIES.items():
        acct = ledger["strategies"][name]
        dec = (decisions or {}).get("strategies", {}).get(name)
        ent = (entries or {}).get("strategies", {}).get(name)
        prev = (prev_settle or {}).get("strategies", {}).get(name)
        last = acct["history"][-1] if acct["history"] and acct["history"][-1]["entry_date"] == prev_d else None
        row = {
            "symbol": cfg["symbol"], "gate_pct": cfg["gate_pct"], "tp_pct": cfg["tp_pct"], "sl_pct": cfg["sl_pct"],
            "decision": dec["decision"] if dec else "PENDING",
            "reason": dec["reason"] if dec else "Waiting for a valid signal file (deadline 22:05 Dubai)",
            "return_pct": (dec or {}).get("return_pct"),
            "entry": None, "previous_trade": None,
            "today_realized_net_pnl": last["net_pnl"] if last else 0.0,
            "cumulative_net_pnl": acct["cumulative_net_pnl"], "equity": acct["equity"],
            "trades": acct["trades"], "wins": acct["wins"], "losses": acct["losses"],
            "win_rate_pct": acct["win_rate_pct"], "open_position": acct["open_position"],
        }
        if dec and dec["decision"] == "BUY":
            if ent is None:
                row["entry"] = {"status": "PENDING", "note": "Entry price is recorded at 22:05 Dubai"}
            else:
                row["entry"] = {k: ent.get(k) for k in
                                ("status", "entry_price", "entry_time_utc", "entry_time_dubai",
                                 "tp_price", "sl_price", "expiry_utc", "reason") if k in ent}
        if prev and prev["status"] in ("TP", "SL", "TIME_EXIT"):
            row["previous_trade"] = {k: prev[k] for k in
                                     ("status", "ambiguous_same_minute", "entry_price", "exit_price",
                                      "exit_time_dubai", "gross_return_pct", "net_return_pct")}
        rows[name] = row
    report = {
        "schema_version": SCHEMA_VERSION, "record": "REPORT",
        "report_date": d, "boundary_utc": iso(boundary), "boundary_dubai": iso(boundary.astimezone(DUBAI)),
        "generated_at_utc": iso(utcnow()), "stage": stage, "final": stage == "FINAL",
        "signal_valid": (decisions or {}).get("signal_valid"),
        "signal_generated_at_utc": (decisions or {}).get("signal_generated_at_utc"),
        "rules": {"entry": "Binance Spot price at 22:05 Dubai (open of the 18:05 UTC 1m candle)",
                  "fee_per_side": FEE_PER_SIDE, "same_minute_tp_sl": "counts as SL",
                  "start_equity_each": START_EQUITY, "paper_trading_only": True},
        "strategies": rows,
    }
    changed = write_if_changed(REPORTS / f"{d}.json", report)
    write_if_changed(REPORTS / "latest.json", report)
    if changed or not (REPORTS / f"{d}.md").exists():
        (REPORTS / f"{d}.md").write_text(render_markdown(report), encoding="utf-8")
    return report


def render_markdown(r):
    fmt = lambda v, p=6: "-" if v is None else f"{v:.{p}g}"
    lines = [f"# Simple strategies - {r['report_date']} (22:00 Dubai)", "",
             f"Stage: **{r['stage']}** | signal valid: {r['signal_valid']} | generated {r['generated_at_utc']}", "",
             "| Strategy | Coin | Decision | 24h return | Entry (22:05) | TP | SL |", "|---|---|---|---|---|---|---|"]
    for n, s in r["strategies"].items():
        e = s["entry"] or {}
        ret = "-" if s["return_pct"] is None else f"{s['return_pct']:+.2f}%"
        lines.append(f"| {n} | {s['symbol']} | **{s['decision']}** | {ret} | "
                     f"{fmt(e.get('entry_price')) if e.get('status') != 'PENDING' else 'pending'} | "
                     f"{fmt(e.get('tp_price'))} | {fmt(e.get('sl_price'))} |")
    lines += ["", "| Strategy | Last trade | Net % | Today P&L | Cum. P&L | Equity | Trades | W/L | Win rate |",
              "|---|---|---|---|---|---|---|---|---|"]
    for n, s in r["strategies"].items():
        p = s["previous_trade"] or {}
        wr = "-" if s["win_rate_pct"] is None else f"{s['win_rate_pct']:.0f}%"
        net = "-" if not p else f"{p['net_return_pct']:+.2f}%"
        lines.append(f"| {n} | {p.get('status', '-')} | {net} | ${s['today_realized_net_pnl']:,.2f} | "
                     f"${s['cumulative_net_pnl']:,.2f} | ${s['equity']:,.2f} | {s['trades']} | "
                     f"{s['wins']}/{s['losses']} | {wr} |")
    lines += ["", "Entry = Binance Spot price at 22:05 Dubai. Fees 0.1% per side. "
                  "TP and SL in the same minute counts as SL. Paper trading only.", ""]
    return "\n".join(lines)


# --------------------------------------------------------------------- main
def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "nightly"
    now = utcnow()
    boundary = boundary_for(now)
    log(f"cmd={cmd} boundary={iso(boundary)}")
    if cmd == "entry":
        entry_time = boundary + ENTRY_OFFSET
        if utcnow() < entry_time + timedelta(seconds=2):
            wait = (entry_time + timedelta(seconds=2) - utcnow()).total_seconds()
            log(f"waiting {wait:.0f}s for 22:05 Dubai")
            time.sleep(wait)
        decisions = decide(boundary, final=True)
        capture_entries(boundary, decisions)
    elif cmd == "nightly":
        decide(boundary, final=utcnow() >= boundary + ENTRY_OFFSET)
    else:
        raise SystemExit(f"unknown command {cmd}")
    settle_all(utcnow())
    build_report(boundary, rebuild_ledger())
    return 0


if __name__ == "__main__":
    sys.exit(main())
