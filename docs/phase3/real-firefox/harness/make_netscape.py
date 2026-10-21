#!/usr/bin/env python3
"""Synthetic Netscape bookmark files used as the SOURCE that real Firefox imports.

* messy()  - tests/fixtures/gen_messy_library.py output (Chrome-flavoured: 592 bookmarks, 100 folders, Persian titles...)
* rich()   - hand-written, Firefox-flavoured: TAGS, SHORTCUTURL, <HR> separators, <DD> descriptions, the toolbar /
             unfiled roots, odd URL schemes, entities, a 3-digit-depth tree, a slash in a folder name, RTL/Persian text.

Nothing here comes from a real person: every host is *.example / example.org, every title is invented.
usage: make_netscape.py <outdir>   (writes messy.html and rich.html)
"""
from __future__ import annotations

import html
import sys
from pathlib import Path

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def messy() -> str:
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(FIXTURES))
    import gen_messy_library as g
    return g.generate()


def a(url, title, add=1600000000, mod=None, **attrs):
    extra = "".join(f' {k.upper()}="{html.escape(str(v), quote=True)}"' for k, v in attrs.items())
    mod = f' LAST_MODIFIED="{mod}"' if mod else ""
    return f'<DT><A HREF="{html.escape(url, quote=True)}" ADD_DATE="{add}"{mod}{extra}>{html.escape(title, quote=False)}</A>'


def rich() -> str:
    long_url = "https://long.example.org/" + "/".join(f"segment{i}" for i in range(300)) + "?q=" + "x" * 1500
    deep = []
    for d in range(1, 13):
        deep.append("    " * d + f'<DT><H3 ADD_DATE="1500000000">Level {d}</H3>')
        deep.append("    " * d + "<DL><p>")
    deep.append("    " * 13 + a("https://deep.example.org/bottom", "Bottom of a 12-level tree", 1500000123))
    for d in range(12, 0, -1):
        deep.append("    " * d + "</DL><p>")
    out = f"""<!DOCTYPE NETSCAPE-Bookmark-file-1>
<!-- This is an automatically generated file.
     It will be read and overwritten.
     DO NOT EDIT! -->
<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">
<TITLE>Bookmarks</TITLE>
<H1>Bookmarks Menu</H1>

<DL><p>
    <DT><H3 ADD_DATE="1400000000" LAST_MODIFIED="1400000001">Menu folder one</H3>
    <DL><p>
        {a("https://menu1.example.org/a", "Menu item A", 1600000001, 1600000002, tags="alpha,beta", shortcuturl="mka")}
        <DD>A description for Menu item A (two words: café ☕)
        {a("https://menu1.example.org/b", "Menu item B with & entity <b>", 1600000003, tags="gamma delta,شهر")}
        <HR>
        {a("https://menu1.example.org/c", "  padded   title  ", 1600000005)}
    </DL><p>
    {a("https://menu.example.org/loose", "Loose menu bookmark", 1600000010)}
    <DT><H3 ADD_DATE="1400000100">پوشهٔ فارسی</H3>
    <DL><p>
        {a("https://fa.example.org/صفحه", "عنوان فارسی ۱۲۳ بین‌المللی", 1600000020, tags="فارسی,persian")}
        {a("https://xn--mgbh0fb.example/صفحه", "IDN host", 1600000021)}
    </DL><p>
    <DT><H3 ADD_DATE="1400000200">Q&amp;A / FAQ</H3>
    <DL><p>
        {a("https://slash.example.org/1", "Inside a folder whose name has a slash", 1600000030)}
    </DL><p>
    <DT><H3 ADD_DATE="1400000300">Odd schemes</H3>
    <DL><p>
        {a("javascript:void(document.title='x')", "Bookmarklet", 1600000040)}
        {a("data:text/html,<h1>hi</h1>", "Data URL", 1600000041)}
        {a("about:config", "about:config", 1600000042)}
        {a("file:///tmp/example/notes.txt", "Local file", 1600000043)}
        {a("mailto:someone@example.org", "Mail link", 1600000044)}
        {a("place:type=6&sort=14&maxResults=10", "A smart bookmark (place: query)", 1600000045)}
        {a(long_url, "Very long URL", 1600000046)}
        {a("https://example.org/#frag", "Fragment", 1600000047)}
        {a("https://example.org/dup", "Same URL, folder one", 1600000048)}
    </DL><p>
    <DT><H3 ADD_DATE="1400000400">Dates</H3>
    <DL><p>
        {a("https://dates.example.org/zero", "ADD_DATE zero", 0)}
        {a("https://dates.example.org/old", "ADD_DATE 1996", 835000000)}
        {a("https://dates.example.org/future", "ADD_DATE 2200", 7258118400)}
        {a("https://dates.example.org/ms", "ADD_DATE in milliseconds", 1600000000000)}
    </DL><p>
    <DT><H3 ADD_DATE="1400000500">Deep tree</H3>
    <DL><p>
{chr(10).join(deep)}
    </DL><p>
    <DT><H3 ADD_DATE="1400000600" LAST_MODIFIED="1400000700" PERSONAL_TOOLBAR_FOLDER="true">Bookmarks Toolbar</H3>
    <DD>The toolbar folder description
    <DL><p>
        {a("https://toolbar.example.org/one", "Toolbar one", 1600000100, tags="toolbar-tag")}
        {a("https://example.org/dup", "Same URL, toolbar copy", 1600000101)}
        <DT><H3 ADD_DATE="1400000800">Toolbar sub</H3>
        <DL><p>
            {a("https://toolbar.example.org/sub", "Toolbar sub item", 1600000102, shortcuturl="tsub", tags="x")}
            <HR>
            <HR>
            {a("https://toolbar.example.org/sub2", "After two separators", 1600000103)}
        </DL><p>
    </DL><p>
    <DT><H3 ADD_DATE="1400000900" LAST_MODIFIED="1400001000" UNFILED_BOOKMARKS_FOLDER="true">Other Bookmarks</H3>
    <DL><p>
        {a("https://unfiled.example.org/one", "Unfiled one", 1600000200)}
        <DT><H3 ADD_DATE="1400001100">Empty folder</H3>
        <DL><p>
        </DL><p>
        <DT><H3 ADD_DATE="1400001200">Folder with trailing space </H3>
        <DL><p>
            {a("https://unfiled.example.org/two", "Unfiled two", 1600000201, tags="Mixed Case,UPPER")}
        </DL><p>
    </DL><p>
</DL><p>
"""
    return out


if __name__ == "__main__":
    outdir = Path(sys.argv[1])
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "messy.html").write_text(messy(), encoding="utf-8")
    (outdir / "rich.html").write_text(rich(), encoding="utf-8")
    print("wrote", outdir / "messy.html", outdir / "rich.html")
