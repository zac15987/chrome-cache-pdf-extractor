# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```
pip install -r requirements.txt           # pymupdf, Pillow
python src/extract_cache_pdfs.py          # scan %LOCALAPPDATA%/Google/Chrome/User Data → ./extracted_pdfs/
python src/extract_cache_pdfs.py --cache-dir <path> --out <dir> --min-size 4096
python src/invert_pdf.py <pdf>...         # write <stem>.dark.pdf + <stem>.dark_hue.pdf to ./dark_pdfs/
```

No test suite, linter, or formatter is configured. Python 3.9+ is required.

## Platform

- Windows-first. `extract_cache_pdfs.py` opens cache files via `CreateFileW` with `FILE_SHARE_READ|WRITE|DELETE` (see `read_bytes_shared`, src/extract_cache_pdfs.py:50) so it works while Chrome is running. On macOS/Linux it falls through to `path.read_bytes()` and the user must close Chrome first.
- Never replace the ctypes shared-mode read with plain `open()` — that breaks the "works while Chrome is running" property.

## Architecture

The non-obvious part is how `extract_cache_pdfs.py` recovers original filenames for carved PDFs. Two independent passes that must stay wired together:

1. **Carve**: walk every `Cache_Data`/`Cache` directory under the Chrome profile root, read each file (including the inline `data_0..data_3` block files), attempt gzip/zlib decompression of any `\x1f\x8b\x08` stream, then search for `%PDF-`…`%%EOF` and validate each hit by opening it with PyMuPDF. Dedupe by SHA-256.

2. **Filename resolution** (only works for PDFs stored as standalone `f_XXXXXX` files):
   - `f_002456` → build CacheAddr `0x80000000 | 0x2456` and scan the block files for that 4-byte pattern (`find_entry_for_fnum`).
   - A hit at offset 60 of a 256-byte `EntryStore` → parse `key_len`, `long_key`, `data_size[4]`, `data_addr[4]` (`parse_entry`). Stream 0 is response headers; the key is either inline (cap 160 bytes) or referenced via `long_key`/`read_block_addr`.
   - The inline-key 160-byte cap truncates long URLs. `collect_full_pdf_urls` regex-sweeps every block file for complete `https?://…\.pdf` URLs so a truncated key can be prefix-matched back to its full URL (`resolve_full_url`).
   - Prefer `Content-Disposition: filename*=UTF-8''…` (headers) over the URL path. URL-decode both; sanitize via `safe_filename`.
   - Small PDFs stored inline in `data_*` (not in `f_*` files) cannot be resolved this way — they fall back to `<sha256-prefix>_<source>.pdf`.

`bufs_for(source)` memoizes one `(blockfiles, full_urls)` tuple per profile's `Cache_Data` directory, so a multi-profile scan doesn't reparse the same block files.

Reference: Chromium's `disk_cache/blockfile/disk_format.h` — `BLOCK_SIZES`, `ENTRY_SIZE`, `KEY_INLINE_*`, and the `data_addr[1]` offset of 60 are all derived from that struct layout.

`invert_pdf.py` is independent: rasterize each page via PyMuPDF → `PIL.ImageOps.invert`, with an optional HSV hue-rotate-180° variant that preserves the natural hue of colored figures after inversion. Both variants are always written.

## Known limitations (do not "fix" without discussion)

- Only the classic blockfile cache is parsed. The newer Simple Cache format is out of scope.
- Inline-stored small PDFs get hash-prefixed names by design — there is no EntryStore back-reference available for them.
