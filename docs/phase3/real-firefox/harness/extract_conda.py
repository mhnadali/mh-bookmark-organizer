#!/usr/bin/env python3
"""Extract a .conda package (a zip of pkg-*.tar.zst + info-*.tar.zst) into an EMPTY directory after verifying its sha256.

usage: python3 -I extract_conda.py <pkg.conda> <expected-sha256> <empty-dest-dir>
Needs the `zstandard` wheel from PyPI (only this helper does; the code under test, mhbm, is standard library only).
The tarfile 'data' filter refuses absolute paths, links that leave the destination, devices and set-uid bits.
"""
import hashlib
import io
import sys
import tarfile
import zipfile
from pathlib import Path

import zstandard

pkg, want, dest = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
digest = hashlib.sha256(pkg.read_bytes()).hexdigest()
if digest != want:
    sys.exit(f"sha256 mismatch: {digest} != {want}")
if dest.exists() and any(dest.iterdir()):
    sys.exit("destination must be empty")
dest.mkdir(parents=True, exist_ok=True)
with zipfile.ZipFile(pkg) as z:
    for name in z.namelist():
        if name.endswith(".tar.zst"):
            raw = zstandard.ZstdDecompressor().stream_reader(io.BytesIO(z.read(name)))
            with tarfile.open(fileobj=raw, mode="r|") as t:
                t.extractall(dest, filter="data")
print("extracted", pkg.name, "to", dest)
