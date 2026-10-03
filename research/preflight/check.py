"""Pre-flight for tonight: exercise the real settlement price-path fetch (2 pages) and preview the open positions."""
import json, sys
from datetime import timedelta
sys.path.insert(0, "scripts")
import simple_book as b
now = b.utcnow()
boundary = b.boundary_of("2026-10-02")
out = {"checked_at_utc": b.iso(now), "tracks": {}}
for track, edir in (("official", "entries"), ("instant", "instant_entries")):
    entries = b.load(b.BOOK / edir / "2026-10-02.json")
    start = boundary + b.TRACKS[track]["scan_offset"]
    end = now.replace(second=0, microsecond=0) - timedelta(minutes=1)
    bars = b.klines("HOLOUSDT", start, end)
    expected = int((end - start).total_seconds() // 60)
    res = {"minutes_expected": expected, "minutes_fetched": len(bars), "strategies": {}}
    last_bar = bars[max(bars)]
    for name in ("SimpleH", "SimpleH2"):
        row = entries["strategies"][name]
        r = b.settle_position(row, b.STRATEGIES[name], bars, (last_bar[3],), start, end)
        res["strategies"][name] = {"so_far": r["status"] if r["status"] != "TIME_EXIT" else "STILL_OPEN",
                                   "entry": row["entry_price"], "last_price": last_bar[3],
                                   "exit_time_dubai": r["exit_time_dubai"] if r["status"] != "TIME_EXIT" else None,
                                   "net_return_pct_if_closed_now": r["net_return_pct"]}
    assert len(bars) == expected, (len(bars), expected)
    out["tracks"][track] = res
json.dump(out, open("research/preflight/result.json", "w"), indent=1)
print(json.dumps(out, indent=1))
