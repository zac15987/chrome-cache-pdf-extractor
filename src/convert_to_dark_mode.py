"""Convert PDFs to a dark-mode palette modelled on VS Code's Dark+ theme.

Pure black on pure white becomes #D4D4D4 on #1E1E1E — the same foreground/
background the VS Code editor uses by default. Unlike a raster negate, this
leaves the underlying PDF content untouched: text stays selectable, vector
graphics stay vector, embedded images keep their native resolution.

The palette is applied at display time by four full-page rectangles layered
into each page's content stream. The first is opaque white drawn BEFORE the
existing content — it gives the page's transparency group an opaque white
backdrop, without which `Difference` blend mode silently degrades to Normal
(the overlay rects replace whatever's under them and everything collapses
to a single grey). The remaining three rects are appended AFTER the content
with standard PDF blend modes; composed they reproduce exactly the LUT
`new = BG + (255 - old) * (FG - BG) / 255`:

    0. Normal      fill #FFFFFF   prepended opaque backdrop (see above)
    1. Difference  fill #FFFFFF   inverts:   P -> 255 - P
    2. Multiply    fill  #CECECE  scales:    P -> P * M / 255      (M = 206)
    3. Screen      fill  #1E1E1E  lifts:     P -> 255 - (255 - P)(255 - S)/255
                                                                    (S = 30)

Usage:
    python convert_to_dark_mode.py input.pdf [more.pdf ...] [--out-dir dark_pdfs]
"""
from __future__ import annotations

import argparse
import sys
from glob import glob
from pathlib import Path

import fitz


# VS Code Dark+ editor palette. Pure gray on both ends so one overlay sequence
# handles R, G and B uniformly.
VSCODE_BG = 0x1E  # editor.background  (#1E1E1E)
VSCODE_FG = 0xD4  # editor.foreground  (#D4D4D4)

# Closed-form derivation of the Multiply/Screen fill colours that make the
# three-layer composition equal to the LUT in the module docstring.
_S = VSCODE_BG
_M = round(255 * (1 - (255 - VSCODE_FG) / (255 - VSCODE_BG)))


def _rgb_op(gray: int) -> str:
    c = gray / 255
    return f"{c:.6f} {c:.6f} {c:.6f}"


def _overlay_ops(width: float, height: float) -> bytes:
    """PDF content stream bytes for the three-layer dark-mode overlay."""
    return (
        f"q /BMDiff gs {_rgb_op(255)} rg 0 0 {width} {height} re f Q\n"
        f"q /BMMul gs {_rgb_op(_M)} rg 0 0 {width} {height} re f Q\n"
        f"q /BMScr gs {_rgb_op(_S)} rg 0 0 {width} {height} re f Q\n"
    ).encode("latin-1")


_EXTGSTATES = (
    ("BMDiff", "Difference"),
    ("BMMul",  "Multiply"),
    ("BMScr",  "Screen"),
)


def _make_stream(doc: fitz.Document, body: bytes) -> int:
    xref = doc.get_new_xref()
    doc.update_object(xref, "<<>>")
    doc.update_stream(xref, body, new=True)
    return xref


def _append_dark_overlay(doc: fitz.Document, page: fitz.Page) -> None:
    """Prepend a white backdrop and append the three blend-mode rects."""
    if not page.is_wrapped:
        page.wrap_contents()

    for name, bm in _EXTGSTATES:
        doc.xref_set_key(
            page.xref,
            f"Resources/ExtGState/{name}",
            f"<< /Type /ExtGState /BM /{bm} >>",
        )

    w, h = page.rect.width, page.rect.height
    bg = _make_stream(
        doc,
        f"q {_rgb_op(255)} rg 0 0 {w} {h} re f Q\n".encode("latin-1"),
    )
    overlay = _make_stream(doc, _overlay_ops(w, h))

    refs = [bg] + page.get_contents() + [overlay]
    doc.xref_set_key(
        page.xref, "Contents",
        "[ " + " ".join(f"{x} 0 R" for x in refs) + " ]",
    )


def convert_pdf(src: Path, out_dir: Path) -> Path:
    dst = out_dir / f"{src.stem}.dark.pdf"
    with fitz.open(src) as doc:
        for i, page in enumerate(doc, 1):
            _append_dark_overlay(doc, page)
            print(f"  page {i}/{doc.page_count}", file=sys.stderr)
        doc.save(dst, deflate=True, garbage=3)
    return dst


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("inputs", type=Path, nargs="+", help="PDF file(s) to convert")
    p.add_argument("--out-dir", type=Path, default=Path("dark_pdfs"))
    args = p.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)

    inputs: list[Path] = []
    for raw in args.inputs:
        s = str(raw)
        if any(ch in s for ch in "*?["):
            matched = sorted(glob(s))
            if not matched:
                print(f"skip (no match): {s}", file=sys.stderr)
            inputs.extend(Path(m) for m in matched)
        else:
            inputs.append(raw)

    for src in inputs:
        if not src.exists():
            print(f"skip (missing): {src}", file=sys.stderr)
            continue
        print(f"converting {src} -> {args.out_dir}/", file=sys.stderr)
        dst = convert_pdf(src, args.out_dir)
        print(f"  done: {dst.name}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
