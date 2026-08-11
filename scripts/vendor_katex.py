"""Download and vendor KaTeX CSS, JavaScript, and WOFF2 fonts for offline rendering."""

from __future__ import annotations

import io
import sys
import tarfile
import urllib.request
from pathlib import Path

VERSION = "0.16.11"
URL = f"https://registry.npmjs.org/katex/-/katex-{VERSION}.tgz"
DEST = Path(__file__).resolve().parents[1] / "src" / "dersnotu" / "assets" / "katex"

WANTED = {
    "package/dist/katex.min.css": "katex.min.css",
    "package/dist/katex.min.js": "katex.min.js",
    "package/dist/contrib/auto-render.min.js": "auto-render.min.js",
}


def main() -> int:
    DEST.mkdir(parents=True, exist_ok=True)
    print(f"downloading: {URL}")
    with urllib.request.urlopen(URL, timeout=120) as resp:
        blob = resp.read()
    print(f"received {len(blob) / 1024:.0f} KB")

    fonts_dir = DEST / "fonts"
    fonts_dir.mkdir(exist_ok=True)
    n_fonts = 0

    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        for member in tar.getmembers():
            if member.name in WANTED:
                src = tar.extractfile(member)
                if src:
                    (DEST / WANTED[member.name]).write_bytes(src.read())
                    print("  ✔", WANTED[member.name])
            elif member.name.startswith("package/dist/fonts/") and member.name.endswith(".woff2"):
                src = tar.extractfile(member)
                if src:
                    (fonts_dir / Path(member.name).name).write_bytes(src.read())
                    n_fonts += 1

    print(f"  ✔ {n_fonts} WOFF2 fonts")

    missing = [v for v in WANTED.values() if not (DEST / v).exists()]
    if missing:
        print("MISSING:", missing, file=sys.stderr)
        return 1
    print(f"complete → {DEST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
