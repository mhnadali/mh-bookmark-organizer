"""Render the long description the way PyPI does (readme_renderer) and list every link / image that would not work there."""
import re, sys
from pathlib import Path
import readme_renderer.markdown as md
text = Path(sys.argv[1]).read_text(encoding="utf-8")
html = md.render(text)
if html is None:
    print("README does NOT render (PyPI would show it as plain text)"); sys.exit(2)
bad = []
for kind, url in re.findall(r'\b(href|src)="([^"]*)"', html):
    if url.startswith("#") or re.match(r"^(https?:|mailto:)", url):
        continue
    bad.append((kind, url))
print(f"rendered ok, {len(html)} bytes of HTML; relative/unusable links: {len(bad)}")
for k, u in bad:
    print(f"  {k}={u}")
sys.exit(1 if bad else 0)
