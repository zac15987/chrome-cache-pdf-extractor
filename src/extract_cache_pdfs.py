"""Scan Chrome cache files, carve embedded PDFs, save with their original filenames.

For each `f_XXXXXX` external cache file that contains a PDF body, we parse the
blockfile EntryStore that points at it, recover the URL key (plus long URLs
via prefix match) and any Content-Disposition filename, and save the PDF using
that name. Falls back to a hash-prefixed name when the entry cannot be resolved.

Usage:
    python extract_cache_pdfs.py
    python extract_cache_pdfs.py --cache-dir "C:/Users/Jeff/AppData/Local/Google/Chrome/User Data"
    python extract_cache_pdfs.py --out ./extracted
"""
from __future__ import annotations

import argparse
import ctypes
import gzip
import hashlib
import os
import re
import struct
import sys
import urllib.parse
import zlib
from pathlib import Path

import fitz


# ---------- Shared-mode file read (Windows) ----------
# Chrome holds cache files with a restrictive share mode, so normal open() fails
# with PermissionError while Chrome is running. We open with every share flag
# set via CreateFileW — same trick ChromeCacheView uses.

if sys.platform == "win32":
    _GENERIC_READ = 0x80000000
    _OPEN_EXISTING = 3
    _SHARE_ALL = 0x1 | 0x2 | 0x4  # READ | WRITE | DELETE
    _INVALID_HANDLE = ctypes.c_void_p(-1).value
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateFileW.restype = ctypes.c_void_p
    _k32.CreateFileW.argtypes = [
        ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_ulong,
        ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p,
    ]
    _k32.ReadFile.restype = ctypes.c_int
    _k32.GetFileSizeEx.restype = ctypes.c_int
    _k32.CloseHandle.restype = ctypes.c_int

    def read_bytes_shared(path: Path) -> bytes:
        h = _k32.CreateFileW(str(path), _GENERIC_READ, _SHARE_ALL, None,
                             _OPEN_EXISTING, 0, None)
        if h == _INVALID_HANDLE or h is None:
            raise PermissionError(f"CreateFileW failed ({ctypes.get_last_error()}): {path}")
        try:
            size = ctypes.c_longlong()
            if not _k32.GetFileSizeEx(ctypes.c_void_p(h), ctypes.byref(size)):
                raise OSError(ctypes.get_last_error())
            total = size.value
            buf = (ctypes.c_ubyte * total)()
            read = ctypes.c_ulong(0)
            if not _k32.ReadFile(ctypes.c_void_p(h), buf, total, ctypes.byref(read), None):
                raise OSError(ctypes.get_last_error())
            return bytes(buf[: read.value])
        finally:
            _k32.CloseHandle(ctypes.c_void_p(h))
else:
    def read_bytes_shared(path: Path) -> bytes:
        return path.read_bytes()

PDF_MAGIC = b"%PDF-"
PDF_EOF = b"%%EOF"

ENTRY_SIZE = 256
KEY_INLINE_OFFSET = 96
KEY_INLINE_MAXLEN = 160
BLOCK_HEADER_SIZE = 8192
BLOCK_SIZES = {1: 36, 2: 256, 3: 1024, 4: 4096}


# ---------- Chrome blockfile parsing ----------


def ext_cacheaddr(file_num: int) -> bytes:
    return (0x80000000 | file_num).to_bytes(4, "little")


def load_blockfiles(cache_data_dir: Path) -> dict[str, bytes]:
    out = {}
    for n in ("data_0", "data_1", "data_2", "data_3"):
        p = cache_data_dir / n
        if p.exists():
            try:
                out[n] = read_bytes_shared(p)
            except (PermissionError, OSError):
                pass
    return out


def read_block_addr(bufs: dict[str, bytes], addr: int, length: int) -> bytes | None:
    if not addr & 0x80000000:
        return None
    kind = (addr >> 28) & 0x07
    if kind == 0:
        return None
    block_size = BLOCK_SIZES.get(kind)
    if not block_size:
        return None
    file_num = (addr >> 16) & 0xFF
    num_blocks = ((addr >> 24) & 0x03) + 1
    block_off = addr & 0xFFFF
    buf = bufs.get(f"data_{file_num}")
    if not buf:
        return None
    start = BLOCK_HEADER_SIZE + block_off * block_size
    return buf[start : start + min(length, block_size * num_blocks)]


def find_entry_for_fnum(bufs: dict[str, bytes], file_num: int) -> tuple[str, int] | None:
    needle = ext_cacheaddr(file_num)
    for name, buf in bufs.items():
        i = 0
        while True:
            j = buf.find(needle, i)
            if j < 0:
                break
            # data_addr[1] sits at offset 60 inside a 256-byte EntryStore
            if j >= 60:
                entry_start = j - 60
                hash_ = struct.unpack_from("<I", buf, entry_start)[0]
                key_len = struct.unpack_from("<i", buf, entry_start + 32)[0]
                if hash_ and 0 < key_len < 4096:
                    return name, entry_start
            i = j + 1
    return None


def parse_entry(bufs: dict[str, bytes], buf_name: str, entry_start: int) -> dict:
    e = bufs[buf_name][entry_start : entry_start + ENTRY_SIZE]
    key_len = struct.unpack_from("<i", e, 32)[0]
    long_key = struct.unpack_from("<I", e, 36)[0]
    data_size = struct.unpack_from("<4i", e, 40)
    data_addr = struct.unpack_from("<4I", e, 56)

    if long_key:
        key_bytes = (read_block_addr(bufs, long_key, key_len) or b"")[:key_len]
    else:
        key_bytes = e[KEY_INLINE_OFFSET : KEY_INLINE_OFFSET + min(key_len, KEY_INLINE_MAXLEN)]

    headers = b""
    if data_addr[0]:
        headers = (read_block_addr(bufs, data_addr[0], data_size[0]) or b"")[: data_size[0]]
    return {"key_bytes": key_bytes, "headers": headers}


def collect_full_pdf_urls(bufs: dict[str, bytes]) -> list[str]:
    pat = re.compile(rb'https?://[^\s\x00<>"\']{5,800}\.pdf', re.I)
    urls: set[str] = set()
    for buf in bufs.values():
        for m in pat.finditer(buf):
            urls.add(m.group(0).decode("utf-8", errors="replace"))
    return sorted(urls)


def resolve_full_url(truncated: str | None, full_urls: list[str]) -> str | None:
    if not truncated:
        return None
    hits = [u for u in full_urls if u.startswith(truncated)]
    if not hits:
        return truncated
    return min(hits, key=len) if len(hits) > 1 else hits[0]


def url_from_key(key_bytes: bytes) -> str | None:
    text = key_bytes.split(b"\x00", 1)[0].decode("utf-8", errors="replace")
    urls = re.findall(r'https?://[^\s"\'<>]+', text)
    return urls[-1] if urls else None


def filename_from_disposition(headers: bytes) -> str | None:
    m = re.search(rb"(?i)content-disposition:[^\r\n\x00]+", headers)
    if not m:
        return None
    disp = m.group(0).decode("utf-8", errors="replace")
    m = re.search(r"filename\*=(?:UTF-8'')?([^;\r\n]+)", disp, re.I)
    if m:
        return urllib.parse.unquote(m.group(1).strip().strip('"'))
    m = re.search(r'filename="?([^";\r\n]+)"?', disp, re.I)
    if m:
        return urllib.parse.unquote(m.group(1))
    return None


def filename_from_url(url: str) -> str | None:
    path = url.split("?", 1)[0]
    name = urllib.parse.unquote(path.rsplit("/", 1)[-1])
    return name if name.lower().endswith(".pdf") else None


def safe_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    return name.strip(" .") or "unknown.pdf"


def resolve_original_name(
    bufs: dict[str, bytes], full_urls: list[str], source: Path
) -> str | None:
    """Given a cache source file path (e.g. .../f_002456), return the original filename."""
    m = re.fullmatch(r"f_([0-9a-f]+)", source.name)
    if not m:
        return None
    fnum = int(m.group(1), 16)
    loc = find_entry_for_fnum(bufs, fnum)
    if not loc:
        return None
    info = parse_entry(bufs, *loc)
    url = resolve_full_url(url_from_key(info["key_bytes"]), full_urls)
    return (
        filename_from_disposition(info["headers"])
        or (filename_from_url(url) if url else None)
    )


# ---------- PDF carving ----------


def try_decompress(blob: bytes) -> list[bytes]:
    out = [blob]
    i = 0
    while True:
        j = blob.find(b"\x1f\x8b\x08", i)
        if j < 0:
            break
        try:
            out.append(gzip.decompress(blob[j:]))
        except Exception:
            try:
                d = zlib.decompressobj(31).decompress(blob[j:])
                if d:
                    out.append(d)
            except Exception:
                pass
        i = j + 3
    try:
        out.append(zlib.decompress(blob))
    except Exception:
        pass
    return out


def carve_pdfs(blob: bytes) -> list[bytes]:
    results, start = [], 0
    while True:
        i = blob.find(PDF_MAGIC, start)
        if i < 0:
            break
        last_eof = blob.rfind(PDF_EOF, i)
        if last_eof < 0:
            start = i + len(PDF_MAGIC)
            continue
        end = last_eof + len(PDF_EOF)
        while end < len(blob) and blob[end : end + 1] in (b"\r", b"\n"):
            end += 1
        results.append(blob[i:end])
        start = end
    return results


# ---------- Orchestration ----------


def default_chrome_user_data() -> Path:
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData/Local")
    return Path(local) / "Google" / "Chrome" / "User Data"


def iter_cache_files(root: Path):
    if not root.exists():
        return
    for dirpath, _, filenames in os.walk(root):
        if Path(dirpath).name in {"Cache_Data", "Cache"}:
            for fn in filenames:
                yield Path(dirpath) / fn


def unique_path(out_dir: Path, name: str, used: set[str]) -> Path:
    base, ext = os.path.splitext(name)
    candidate, k = name, 1
    while candidate.lower() in used:
        k += 1
        candidate = f"{base} ({k}){ext}"
    used.add(candidate.lower())
    return out_dir / candidate


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--cache-dir", type=Path, default=default_chrome_user_data(),
        help="Chrome User Data root (scans all profiles)",
    )
    p.add_argument("--out", type=Path, default=Path("extracted_pdfs"))
    p.add_argument("--min-size", type=int, default=1024)
    args = p.parse_args()

    if not args.cache_dir.exists():
        print(f"error: {args.cache_dir} does not exist", file=sys.stderr)
        return 1

    args.out.mkdir(parents=True, exist_ok=True)
    print(f"scanning: {args.cache_dir}", file=sys.stderr)

    # One bufs map per profile's Cache_Data so we can resolve filenames per-profile
    profile_bufs: dict[Path, tuple[dict[str, bytes], list[str]]] = {}

    def bufs_for(source: Path) -> tuple[dict[str, bytes], list[str]]:
        key = source.parent
        if key not in profile_bufs:
            bufs = load_blockfiles(key)
            profile_bufs[key] = (bufs, collect_full_pdf_urls(bufs))
        return profile_bufs[key]

    seen_digest: set[str] = set()
    used_names: set[str] = set()
    scanned = saved = 0

    for path in iter_cache_files(args.cache_dir):
        scanned += 1
        try:
            blob = read_bytes_shared(path)
        except (PermissionError, OSError) as e:
            print(f"  skip {path.name}: {e}", file=sys.stderr)
            continue
        if PDF_MAGIC not in blob and b"\x1f\x8b\x08" not in blob:
            continue

        for variant in try_decompress(blob):
            for pdf in carve_pdfs(variant):
                if len(pdf) < args.min_size:
                    continue
                digest = hashlib.sha256(pdf).hexdigest()
                if digest in seen_digest:
                    continue
                try:
                    with fitz.open(stream=pdf, filetype="pdf") as doc:
                        if doc.page_count == 0:
                            continue
                except Exception:
                    continue
                seen_digest.add(digest)

                bufs, full_urls = bufs_for(path)
                original = resolve_original_name(bufs, full_urls, path)
                if original:
                    name = safe_filename(original)
                else:
                    name = f"{digest[:12]}_{path.name}.pdf"
                dst = unique_path(args.out, name, used_names)
                dst.write_bytes(pdf)
                saved += 1
                print(f"  + {dst.name}  ({len(pdf):,} bytes  <- {path.name})", file=sys.stderr)

    print(
        f"\nscanned {scanned} cache files, saved {saved} unique PDFs to {args.out}/",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
