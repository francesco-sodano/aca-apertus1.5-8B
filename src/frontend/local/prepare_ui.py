"""Use Chainlit's supported custom build without remote CDN/font dependencies."""

import re
import shutil
from pathlib import Path

import chainlit

source = Path(chainlit.__file__).parent / "frontend" / "dist"
target = Path("/app/ui")
shutil.copytree(source, target, ignore=shutil.ignore_patterns("*.map"))
index = target / "index.html"
html, removed = re.subn(r'<link\b[^>]*href="https://[^"]+"[^>]*>\s*', "", index.read_text())
if removed < 3:
    raise RuntimeError("The pinned Chainlit build's external asset template changed.")
index.write_text(html)
