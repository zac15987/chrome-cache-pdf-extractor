# chrome-cache-pdf-extractor

Carve PDFs out of Chrome's on-disk cache and save them with their **original
filenames** — including non-ASCII (Chinese, Japanese, …) filenames that tools
like ChromeCacheView display as `\uXXXX` escapes.

Bundled with a companion tool, `invert_pdf.py`, that converts any PDF to a
dark-mode version (black background, white text) — handy when the original
asset is a white-background scan or a slide deck and you want to read it
without the glare.

## Features

- Scans every `Cache_Data` directory under `Chrome/User Data/**`.
- Carves PDFs by `%PDF-` / `%%EOF` magic, including inside gzip-compressed
  response bodies.
- Deduplicates by SHA-256.
- Validates each carve by opening it with PyMuPDF (drops false positives such
  as JavaScript sources that happen to contain `%PDF-` as a string literal).
- Parses the Chrome blockfile cache (`data_0`..`data_3`) to recover each
  entry's request URL and `Content-Disposition`, then saves the PDF under its
  real filename. UTF-8 safe — Chinese filenames round-trip cleanly.
- Reads cache files in Windows shared mode (same trick as ChromeCacheView),
  so it works **while Chrome is running**.

## Requirements

- Python 3.9+
- Windows (Chrome's exclusive-lock workaround is Windows-specific; on macOS /
  Linux the scripts will still run, but cache files must be readable without
  contention, e.g. with Chrome closed).

Install dependencies:

```
pip install -r requirements.txt
```

## Usage

### Extract PDFs from the cache

```
python src/extract_cache_pdfs.py
```

Defaults:
- `--cache-dir`  `%LOCALAPPDATA%/Google/Chrome/User Data` (scans every profile)
- `--out`        `./extracted_pdfs`
- `--min-size`   `1024` (skip fragments smaller than 1 KB)

Example:

```
python src/extract_cache_pdfs.py --out ./pdfs --min-size 4096
```

When the original name cannot be recovered (e.g. for small PDFs that live
inline in the block files rather than as separate `f_*` resource files), the
tool falls back to `<sha256-prefix>_<source>.pdf`.

### Dark-mode the PDFs (optional companion)

```
python src/invert_pdf.py extracted_pdfs/*.pdf
```

For every input PDF, writes two variants into `./dark_pdfs/`:

- `<stem>.dark.pdf`     — plain raster negate. Cleanest black/white for
                          text-heavy content; colored hues get flipped.
- `<stem>.dark_hue.pdf` — negate + 180° hue rotation. Coloured figures and
                          circuit diagrams keep their natural hue.

Flags:

- `--out-dir` (default `dark_pdfs`)
- `--dpi`     (default `200`)

## How it works

Chrome's classic disk cache (the one with `data_0..data_3` + `f_XXXXXX`
files) stores each HTTP response as an `EntryStore` record — a fixed 256-byte
struct — inside one of the block files. The record holds:

- the full request URL (the "key"), inline or via a `long_key` pointer,
- `data_size[4]` and `data_addr[4]` arrays pointing at up to four streams
  (stream 0 = response headers, stream 1 = response body).

When the body is large, stream 1 is stored as a standalone `f_XXXXXX` file
whose number is encoded into the `data_addr[1]` CacheAddr as
`0x80000000 | file_number`.

Given a carved PDF from `f_002456`, we:

1. Build the CacheAddr (`0x80002456`) and search every block file for that
   4-byte pattern.
2. The hit sits at offset 60 of the owning `EntryStore`; we back up, parse
   the struct, and read the inline key bytes.
3. Inline keys are capped at 160 bytes, so long URLs get truncated. We fix
   this by collecting every complete `.pdf` URL found anywhere in the cache
   and matching the truncated prefix back to the full URL.
4. The request URL's path component — URL-decoded — is the original
   filename. If `Content-Disposition: filename*=UTF-8''…` is present in the
   response headers (stream 0), we prefer that.

Chrome holds the cache files with a restrictive share mode, so plain
`open()` fails with `PermissionError` while the browser is running. We call
`CreateFileW` directly via `ctypes` with
`FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE` — the same
approach ChromeCacheView uses — to read them regardless.

## Limitations

- Only the classic blockfile cache is parsed. The newer Simple Cache format
  (used by Service Worker / HTTP/3 sometimes) is not yet supported.
- Small PDFs stored inline in `data_*` block files rather than in separate
  `f_*` files won't get their filename resolved; they're saved with a
  content-hash prefix.
- Shared-mode reads are Windows-only. On macOS / Linux you may need to close
  Chrome before running.

## License

MIT — see [LICENSE](LICENSE).
