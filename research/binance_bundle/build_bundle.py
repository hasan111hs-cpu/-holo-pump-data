#!/usr/bin/env python3
"""Download ORIGINAL Binance Vision spot 1m monthly archives + official .CHECKSUM files,
verify SHA-256, and pack them UNCHANGED (stored, not recompressed) into one bundle per symbol."""
import hashlib, json, urllib.request, zipfile
from pathlib import Path

BASE = "https://data.binance.vision/data/spot/monthly/klines"
SYMBOLS = ["ENAUSDT", "PUMPUSDT", "HOLOUSDT"]
MONTHS = [f"2025-{m:02d}" for m in range(9, 13)] + [f"2026-{m:02d}" for m in range(1, 9)]
out = Path("bundle_out"); out.mkdir(exist_ok=True)
manifest = {"source": BASE, "files": []}

def get(url):
    with urllib.request.urlopen(url, timeout=180) as r:
        return r.read()

for sym in SYMBOLS:
    bundle = out / f"{sym}_binance_vision_spot_1m_2025-09_2026-08.zip"
    with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_STORED) as z:
        for month in MONTHS:
            name = f"{sym}-1m-{month}.zip"
            url = f"{BASE}/{sym}/1m/{name}"
            blob = get(url)
            chk = get(url + ".CHECKSUM")
            expected = chk.decode().split()[0].lower()
            actual = hashlib.sha256(blob).hexdigest()
            if actual != expected:
                raise SystemExit(f"CHECKSUM MISMATCH {name}")
            z.writestr(name, blob)
            z.writestr(name + ".CHECKSUM", chk)
            manifest["files"].append({"file": name, "url": url, "sha256": actual, "bytes": len(blob)})
            print("ok", name, len(blob), actual, flush=True)
    print(bundle, bundle.stat().st_size, flush=True)
(out / "MANIFEST.json").write_text(json.dumps(manifest, indent=1))
