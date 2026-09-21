#!/usr/bin/env python3
"""Build the frozen monthly multi-coin universe from Binance Data Vision archives.

NO live exchangeInfo calls are used. Delisted symbols are discovered from the
public S3 archive listing. Every downloaded archive is verified against its
published .CHECKSUM before use.
"""
from __future__ import annotations
import csv, hashlib, io, json, re, time, urllib.parse, urllib.request, urllib.error, zipfile
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

S3 = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
CDN = "https://data.binance.vision"
ROOT = "data/futures/um"
NS = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
UA = {"User-Agent": "HOLO-PUMP-Quant-Lab/multicoin-universe-1.1"}
OUT = Path("research/multicoin_expansion")
START_MONTH = date(2025, 9, 1)
END_MONTH = date(2026, 8, 1)
DOWNLOAD_START = date(2025, 7, 1)
DOWNLOAD_END = date(2026, 8, 31)
TOP_N = 15
MIN_AGE_DAYS = 60

STABLECOIN_UNDERLYINGS = {
    "USDC", "BUSD", "TUSD", "USDP", "FDUSD", "DAI", "UST", "USTC", "USDE",
    "USDS", "PYUSD", "EURC", "AEUR", "XUSD", "USD1", "USDJ", "USDX", "SUSD",
}
INDEX_UNDERLYINGS = {"DEFI", "BTCDOM"}
# Frozen rule says leveraged-token products are excluded. Archive enumeration is
# already USDT-M perpetuals; no actual leveraged-token USDT-M perpetual has been
# evidenced. Keep the denylist explicit and empty unless archive evidence exists.
LEVERAGED_TOKEN_DENYLIST: set[str] = set()
# Audit only: the REMOVED heuristic, never used for eligibility.
REMOVED_LEVERAGED_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR")


def get(url: str, retries: int = 6) -> bytes | None:
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            last = e
        except Exception as e:
            last = e
        time.sleep(min(2 ** i, 30))
    raise RuntimeError(f"GET failed: {url}: {last}")


def s3_list(prefix: str, delimiter: str = ""):
    token = None
    while True:
        q = {"list-type": "2", "max-keys": "1000", "prefix": prefix}
        if delimiter:
            q["delimiter"] = delimiter
        if token:
            q["continuation-token"] = token
        raw = get(S3 + "?" + urllib.parse.urlencode(q))
        if raw is None:
            raise RuntimeError(f"S3 listing unavailable: {prefix}")
        root = ET.fromstring(raw)
        yield root
        if root.findtext("s3:IsTruncated", "false", NS) != "true":
            break
        token = root.findtext("s3:NextContinuationToken", None, NS)
        if not token:
            raise RuntimeError("Truncated S3 response without continuation token")


def list_prefixes(prefix: str) -> list[str]:
    out = []
    for root in s3_list(prefix, "/"):
        out.extend(x.text for x in root.findall("s3:CommonPrefixes/s3:Prefix", NS) if x.text)
    return sorted(set(out))


def list_keys(prefix: str) -> list[str]:
    out = []
    for root in s3_list(prefix):
        out.extend(x.text for x in root.findall("s3:Contents/s3:Key", NS) if x.text)
    return sorted(set(out))


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def verified_zip(key: str, manifest: list[dict]) -> bytes:
    url = f"{CDN}/{key}"
    blob = get(url)
    if blob is None:
        raise FileNotFoundError(key)
    chk = get(url + ".CHECKSUM")
    if chk is None:
        raise RuntimeError(f"Missing CHECKSUM: {key}")
    expected = chk.decode().strip().split()[0].lower()
    actual = sha256(blob)
    if actual != expected:
        raise RuntimeError(f"SHA256 mismatch: {key}: {actual} != {expected}")
    manifest.append({"key": key, "bytes": len(blob), "sha256": actual, "checksum_verified": True})
    return blob


def csv_rows(blob: bytes):
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        names = [n for n in z.namelist() if n.lower().endswith(".csv")]
        if len(names) != 1:
            raise RuntimeError(f"Expected one CSV, found {names}")
        with z.open(names[0]) as f:
            yield from csv.reader(io.TextIOWrapper(f, encoding="utf-8"))


def ts_ms(v: str) -> int:
    n = int(float(v))
    return n // 1000 if n > 10**14 else n


def month_iter(a: date, b: date):
    cur, end = date(a.year, a.month, 1), date(b.year, b.month, 1)
    while cur <= end:
        yield cur
        cur = date(cur.year + (cur.month == 12), 1 if cur.month == 12 else cur.month + 1, 1)


def month_str(d: date) -> str:
    return d.strftime("%Y-%m")


def underlying(symbol: str) -> str | None:
    return symbol[:-4] if symbol.endswith("USDT") else None


def eligibility_static(symbol: str):
    if not symbol.endswith("USDT"):
        return False, "not_USDT_quoted"
    u = underlying(symbol)
    if symbol in {"BTCUSDT", "ETHUSDT"}:
        return False, "excluded_BTC_ETH"
    if "_" in symbol:
        return False, "delivery_contract_not_perpetual"
    if u in STABLECOIN_UNDERLYINGS:
        return False, "stablecoin_underlying"
    if u in INDEX_UNDERLYINGS:
        return False, "index_product"
    if symbol in LEVERAGED_TOKEN_DENYLIST:
        return False, "leveraged_token_product"
    return True, None


def discover_symbols() -> list[str]:
    base = f"{ROOT}/monthly/klines/"
    return sorted(p[len(base):].strip("/") for p in list_prefixes(base))


def available_1d_month_keys(symbol: str) -> list[str]:
    return [k for k in list_keys(f"{ROOT}/monthly/klines/{symbol}/1d/")
            if k.endswith(".zip") and not k.endswith(".zip.CHECKSUM")]


def parse_month_from_key(key: str) -> date | None:
    m = re.search(r"-(\d{4})-(\d{2})\.zip$", key)
    return date(int(m.group(1)), int(m.group(2)), 1) if m else None


def first_trading_day(keys: list[str], manifest: list[dict]) -> date | None:
    pairs = sorted((parse_month_from_key(k), k) for k in keys if parse_month_from_key(k))
    if not pairs:
        return None
    blob = verified_zip(pairs[0][1], manifest)
    vals = []
    for r in csv_rows(blob):
        if r and r[0].strip().isdigit():
            vals.append(datetime.fromtimestamp(ts_ms(r[0]) / 1000, tz=timezone.utc).date())
    return min(vals) if vals else None


def load_volume_days(keys: list[str], manifest: list[dict]) -> dict[date, float]:
    wanted = set(month_iter(DOWNLOAD_START, DOWNLOAD_END))
    by_day = defaultdict(float)
    for k in keys:
        mo = parse_month_from_key(k)
        if mo not in wanted:
            continue
        blob = verified_zip(k, manifest)
        for r in csv_rows(blob):
            if not r or not r[0].strip().isdigit():
                continue
            d = datetime.fromtimestamp(ts_ms(r[0]) / 1000, tz=timezone.utc).date()
            if DOWNLOAD_START <= d <= DOWNLOAD_END:
                by_day[d] += float(r[7])
    return dict(by_day)


def archive_span(keys: list[str]) -> tuple[str | None, str | None]:
    months = sorted(m for k in keys if (m := parse_month_from_key(k)))
    return (month_str(months[0]), month_str(months[-1])) if months else (None, None)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    manifest: list[dict] = []
    symbols = discover_symbols()
    print(f"Enumerated {len(symbols)} archive symbols")

    # Enumerate archive coverage BEFORE exclusions so classification decisions are auditable.
    keys_by_symbol, spans = {}, {}
    for i, sym in enumerate(symbols, 1):
        keys = available_1d_month_keys(sym)
        keys_by_symbol[sym] = keys
        first_m, last_m = archive_span(keys)
        spans[sym] = {"first_archive_month": first_m, "last_archive_month": last_m}
        if i % 25 == 0 or i == len(symbols):
            print(f"Archive enumeration progress: {i}/{len(symbols)} symbols")

    def audit_matches(predicate):
        return [{"symbol": s, **spans[s]} for s in symbols if predicate(s)]

    removed_suffix_matches = audit_matches(
        lambda s: s.endswith("USDT") and any((underlying(s) or "").endswith(x) and len(underlying(s) or "") > len(x)
                                                 for x in REMOVED_LEVERAGED_SUFFIXES))
    stable_matches = audit_matches(lambda s: underlying(s) in STABLECOIN_UNDERLYINGS)
    index_matches = audit_matches(lambda s: underlying(s) in INDEX_UNDERLYINGS)
    leveraged_denylist_matches = audit_matches(lambda s: s in LEVERAGED_TOKEN_DENYLIST)

    static, metadata, volumes = {}, {}, {}
    for i, sym in enumerate(symbols, 1):
        ok, reason = eligibility_static(sym)
        static[sym] = {"eligible_static": ok, "exclusion_reason": reason}
        if ok:
            keys = keys_by_symbol[sym]
            if not keys:
                static[sym] = {"eligible_static": False, "exclusion_reason": "no_monthly_1d_archive"}
            else:
                first = first_trading_day(keys, manifest)
                if first is None:
                    static[sym] = {"eligible_static": False, "exclusion_reason": "no_parseable_1d_kline"}
                else:
                    metadata[sym] = {"first_trading_day": first.isoformat(), **spans[sym]}
                    volumes[sym] = load_volume_days(keys, manifest)
        if i % 25 == 0 or i == len(symbols):
            print(f"Download/parse progress: {i}/{len(symbols)} symbols; verified files={len(manifest)}")

    months_out = {}
    previous_members: set[str] = set()
    for formation in month_iter(START_MONTH, END_MONTH):
        window_end, window_start = formation - timedelta(days=1), formation - timedelta(days=30)
        ranked, exclusions = [], []
        for sym in symbols:
            st = static.get(sym, {})
            if not st.get("eligible_static"):
                exclusions.append({"symbol": sym, "reason": st.get("exclusion_reason", "static_exclusion")})
                continue
            first = date.fromisoformat(metadata[sym]["first_trading_day"])
            age = (formation - first).days
            if age < MIN_AGE_DAYS:
                exclusions.append({"symbol": sym, "reason": "contract_age_lt_60_complete_days", "age_days": age})
                continue
            daily = volumes[sym]
            dates = [window_start + timedelta(days=j) for j in range(30)]
            missing = [d.isoformat() for d in dates if d not in daily]
            if missing:
                exclusions.append({"symbol": sym, "reason": "incomplete_30d_volume_window", "missing_days": missing})
                continue
            ranked.append({
                "symbol": sym,
                "quote_volume_30d": sum(daily[d] for d in dates),
                "contract_age_days": age,
                "first_trading_day": first.isoformat(),
                "last_available_archive_month": metadata[sym]["last_archive_month"],
                "delisting_date": None,
            })
        ranked.sort(key=lambda x: (-x["quote_volume_30d"], x["symbol"]))
        selected = ranked[:TOP_N]
        current = {x["symbol"] for x in selected}
        is_study_start = formation == START_MONTH
        for j, r in enumerate(selected, 1):
            r["rank"] = j
            # Membership transition flags are meaningful only inside the observed study.
            # At the left boundary we do not know whether a member entered before study start.
            r["entered_universe"] = None if is_study_start else (r["symbol"] not in previous_members)
            r["exited_universe"] = False  # finalized below after next month's membership is known
            r["boundary"] = "study_start" if is_study_start else None
        months_out[month_str(formation)] = {
            "formation_timestamp": f"{formation.isoformat()}T00:00:00Z",
            "volume_window_start": window_start.isoformat(),
            "volume_window_end": window_end.isoformat(),
            "eligible_count": len(ranked),
            "requested_top_n": TOP_N,
            "selected_count": len(selected),
            "members": selected,
            # Transition flags intentionally live only on selected members.
            "full_eligible_ranking": [
                {k: v for k, v in {**r, "rank": j + 1}.items()
                 if k not in {"entered_universe", "exited_universe", "boundary"}}
                for j, r in enumerate(ranked)
            ],
            "exclusions": exclusions,
        }
        previous_members = current

    month_names = list(months_out)
    for idx, mo in enumerate(month_names):
        is_study_end = idx + 1 == len(month_names)
        next_members = ({x["symbol"] for x in months_out[month_names[idx + 1]]["members"]}
                        if not is_study_end else set())
        for r in months_out[mo]["members"]:
            # At the right boundary we do not know whether a member exits after study end.
            r["exited_universe"] = None if is_study_end else (r["symbol"] not in next_members)
            if is_study_end:
                r["boundary"] = "study_end"

    selected_months = defaultdict(list)
    for mo, rec in months_out.items():
        for r in rec["members"]:
            selected_months[r["symbol"]].append(mo)
    symbol_summary = {}
    later_delisted = 0
    for sym, mos in sorted(selected_months.items()):
        last_archive = spans[sym]["last_archive_month"]
        inferred_later_delisted = bool(last_archive and last_archive < month_str(END_MONTH))
        later_delisted += int(inferred_later_delisted)
        symbol_summary[sym] = {
            "months_present": mos,
            "months_present_count": len(mos),
            "first_universe_month": mos[0],
            "last_universe_month": mos[-1],
            "first_archive_month": spans[sym]["first_archive_month"],
            "last_available_archive_month": last_archive,
            "delisting_date": None,
            "later_delisted_by_archive_end_before_2026_08": inferred_later_delisted,
        }

    symbols_with_2026_08_archive = sum(1 for sym in symbols if spans[sym]["last_archive_month"] >= "2026-08")
    print(f"Archive coverage audit: {symbols_with_2026_08_archive}/{len(symbols)} enumerated symbols have a 2026-08 archive")

    universe = {
        "schema_version": 2,
        "status": "FROZEN_UNIVERSE_STAGE1",
        "research_interval": ["2025-09-01", "2026-08-31"],
        "source": {"archive": CDN, "listing": S3, "live_exchangeInfo_used": False},
        "rule": {
            "formation": "00:00 UTC on first UTC day of each calendar month",
            "volume_window": "immediately preceding 30 complete UTC days",
            "ranking_metric": "sum quote asset volume from Binance USDT-M 1d klines",
            "eligibility": ["USDT-M perpetual only", "exclude BTCUSDT", "exclude ETHUSDT",
                            "exclude stablecoin-underlying pairs", "exclude index products",
                            "exclude leveraged-token products", "contract age >= 60 complete days"],
            "selection": "highest-ranked 15 eligible contracts; no discretionary replacement",
            "membership": "frozen for calendar month; no intra-month change",
        },
        "classification_constants": {
            "stablecoin_underlyings": sorted(STABLECOIN_UNDERLYINGS),
            "index_underlyings": sorted(INDEX_UNDERLYINGS),
            "leveraged_token_denylist": sorted(LEVERAGED_TOKEN_DENYLIST),
            "removed_suffix_heuristic": list(REMOVED_LEVERAGED_SUFFIXES),
            "removed_suffix_heuristic_archive_matches_not_excluded": removed_suffix_matches,
            "stablecoin_archive_matches_excluded": stable_matches,
            "index_archive_matches_excluded": index_matches,
            "leveraged_token_denylist_archive_matches_excluded": leveraged_denylist_matches,
        },
        "archive_symbols_enumerated": len(symbols),
        "survivorship_evidence": {
            "distinct_symbols_selected": len(selected_months),
            "symbols": symbol_summary,
            "selected_members_later_delisted_count": later_delisted,
            "delisting_date_note": "Exact delisting date is not inferred from monthly archive coverage; last_available_archive_month is reported where applicable.",
        },
        "months": months_out,
    }
    (OUT / "universe.json").write_text(json.dumps(universe, indent=2, sort_keys=True) + "\n")
    man = {
        "schema_version": 2,
        "stage": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "files_verified": len(manifest),
        "files": sorted(manifest, key=lambda x: x["key"]),
        "universe_sha256": hashlib.sha256((OUT / "universe.json").read_bytes()).hexdigest(),
    }
    (OUT / "stage1_manifest.json").write_text(json.dumps(man, indent=2, sort_keys=True) + "\n")
    print(f"Wrote {OUT/'universe.json'} and {OUT/'stage1_manifest.json'}")


if __name__ == "__main__":
    main()
