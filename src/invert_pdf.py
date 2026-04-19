"""Invert PDFs to dark mode (black bg / white text) via raster negate.

For each input PDF, writes two variants into --out-dir:
    <stem>.dark.pdf       plain negate (black/white clean, colored hues get flipped)
    <stem>.dark_hue.pdf   negate + hue-rotate 180° (colored images keep natural hue)

Usage:
    python invert_pdf.py input.pdf [more.pdf ...] [--out-dir dark_pdfs] [--dpi 200]
"""
from __future__ import annotations

import argparse
import sys
from io import BytesIO
from pathlib import Path

import fitz
from PIL import Image, ImageOps


def _hue_rotate_180(img: Image.Image) -> Image.Image:
    hsv = img.convert("HSV")
    h, s, v = hsv.split()
    h = h.point(lambda p: (p + 128) % 256)
    return Image.merge("HSV", (h, s, v)).convert("RGB")


def invert_variants(img: Image.Image) -> tuple[Image.Image, Image.Image]:
    """Return (plain-inverted, inverted+hue-rotated) for an RGB page image."""
    inv = ImageOps.invert(img.convert("RGB"))
    return inv, _hue_rotate_180(inv)


def _to_png(img: Image.Image) -> bytes:
    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def invert_pdf(src: Path, out_dir: Path, dpi: int) -> tuple[Path, Path]:
    zoom = dpi / 72
    matrix = fitz.Matrix(zoom, zoom)
    dst_plain = out_dir / f"{src.stem}.dark.pdf"
    dst_hue   = out_dir / f"{src.stem}.dark_hue.pdf"
    with fitz.open(src) as doc, fitz.open() as plain, fitz.open() as hue:
        for i, page in enumerate(doc, 1):
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            inv_plain, inv_hue = invert_variants(img)
            rect = page.rect
            for out_doc, inv_img in ((plain, inv_plain), (hue, inv_hue)):
                new_page = out_doc.new_page(width=rect.width, height=rect.height)
                new_page.insert_image(new_page.rect, stream=_to_png(inv_img))
            print(f"  page {i}/{len(doc)}", file=sys.stderr)
        plain.save(dst_plain, deflate=True, garbage=3)
        hue.save(dst_hue, deflate=True, garbage=3)
    return dst_plain, dst_hue


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("inputs", type=Path, nargs="+", help="PDF file(s) to invert")
    p.add_argument("--out-dir", type=Path, default=Path("dark_pdfs"))
    p.add_argument("--dpi", type=int, default=200, help="render DPI (default 200)")
    args = p.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)

    for src in args.inputs:
        if not src.exists():
            print(f"skip (missing): {src}", file=sys.stderr)
            continue
        print(f"inverting {src} -> {args.out_dir}/ @ {args.dpi} dpi", file=sys.stderr)
        a, b = invert_pdf(src, args.out_dir, args.dpi)
        print(f"  done: {a.name}, {b.name}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
