#!/usr/bin/env python3
"""Make an sdist byte-for-byte reproducible: ``python normalize_sdist.py dist/name-1.2.3.tar.gz`` (rewrites the file in place).

setuptools honours SOURCE_DATE_EPOCH for wheels but not for sdists: the files it generates (PKG-INFO, setup.cfg, *.egg-info) carry
the build time and the gzip header does too. This repacks the archive with sorted members, one fixed timestamp, no owner names and
fixed permissions. The content of every file is untouched. Standard library only.
"""
import gzip
import io
import os
import sys
import tarfile


def normalize(path: str, epoch: int) -> None:
    with tarfile.open(path, "r:gz") as src:
        members = sorted(src.getmembers(), key=lambda m: m.name)
        blobs = {m.name: (src.extractfile(m).read() if m.isfile() else None) for m in members}
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as out:
        for m in members:
            m.mtime, m.uid, m.gid, m.uname, m.gname = epoch, 0, 0, "", ""
            m.mode = 0o755 if m.isdir() or (m.mode & 0o111) else 0o644
            m.pax_headers = {}
            out.addfile(m, io.BytesIO(blobs[m.name]) if blobs[m.name] is not None else None)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f, gzip.GzipFile(filename="", mode="wb", fileobj=f, mtime=epoch, compresslevel=9) as gz:
        gz.write(raw.getvalue())
    os.replace(tmp, path)


if __name__ == "__main__":
    if len(sys.argv) != 2 or "SOURCE_DATE_EPOCH" not in os.environ:
        sys.exit("usage: SOURCE_DATE_EPOCH=<seconds> python normalize_sdist.py dist/<name>.tar.gz")
    normalize(sys.argv[1], int(os.environ["SOURCE_DATE_EPOCH"]))
