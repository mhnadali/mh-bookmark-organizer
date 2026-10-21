#!/usr/bin/env python3
"""Experiment C: feed real Chromium deliberately damaged Bookmarks files and record what it does with them.

usage: exp_c_cases.py <skill_dir> <base_bookmarks.json> <profile_skeleton_dir> <workdir> [case-name ...] [--bin chromium]

For every case: copy the skeleton profile, drop the mutated file in as Default/Bookmarks, start Chromium (probe
extension), read chrome.bookmarks.getTree(), then create+delete one bookmark so Chromium rewrites the file, quit, and
analyse (ids kept? guids kept? content kept? checksum of the rewritten file valid? Bookmarks.bak created?).
Output: <workdir>/results.json and a text table on stdout.
"""
import copy
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

skill, base_path, skeleton, workdir = sys.argv[1:5]
rest = sys.argv[5:]
bin_args = []
if "--bin" in rest:
    i = rest.index("--bin")
    bin_args = ["--bin", rest[i + 1]]
    del rest[i:i + 2]
wanted = rest
sys.path.insert(0, os.path.join(skill, "scripts"))
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
from mhbm import formats  # noqa: E402

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import cmp_trees  # noqa: E402

base = json.loads(Path(base_path).read_text(encoding="utf-8"))


def dump(d):
    return json.dumps(d, indent=3, ensure_ascii=False).encode("utf-8")


def fix(d):
    d["checksum"] = formats.chrome_checksum(d["roots"])
    return d


def urls(d):
    out = []

    def w(n):
        if n.get("type") == "url":
            out.append(n)
        for c in n.get("children", []):
            w(c)
    for r in d["roots"].values():
        w(r)
    return out


def folders(d):
    out = []

    def w(n, top=True):
        if n.get("type") == "folder" and not top:
            out.append(n)
        for c in n.get("children", []):
            w(c, False)
    for r in d["roots"].values():
        w(r)
    return out


def mut(fn, checksum="fix"):
    def make():
        d = copy.deepcopy(base)
        fn(d)
        if checksum == "fix":
            try:
                fix(d)
            except Exception as e:  # a structure our own checksum cannot even walk
                d["checksum"] = "0" * 32
        return dump(d)
    return make


def set_(d, key, val):
    d[key] = val


cases = {}
cases["c00_control_valid"] = lambda: dump(base)
cases["c01_checksum_wrong"] = mut(lambda d: d.__setitem__("checksum", "0" * 32), checksum=None)
cases["c02_checksum_missing"] = mut(lambda d: d.pop("checksum"), checksum=None)
cases["c03_checksum_uppercase"] = mut(lambda d: d.__setitem__("checksum", base["checksum"].upper()), checksum=None)
cases["c04_checksum_wrong_but_ids_are_fine_title_changed"] = mut(lambda d: urls(d)[0].__setitem__("name", urls(d)[0]["name"] + " edited"), checksum=None)
cases["c05_duplicate_ids"] = mut(lambda d: urls(d)[1].__setitem__("id", urls(d)[0]["id"]))
cases["c06_id_zero"] = mut(lambda d: urls(d)[0].__setitem__("id", "0"))
cases["c07_id_non_numeric"] = mut(lambda d: urls(d)[0].__setitem__("id", "abc"))
cases["c08_id_missing"] = mut(lambda d: urls(d)[0].pop("id"))
cases["c09_id_is_json_number"] = mut(lambda d: urls(d)[0].__setitem__("id", int(urls(d)[0]["id"])))
cases["c10_id_negative"] = mut(lambda d: urls(d)[0].__setitem__("id", "-5"))
cases["c11_id_huge"] = mut(lambda d: urls(d)[0].__setitem__("id", "99999999999999999999"))
cases["c12_guid_not_a_guid"] = mut(lambda d: urls(d)[0].__setitem__("guid", "not-a-guid"))
cases["c13_guid_duplicate"] = mut(lambda d: urls(d)[1].__setitem__("guid", urls(d)[0]["guid"]))
cases["c14_guid_uppercase"] = mut(lambda d: urls(d)[0].__setitem__("guid", urls(d)[0]["guid"].upper()))
cases["c15_guid_missing"] = mut(lambda d: urls(d)[0].pop("guid"))
cases["c16_guid_not_uuid_v4_v5"] = mut(lambda d: urls(d)[0].__setitem__("guid", "00000000-0000-0000-0000-000000000001"))
cases["c17_root_guid_wrong"] = mut(lambda d: d["roots"]["bookmark_bar"].__setitem__("guid", "11111111-2222-4333-8444-555555555555"))
cases["c18_root_guid_missing"] = mut(lambda d: d["roots"]["other"].pop("guid"))
cases["c19_unknown_node_field"] = mut(lambda d: urls(d)[0].__setitem__("surprise", {"a": [1, 2]}))
cases["c20_unknown_top_level_field"] = mut(lambda d: d.__setitem__("future_field", "x"))
cases["c21_bom"] = lambda: b"\xef\xbb\xbf" + dump(base)
cases["c22_truncated_half"] = lambda: dump(base)[: len(dump(base)) // 2]
cases["c23_empty_file"] = lambda: b""
cases["c24_missing_synced_root"] = mut(lambda d: d["roots"].pop("synced"))
cases["c25_missing_other_root"] = mut(lambda d: d["roots"].pop("other"))
cases["c26_missing_bar_root"] = mut(lambda d: d["roots"].pop("bookmark_bar"))
cases["c27_date_added_not_numeric"] = mut(lambda d: urls(d)[0].__setitem__("date_added", "yesterday"))
cases["c28_date_added_zero"] = mut(lambda d: urls(d)[0].__setitem__("date_added", "0"))
cases["c29_date_added_missing"] = mut(lambda d: urls(d)[0].pop("date_added"))
cases["c30_date_added_unix_seconds"] = mut(lambda d: urls(d)[0].__setitem__("date_added", "1700000000"))
cases["c31_url_empty"] = mut(lambda d: urls(d)[0].__setitem__("url", ""))
cases["c32_url_missing"] = mut(lambda d: urls(d)[0].pop("url"))
cases["c33_url_node_with_children"] = mut(lambda d: urls(d)[0].__setitem__("children", [copy.deepcopy(urls(d)[1])]))
cases["c34_type_unknown"] = mut(lambda d: urls(d)[0].__setitem__("type", "separator"))
cases["c35_type_missing"] = mut(lambda d: urls(d)[0].pop("type"))
cases["c36_name_missing"] = mut(lambda d: urls(d)[0].pop("name"))
cases["c37_name_not_string"] = mut(lambda d: urls(d)[0].__setitem__("name", 42))
cases["c38_version_2"] = mut(lambda d: d.__setitem__("version", 2))
cases["c39_version_missing"] = mut(lambda d: d.pop("version"))
cases["c40_noncanonical_urls"] = mut(lambda d: [urls(d)[0].__setitem__("url", "HTTPS://Example.COM"), urls(d)[1].__setitem__("url", "https://example.org/a b/ü?q=1#x"), urls(d)[2].__setitem__("url", "  https://spaces.example/  ")])
cases["c41_trailing_garbage"] = lambda: dump(base) + b"\n\x00\x00garbage"
cases["c42_crlf_and_minified"] = lambda: json.dumps(base, ensure_ascii=True, separators=(",", ":")).encode()
cases["c43_empty_roots_object"] = mut(lambda d: d.__setitem__("roots", {}), checksum=None)
cases["c44_roots_is_list"] = mut(lambda d: d.__setitem__("roots", []), checksum=None)
cases["c45_extra_root_key"] = mut(lambda d: d["roots"].__setitem__("custom_root", copy.deepcopy(d["roots"]["other"])))
cases["c46_children_not_list"] = mut(lambda d: d["roots"]["bookmark_bar"].__setitem__("children", "oops"), checksum=None)
cases["c47_deep_nesting_300"] = None  # built below
cases["c48_two_same_name_sibling_folders_unsorted"] = lambda: dump(base)
cases["c49_folder_with_url_field"] = mut(lambda d: folders(d)[0].__setitem__("url", "https://folder.example/"))
cases["c50_meta_info_garbage"] = mut(lambda d: urls(d)[0].__setitem__("meta_info", {"power_bookmark_meta": 5, "x": [1]}))


def deep():
    d = copy.deepcopy(base)
    node = d["roots"]["other"]
    for i in range(300):
        child = {"id": str(1000 + i), "name": f"d{i}", "type": "folder", "date_added": "13300000000000000", "date_modified": "0",
                 "date_last_used": "0", "guid": f"00000000-0000-4000-8000-{i:012x}", "children": []}
        node["children"].append(child)
        node = child
    node["children"].append({"id": "5000", "name": "bottom", "type": "url", "url": "https://bottom.example/", "date_added": "13300000000000000",
                             "date_last_used": "0", "guid": "00000000-0000-4000-8000-ffffffffffff"})
    return dump(fix(d))


cases["c47_deep_nesting_300"] = deep


def safe_verify(d):
    """verify_chrome, but a crash is a result too (it is a bug in verify_chrome: it must report problems, never raise)."""
    try:
        return formats.verify_chrome(d)[:3]
    except Exception as e:  # noqa: BLE001
        return [f"verify_chrome RAISED {type(e).__name__}: {e}"]


def load_any(raw: bytes):
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except Exception:
        return None


def run_case(name, make):
    cdir = Path(workdir) / name
    if cdir.exists():
        shutil.rmtree(cdir)
    cdir.mkdir(parents=True)
    udd = cdir / "udd"
    shutil.copytree(skeleton, udd)
    for stale in ("Bookmarks", "Bookmarks.bak"):
        p = udd / "Default" / stale
        if p.exists():
            p.unlink()
    raw = make()
    (udd / "Default" / "Bookmarks").write_bytes(raw)
    (cdir / "input.json").write_bytes(raw)
    t0 = time.time()
    proc = subprocess.run(["node", str(HERE / "exp_dump.js"), str(udd), str(cdir / "tree.json"), str(cdir / "chrome.log"), "--wait", "3500", "--touch", *bin_args],
                          capture_output=True, text=True, timeout=120)
    res = {"case": name, "seconds": round(time.time() - t0, 1), "node_rc": proc.returncode}
    if proc.returncode:
        res["node_err"] = proc.stderr[-300:]
        return res
    tree = json.loads((cdir / "tree.json").read_text())["tree"]
    loaded = cmp_trees.flat(tree)
    res["loaded_nodes"] = len(loaded)
    infile = load_any(raw)
    res["input_parses_as_json"] = infile is not None
    res["input_problems"] = (safe_verify(infile) if isinstance(infile, dict) else ["unparseable"])
    bm = udd / "Default" / "Bookmarks"
    res["files"] = sorted(p.name for p in (udd / "Default").iterdir() if "ookmark" in p.name)
    after = load_any(bm.read_bytes()) if bm.exists() else None
    res["rewritten"] = bm.exists() and bm.read_bytes() != raw
    if isinstance(after, dict):
        res["rewritten_verify"] = safe_verify(after)
        try:
            res["rewritten_nodes"] = len(cmp_trees.flat(after))
        except Exception as e:  # noqa: BLE001
            res["rewritten_nodes"] = f"flat raised {type(e).__name__}"
    if isinstance(infile, dict) and isinstance(infile.get("roots"), dict):
        try:
            fin = cmp_trees.flat(infile)
        except Exception as e:  # noqa: BLE001
            fin = None
            res["flat_error"] = str(e)[:80]
        if fin is not None:
            res["input_nodes"] = len(fin)
            # content (ignoring ids) kept?
            key = lambda f: sorted((p, k, t, u) for (p, i, k, t, u, d, nid) in f)
            res["content_kept_vs_input"] = key(fin) == key(loaded)
            if len(fin) == len(loaded):
                same = sum(1 for a, b in zip(fin, loaded) if a[6] == b[6] and a[2:5] == b[2:5])
                res["ids_equal_at_same_position"] = f"{same}/{len(fin)}"
            if isinstance(after, dict) and isinstance(res.get("rewritten_nodes"), int) and res["rewritten_nodes"] >= 1:
                def guids(d):
                    out = []

                    def w(n):
                        out.append(n.get("guid"))
                        for c in n.get("children", []):
                            w(c)
                    for k in ("bookmark_bar", "other", "synced"):
                        if isinstance(d["roots"].get(k), dict):
                            w(d["roots"][k])
                    return out
                ga, gb = guids(infile), guids(after)
                n = sum(1 for a, b in zip(ga, gb) if a == b)
                res["guids_equal_in_rewritten"] = f"{n}/{len(ga)}"
                res["rewritten_checksum_equals_input_checksum"] = infile.get("checksum") == after.get("checksum")
    return res


results = []
names = [n for n in cases if not wanted or any(w in n for w in wanted)]
for n in names:
    r = run_case(n, cases[n])
    results.append(r)
    print(json.dumps(r, ensure_ascii=False), flush=True)
Path(workdir, "results.json").write_text(json.dumps(results, indent=1, ensure_ascii=False))
