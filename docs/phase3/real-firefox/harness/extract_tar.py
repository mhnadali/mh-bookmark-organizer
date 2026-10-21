#!/usr/bin/env python3
"""Safely extract a tar archive into an EMPTY directory (tarfile 'data' filter refuses absolute paths, links outside, devices).
usage: python3 -I extract_tar.py <archive> <empty-dest> [expected-sha256]"""
import hashlib, os, sys, tarfile
arc, dest = sys.argv[1], sys.argv[2]
if len(sys.argv) > 3:
    h = hashlib.sha256(open(arc, "rb").read()).hexdigest()
    if h != sys.argv[3]:
        sys.exit(f"sha256 mismatch {h}")
if os.path.exists(dest) and os.listdir(dest):
    sys.exit("destination must be empty")
os.makedirs(dest, exist_ok=True)
with tarfile.open(arc) as t:
    t.extractall(dest, filter="data")
print("extracted", arc, "->", dest)
