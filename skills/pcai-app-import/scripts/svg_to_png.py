#!/usr/bin/env python3
"""Convert an SVG icon to a small square PNG for use as a PCAI app logo.

Tries, in order: cairosvg (Python), rsvg-convert, ImageMagick. If none is
available, copies the skill's bundled fallback icon
(assets/fallback-icon.png), so the script always produces a PNG on macOS,
Linux, or Windows with nothing installed. qlmanage is deliberately not
used: QuickLook mis-transforms SVGs and flattens transparency.

Usage: svg_to_png.py input.svg output.png [size]
"""
import shutil
import subprocess
import sys
from pathlib import Path

FALLBACK_ICON = Path(__file__).resolve().parent.parent / "assets" / "fallback-icon.png"


def convert_with_resvg(src, dst, size):
    import resvg_py

    data = resvg_py.svg_to_bytes(svg_string=Path(src).read_text(), width=size, height=size)
    Path(dst).write_bytes(bytes(data))
    return "resvg-py"


def convert_with_cairosvg(src, dst, size):
    import cairosvg

    cairosvg.svg2png(url=src, write_to=dst, output_width=size, output_height=size)
    return "cairosvg"


def convert_with_rsvg(src, dst, size):
    if not shutil.which("rsvg-convert"):
        return None
    subprocess.run(
        ["rsvg-convert", "-w", str(size), "-h", str(size), "-o", dst, src],
        check=True,
        capture_output=True,
    )
    return "rsvg-convert"


def convert_with_imagemagick(src, dst, size):
    for magick in ("magick", "convert"):
        if shutil.which(magick):
            subprocess.run([magick, src, "-resize", f"{size}x{size}", dst], check=True, capture_output=True)
            return f"ImageMagick ({magick})"
    return None


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    src, dst = sys.argv[1], sys.argv[2]
    size = int(sys.argv[3]) if len(sys.argv) > 3 else 256

    for attempt in (
        convert_with_resvg,
        convert_with_cairosvg,
        convert_with_rsvg,
        convert_with_imagemagick,
    ):
        try:
            tool = attempt(src, dst, size)
        except Exception:
            continue
        if tool:
            print(f"converted {src} -> {dst} at {size}x{size} using {tool}")
            return 0

    shutil.copyfile(FALLBACK_ICON, dst)
    print(
        f"no SVG renderer found; used the bundled fallback icon -> {dst} at {size}x{size}. "
        "For an icon matching the app, install one of: pip install resvg-py (self-contained, recommended); "
        "pip install cairosvg (needs the cairo system library on macOS); "
        "librsvg (apt install librsvg2-bin / brew install librsvg); "
        "ImageMagick (apt/winget/choco install imagemagick)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
