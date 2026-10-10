#!/usr/bin/env python3
# v6_simple.py — Video Downloader V6 (single file, better video preview)
#
# Run:  python v6_simple.py
#
# Dependencies:
#   required : requests, pycryptodome
#   optional : Pillow      (image preview)
#              python-vlc  (video preview) — requires VLC desktop app
#
# Preview never writes to ~/Downloads. Video preview streams over loopback
# HTTP so VLC handles seeking with its own client; nothing is buffered whole.
#
# MEGA transfer quota
#   Free/anonymous MEGA quota is tracked per IP over a rolling window. When
#   your IP is over, MEGA answers download requests with HTTP 200 and a tiny
#   JSON body like "[-17]". That is NOT an HTTP error, so without a check the
#   bytes flow into the AES-CTR decryptor and the player gets garbage that
#   looks like a video. stream_range() now peeks the first chunk and raises a
#   clear DownloadError instead.
#
#   MEGA_PROXY routes all download traffic through a Cloudflare Worker so
#   MEGA sees a Cloudflare IP instead of yours. The per-IP quota counter on
#   your home IP never increments.
from __future__ import annotations

import base64
import http.server
import io
import itertools
import json
import mimetypes
import os
import random as _random
import re
import shutil
import socketserver
import sys
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import quote, unquote

import requests
from Crypto.Cipher import AES
from Crypto.Util import Counter

# ===========================================================================
# MEGA PROXY CONFIG
# ---------------------------------------------------------------------------
# Set MEGA_PROXY to your deployed Cloudflare Worker URL to route MEGA
# downloads through Cloudflare (bypasses per-IP transfer quota).
# Leave it empty ("") to use direct MEGA URLs — original behaviour.
#
#   MEGA_PROXY = "https://mega.yourname.workers.dev"
#
# Or use a pool for heavier rotation — one URL is picked at random per stream:
#
#   MEGA_PROXY_POOL = ("https://m1.you.workers.dev,"
#                      "https://m2.you.workers.dev,"
#                      "https://m3.you.workers.dev")
#
# If MEGA_PROXY_POOL is non-empty, MEGA_PROXY is ignored.
# ===========================================================================

MEGA_PROXY = "https://mega.bpmarketofficial.workers.dev"
MEGA_PROXY_POOL = ""

# Reference only — this JS goes on Cloudflare, NOT in this file.
# Copy-paste into the Worker editor at dash.cloudflare.com.
MEGA_WORKER_JS = r"""
const ALLOWED_HOSTS = ["userstorage.mega.co.nz","gfs.mega.co.nz","mega.co.nz"];
function isAllowed(u){try{const h=new URL(u).hostname.toLowerCase();
  return ALLOWED_HOSTS.some(d=>h===d||h.endsWith("."+d))}catch{return false}}
async function handle(request){
  const u=new URL(request.url);
  if(u.pathname==="/health")return new Response("mega-proxy ok",{status:200});
  const target=u.searchParams.get("url");
  if(!target)return new Response("missing ?url=",{status:400});
  if(!isAllowed(target))return new Response("host not allowed",{status:403});
  const fwd=new Headers();
  for(const [k,v] of request.headers){const lk=k.toLowerCase();
    if(lk==="host"||lk==="cf-connecting-ip"||lk==="x-forwarded-for")continue;fwd.set(k,v)}
  if(!fwd.has("user-agent"))fwd.set("user-agent","Mozilla/5.0");
  let upstream;
  try{upstream=await fetch(target,{method:request.method,headers:fwd,
    redirect:"follow",cf:{cacheTtl:0}})}
  catch(e){return new Response("upstream fetch failed: "+e.message,{status:502})}
  const out=new Headers();
  for(const [k,v] of upstream.headers){const lk=k.toLowerCase();
    if(lk==="content-encoding"||lk==="transfer-encoding")continue;out.set(k,v)}
  out.set("Access-Control-Allow-Origin","*");
  out.set("Access-Control-Allow-Headers","Range");
  out.set("Access-Control-Expose-Headers","Content-Range, Content-Length, Accept-Ranges");
  return new Response(upstream.body,{status:upstream.status,
    statusText:upstream.statusText,headers:out});
}
export default{async fetch(request){
  if(request.method==="OPTIONS")return new Response(null,{status:204,headers:{
    "Access-Control-Allow-Origin":"*","Access-Control-Allow-Methods":"GET, HEAD, OPTIONS",
    "Access-Control-Allow-Headers":"Range"}});
  return handle(request);
}};
"""

# ---------------------------------------------------------------------------
# ANSI
# ---------------------------------------------------------------------------

_A = {
    "reset": "\033[0m", "bold": "\033[1m", "dim": "\033[2m",
    "red": "\033[91m", "green": "\033[92m", "yellow": "\033[93m",
    "blue": "\033[94m", "cyan": "\033[96m",
}


def _ansi_ok() -> bool:
    if os.name == "nt":
        try:
            import ctypes
            k = ctypes.windll.kernel32
            h = k.GetStdHandle(-11)
            m = ctypes.c_uint32()
            if k.GetConsoleMode(h, ctypes.byref(m)):
                k.SetConsoleMode(h, m.value | 0x0004)
                return True
        except Exception:
            pass
        return False
    return sys.stdout.isatty()


_ON = _ansi_ok()


def c(s: str, *styles: str) -> str:
    if not _ON or not styles:
        return s
    return "".join(_A.get(x, "") for x in styles) + s + _A["reset"]


def clear() -> None:
    os.system("cls" if os.name == "nt" else "clear")


def tw() -> int:
    return shutil.get_terminal_size((80, 24)).columns


# ---------------------------------------------------------------------------
# Terminal art
# ---------------------------------------------------------------------------

LOGO = r"""
 ██████╗  ██████╗ ██╗    ██╗███╗   ██╗██╗      ██████╗  █████╗ ██████╗
 ██╔══██╗██╔═══██╗██║    ██║████╗  ██║██║     ██╔═══██╗██╔══██╗██╔══██╗
 ██║  ██║██║   ██║██║ █╗ ██║██╔██╗ ██║██║     ██║   ██║███████║██║  ██║
 ██║  ██║██║   ██║██║███╗██║██║╚██╗██║██║     ██║   ██║██╔══██║██║  ██║
 ██████╔╝╚██████╔╝╚███╔███╔╝██║ ╚████║███████╗╚██████╔╝██║  ██║██████╔╝
 ╚═════╝  ╚═════╝  ╚══╝╚══╝ ╚═╝  ╚═══╝╚══════╝ ╚═════╝ ╚═╝  ╚═╝╚═════╝
"""

APP = "VIDEO DOWNLOADER"
VER = "6.0"


def banner(subtitle: str = "terminal-first  ·  simple previews") -> None:
    w = tw()
    if _ON and w >= 72:
        print(c(LOGO, "cyan", "bold"))
        print(c(f"  {VER}   {subtitle}", "dim"))
        print(c("  " + "─" * (w - 4), "blue"))
    else:
        print(f"{APP} v{VER}  ·  {subtitle}")
        print("-" * min(w, 72))


def section(t: str) -> None:
    print()
    print(c(f"  ▎ {t}", "cyan", "bold"))


def ok(m: str) -> None:
    print(c(f"  ✓ {m}", "green"))


def warn(m: str) -> None:
    print(c(f"  ! {m}", "yellow"))


def err(m: str) -> None:
    print(c(f"  ✗ {m}", "red"))


def info(m: str) -> None:
    print(c(f"    {m}", "dim"))


def ask(m: str) -> str:
    return input(c(f"  ▸ {m}", "cyan", "bold")).strip()


# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

class DownloadError(Exception):
    pass


TEXT_BYTES = 128 * 1024

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": f"VideoDownloader/{VER}"})

VIDEO_EXT = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".wmv",
             ".flv", ".mpeg", ".mpg", ".ts"}
AUDIO_EXT = {".mp3", ".wav", ".flac", ".ogg", ".aac", ".m4a", ".opus"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
TEXT_EXT = {".txt", ".log", ".json", ".csv", ".xml", ".yaml", ".yml",
            ".md", ".py", ".js", ".ts", ".html", ".css", ".ini", ".cfg",
            ".toml", ".c", ".cpp", ".h", ".hpp", ".rs", ".go", ".java",
            ".sh", ".bat", ".ps1"}
PDF_EXT = {".pdf"}
ARCHIVE_EXT = {".zip", ".tar", ".gz", ".tgz", ".bz2", ".7z", ".rar"}


# ---------------------------------------------------------------------------
# FileItem
# ---------------------------------------------------------------------------

@dataclass
class FileItem:
    name: str
    size: int = 0
    source: str = ""
    source_type: str = ""
    source_id: str = ""
    source_key: list = field(default_factory=list)
    zip_member: str = ""
    direct_url: str = ""
    duration: str = "Unknown"
    resolution: str = "Unknown"
    is_folder: bool = False
    mime: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def display_size(self) -> str:
        return format_size(self.size)

    @property
    def ext(self) -> str:
        return Path(self.name).suffix.lower()

    @property
    def category(self) -> str:
        e = self.ext
        if e in VIDEO_EXT: return "video"
        if e in AUDIO_EXT: return "audio"
        if e in IMAGE_EXT: return "image"
        if e in PDF_EXT: return "pdf"
        if e in TEXT_EXT: return "text"
        if e in ARCHIVE_EXT: return "archive"
        return "other"

    @property
    def identity(self) -> str:
        return f"{self.source_type}|{self.source_id or self.zip_member or self.name}|{self.name}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def safe_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip().rstrip(". ")
    return name or "file"


def unique_path(p: Path) -> Path:
    if not p.exists():
        return p
    stem, suffix = p.stem, p.suffix
    i = 2
    while True:
        cand = p.with_name(f"{stem} ({i}){suffix}")
        if not cand.exists():
            return cand
        i += 1


def format_size(n: int) -> str:
    if n <= 0:
        return "Unknown"
    units = ["B", "KB", "MB", "GB", "TB"]
    v = float(n)
    for u in units:
        if v < 1024 or u == units[-1]:
            return f"{v:.1f} {u}" if u != "B" else f"{int(v)} B"
        v /= 1024
    return f"{n} B"


def progress(done: int, total: int, started: float) -> str:
    pct = (done / total * 100) if total else 0
    elapsed = max(time.time() - started, 0.001)
    speed = done / elapsed
    if total:
        eta = (total - done) / speed if speed > 0 else 0
        return (f"{pct:6.2f}%  {format_size(done)}/{format_size(total)}  "
                f"{format_size(int(speed))}/s  ETA {int(eta)}s")
    return f"{format_size(done)}  {format_size(int(speed))}/s"


# ---------------------------------------------------------------------------
# Dropbox
# ---------------------------------------------------------------------------

def dropbox_url(u: str) -> str:
    u = u.strip()
    if "?" in u:
        base, q = u.split("?", 1)
        parts = [p for p in q.split("&") if not p.startswith("dl=")]
        parts.append("dl=1")
        return f"{base}?{'&'.join(parts)}"
    return u + "?dl=1"


def _filename_from_headers(h) -> str:
    cd = h.get("content-disposition", "") or ""
    m = re.search(r"filename\*=(?:UTF-8'')?([^;]+)", cd, re.I)
    if m:
        return unquote(m.group(1).strip().strip('"'))
    m = re.search(r'filename="?([^";]+)"?', cd, re.I)
    return m.group(1).strip() if m else ""


class Dropbox:
    name = "dropbox"

    def scan(self, url: str) -> tuple:
        direct = dropbox_url(url)
        fd, tmp = tempfile.mkstemp(prefix="videodl_", suffix=".zip")
        os.close(fd)
        zp = Path(tmp)
        is_zip = False
        single = ("file", 0)
        try:
            with SESSION.get(direct, stream=True, timeout=(20, 60)) as r:
                r.raise_for_status()
                it = r.iter_content(1024 * 1024)
                first = next(it, b"")
                if first[:2] == b"PK":
                    # Looks like a ZIP (folder link): download it so we can list/preview it.
                    is_zip = True
                    with zp.open("wb") as f:
                        f.write(first)
                        for chunk in it:
                            if chunk:
                                f.write(chunk)
                else:
                    # Single file: do NOT download it just to find that out.
                    nm = (_filename_from_headers(r.headers)
                          or unquote(Path(re.sub(r"[?#].*$", "", url)).name) or "file")
                    single = (nm, int(r.headers.get("content-length", 0) or 0))
        except Exception:
            zp.unlink(missing_ok=True)
            raise

        if is_zip:
            try:
                with zipfile.ZipFile(zp) as zf:
                    items = []
                    for i in zf.infolist():
                        if i.is_dir():
                            continue
                        nm = Path(i.filename).name
                        items.append(FileItem(
                            name=nm, size=i.file_size, source=url,
                            source_type="dropbox", zip_member=i.filename,
                            mime=mimetypes.guess_type(nm)[0] or "",
                            extra={"zip_path": str(zp)},
                        ))
                    if not items:
                        zp.unlink(missing_ok=True)
                        raise DownloadError("No files in that Dropbox ZIP.")
                    return "zip", items, {"zip_path": str(zp)}
            except zipfile.BadZipFile:
                # Starts with "PK" but isn't a real ZIP: treat as a single file.
                single = (unquote(Path(re.sub(r"[?#].*$", "", url)).name) or "file",
                          zp.stat().st_size)

        zp.unlink(missing_ok=True)
        nm, size = single
        return "file", [FileItem(
            name=nm, size=size, source=url, source_type="dropbox",
            direct_url=direct, mime=mimetypes.guess_type(nm)[0] or "",
        )], {}

    # -- ranged reads (used by previews and the loopback server) --------------

    @staticmethod
    def _stored_offset(item: FileItem):
        # Byte offset of a STORED (uncompressed) zip member, so seeks are O(1).
        off = item.extra.get("data_off", False)
        if off is not False:
            return off
        zp = item.extra.get("zip_path")
        off = None
        try:
            with zipfile.ZipFile(zp) as zf:
                zi = zf.getinfo(item.zip_member)
            if zi.compress_type == zipfile.ZIP_STORED and not (zi.flag_bits & 0x1):
                with open(zp, "rb") as f:
                    f.seek(zi.header_offset)
                    h = f.read(30)
                if h[:4] == b"PK\x03\x04":
                    off = (zi.header_offset + 30
                           + int.from_bytes(h[26:28], "little")
                           + int.from_bytes(h[28:30], "little"))
        except Exception:
            off = None
        item.extra["data_off"] = off
        return off

    def stream_range(self, item: FileItem, start: int, end: int):
        zp = item.extra.get("zip_path")
        if zp and Path(zp).exists():
            off = self._stored_offset(item)
            if off is not None:
                with open(zp, "rb") as f:
                    f.seek(off + start)
                    left = end - start + 1
                    while left > 0:
                        b = f.read(min(256 * 1024, left))
                        if not b:
                            return
                        left -= len(b)
                        yield b
                return
            with zipfile.ZipFile(zp) as zf, zf.open(item.zip_member) as src:
                src.seek(start)
                left = end - start + 1
                while left > 0:
                    b = src.read(min(256 * 1024, left))
                    if not b:
                        return
                    left -= len(b)
                    yield b
            return

        u = item.direct_url or dropbox_url(item.source)
        with SESSION.get(u, headers={"Range": f"bytes={start}-{end}"},
                         stream=True, timeout=(20, 60)) as r:
            r.raise_for_status()
            skip = start if r.status_code == 200 else 0   # server ignored Range
            left = end - start + 1
            for chunk in r.iter_content(256 * 1024):
                if not chunk:
                    continue
                if skip:
                    if skip >= len(chunk):
                        skip -= len(chunk)
                        continue
                    chunk = chunk[skip:]
                    skip = 0
                if len(chunk) > left:
                    chunk = chunk[:left]
                left -= len(chunk)
                yield chunk
                if left <= 0:
                    return

    def fetch_range(self, item: FileItem, start: int, end: int) -> bytes:
        return b"".join(self.stream_range(item, start, end))

    def download(self, item: FileItem, dest: Path, ctx: Optional[dict] = None) -> None:
        zp = item.extra.get("zip_path") or (ctx or {}).get("zip_path")
        total = item.size
        done = 0
        started = time.time()
        if zp and Path(zp).exists():
            with zipfile.ZipFile(zp) as zf, zf.open(item.zip_member) as src, \
                 dest.open("wb") as out:
                while True:
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
                    done += len(chunk)
                    sys.stdout.write("\r" + progress(done, total, started))
                    sys.stdout.flush()
        else:
            with SESSION.get(item.direct_url or dropbox_url(item.source),
                             stream=True, timeout=(20, 60)) as r:
                r.raise_for_status()
                total = int(r.headers.get("content-length", 0) or 0)
                with dest.open("wb") as out:
                    for chunk in r.iter_content(1024 * 1024):
                        if chunk:
                            out.write(chunk)
                            done += len(chunk)
                            sys.stdout.write("\r" + progress(done, total, started))
                            sys.stdout.flush()
        print()


# ---------------------------------------------------------------------------
# MEGA
# ---------------------------------------------------------------------------

MEGA_API = "https://g.api.mega.co.nz/cs"


def b64url(v: str) -> bytes:
    v = v.replace("-", "+").replace("_", "/").replace(",", "")
    v += "=" * ((4 - len(v) % 4) % 4)
    return base64.b64decode(v)


def to_a32(v: str) -> list:
    raw = b64url(v)
    if len(raw) % 4:
        raw += b"\0" * (4 - len(raw) % 4)
    return [int.from_bytes(raw[i:i + 4], "big") for i in range(0, len(raw), 4)]


def a32_bytes(vals) -> bytes:
    return b"".join(int(v & 0xFFFFFFFF).to_bytes(4, "big") for v in vals)


def dec_key(vals: list, key: list) -> list:
    out = []
    cph = AES.new(a32_bytes(key), AES.MODE_ECB)
    for i in range(0, len(vals), 4):
        blk = vals[i:i + 4]
        if len(blk) < 4:
            break
        d = cph.decrypt(a32_bytes(blk))
        out.extend(int.from_bytes(d[j:j + 4], "big") for j in range(0, 16, 4))
    return out


def dec_attr(enc: str, key: list) -> dict:
    raw = b64url(enc)
    if not raw:
        raise DownloadError("MEGA empty attributes.")
    cph = AES.new(a32_bytes(key[:4]), AES.MODE_CBC, b"\0" * 16)
    plain = cph.decrypt(raw).rstrip(b"\0")
    if not plain.startswith(b"MEGA"):
        raise DownloadError("MEGA attribute decrypt failed.")
    return json.loads(plain[4:].decode("utf-8"))


def parse_mega(url: str) -> tuple:
    u = url.strip()
    m = re.search(r"mega\.[^/]+/file/([A-Za-z0-9_-]+)#([A-Za-z0-9_-]+)", u)
    if m: return "file", m.group(1), m.group(2)
    m = re.search(r"mega\.[^/]+/folder/([A-Za-z0-9_-]+)#([A-Za-z0-9_-]+)", u)
    if m: return "folder", m.group(1), m.group(2)
    m = re.search(r"mega\.[^/]+/#F!([A-Za-z0-9_-]+)[!#]([A-Za-z0-9_-]+)", u)
    if m: return "folder", m.group(1), m.group(2)
    m = re.search(r"mega\.[^/]+/#!([A-Za-z0-9_-]+)!([A-Za-z0-9_-]+)", u)
    if m: return "file", m.group(1), m.group(2)
    raise DownloadError("Not a recognized public MEGA link.")


_MERR = {-2: "internal", -3: "overload", -4: "too many requests",
         -5: "bad request", -6: "unavailable", -8: "not found",
         -9: "denied", -11: "inaccessible", -16: "server error",
         -17: "transfer quota exceeded", -18: "too many requests",
         -19: "rate limited"}


def mega_call(cmd: dict, ctx: Optional[str] = None):
    params = {"id": 0}
    if ctx:
        params["n"] = ctx
    r = SESSION.post(MEGA_API, params=params, json=[cmd], timeout=(20, 60))
    r.raise_for_status()
    data = r.json()
    if not data:
        raise DownloadError("MEGA empty response.")
    res = data[0]
    if isinstance(res, int) and res < 0:
        raise DownloadError(f"MEGA error {res}: {_MERR.get(res, 'unknown')}")
    return res


def mega_file_key(ks: str) -> tuple:
    raw = to_a32(ks)
    if len(raw) < 8:
        raise DownloadError("MEGA key incomplete.")
    fk = [raw[0] ^ raw[4], raw[1] ^ raw[5], raw[2] ^ raw[6], raw[3] ^ raw[7]]
    return fk, [raw[4], raw[5]], (raw[6], raw[7])


def mega_node_key(node: dict, shared: list) -> list:
    enc = node.get("k", "")
    if ":" in enc:
        enc = enc.split(":", 1)[1]
    return dec_key(to_a32(enc), shared)


def mega_dl_info(handle: str, ctx: Optional[str] = None) -> dict:
    res = mega_call({"a": "g", "g": 1, "n": handle}, ctx)
    if not isinstance(res, dict) or "g" not in res:
        raise DownloadError("MEGA gave no download URL.")
    return res


# One API call per file, not one per chunk: cache the temporary download URL.
_MEGA_URLS: dict = {}
_MEGA_LOCK = threading.Lock()


def mega_url(item: FileItem, fresh: bool = False) -> str:
    key = (item.source_id, item.extra.get("context"))
    now = time.time()
    with _MEGA_LOCK:
        hit = _MEGA_URLS.get(key)
        if hit and not fresh and now - hit[1] < 600:
            return hit[0]
    info = mega_dl_info(item.source_id, item.extra.get("context"))
    with _MEGA_LOCK:
        _MEGA_URLS[key] = (info["g"], now)
    return info["g"]


def _mega_refusal(chunk: bytes):
    """If the first bytes look like MEGA's tiny JSON error body (e.g. [-17]),
    return the integer code; otherwise None. MEGA returns these with HTTP 200,
    so the only reliable detector is to peek at the payload."""
    if not chunk or chunk[:1] != b"[" or len(chunk) > 256:
        return None
    try:
        arr = json.loads(chunk.decode("utf-8", "replace"))
    except (ValueError, UnicodeDecodeError):
        return None
    if (isinstance(arr, list) and arr
            and isinstance(arr[0], int) and arr[0] < 0):
        return arr[0]
    return None


class Mega:
    name = "mega"

    # -- proxy plumbing -----------------------------------------------------

    @staticmethod
    def _proxy_url(raw_url: str) -> str:
        """Wrap a MEGA download URL through the Cloudflare Worker, if configured.
        Falls through to the raw URL when no proxy is set, so this is a no-op
        for anyone not using the quota bypass."""
        pool = [u.strip() for u in MEGA_PROXY_POOL.split(",") if u.strip()]
        if pool:
            base = _random.choice(pool)
        elif MEGA_PROXY:
            base = MEGA_PROXY
        else:
            return raw_url
        # quote() is essential: MEGA download URLs contain their own "?" and
        # "&", which would otherwise truncate the outer query string and the
        # Worker would receive a broken URL (or, if the path is "/", nothing
        # at all — the Worker's own health check would return "mega-proxy ok").
        return f"{base.rstrip('/')}/?url={quote(raw_url, safe='')}"

    # -- scan ---------------------------------------------------------------

    def scan(self, url: str) -> tuple:
        kind, handle, key = parse_mega(url)
        if kind == "file":
            return "file", self._scan_file(handle, key), {"key_string": key}
        return "folder", self._scan_folder(handle, key), \
               {"folder_handle": handle, "folder_key": key}

    def _scan_file(self, handle: str, ks: str) -> list:
        fk, iv, mm = mega_file_key(ks)
        info = mega_dl_info(handle)
        attrs = dec_attr(info["at"], fk)
        nm = attrs.get("n", "file")
        return [FileItem(
            name=nm, size=int(info.get("s", 0)),
            source_type="mega", source_id=handle, source_key=fk,
            direct_url=info["g"], mime=mimetypes.guess_type(nm)[0] or "",
            extra={"iv": iv, "meta_mac": mm},
        )]

    def _scan_folder(self, handle: str, ks: str) -> list:
        shared = to_a32(ks)
        items = []
        # Breadth-first walk of the folder tree. Each entry carries the key
        # context it should be decrypted under: root uses the shared key,
        # subfolders introduce their own key via node["k"].
        queue = [(handle, shared, None)]
        seen = set()
        while queue:
            fh, key_ctx, parent_handle = queue.pop(0)
            if fh in seen:
                continue
            seen.add(fh)
            try:
                res = mega_call({"a": "f", "c": 1, "r": 1, "ca": 1}, ctx=fh)
            except Exception:
                continue
            nodes = res.get("f", []) if isinstance(res, dict) else []
            for node in nodes:
                if node.get("t") not in (0, 1):
                    continue
                try:
                    key = mega_node_key(node, key_ctx)
                    if node["t"] == 0:
                        if len(key) < 8:
                            continue
                        fk = [key[0] ^ key[4], key[1] ^ key[5],
                              key[2] ^ key[6], key[3] ^ key[7]]
                        attrs = dec_attr(node["a"], fk)
                        nm = attrs.get("n", "file")
                        items.append(FileItem(
                            name=nm, size=int(node.get("s", 0)),
                            source_type="mega", source_id=node["h"], source_key=fk,
                            mime=mimetypes.guess_type(nm)[0] or "",
                            extra={"context": fh, "iv": [key[4], key[5]],
                                   "meta_mac": (key[6], key[7]),
                                   "parent": node.get("p", "") or parent_handle or "",
                                   "folder_handle": fh},
                        ))
                    else:
                        attrs = dec_attr(node["a"], key[:4])
                        # Subfolder: its key becomes the context for its children.
                        queue.append((node["h"], key, fh))
                except Exception:
                    continue
        if not items:
            raise DownloadError("No files found in that MEGA folder.")
        return items

    # -- streaming (proxy-aware; eager quota check) -------------------------

    def stream_range(self, item: FileItem, start: int, end: int):
        """Return a generator over the decrypted byte range.

        The initial HTTP request and the first-bytes peek happen EAGERLY,
        before this function returns. That lets callers detect MEGA's
        over-quota signal (-17: HTTP 200 with a tiny JSON body) before they
        commit to sending a 200/206 response — otherwise -17 bytes flow into
        the AES-CTR decryptor and the player gets garbage that looks like a
        video, producing a generic "cannot play" error with no diagnostic.
        """
        iv = item.extra.get("iv")
        fk = item.source_key
        if not iv or not fk:
            raise DownloadError("MEGA stream_range missing iv/key.")
        bs = (start // 16) * 16          # CTR needs a 16-byte aligned start

        raw_url = mega_url(item)
        use_url = self._proxy_url(raw_url)

        r = None
        for attempt in (0, 1):
            try:
                r = SESSION.get(use_url, headers={"Range": f"bytes={bs}-{end}"},
                                stream=True, timeout=(20, 60))
                r.raise_for_status()
                break
            except Exception:
                if r is not None:
                    r.close()
                if attempt:
                    raise
                # URL may have expired (or the proxy IP burned its quota).
                raw_url = mega_url(item, fresh=True)
                use_url = self._proxy_url(raw_url)

        it = r.iter_content(256 * 1024)
        try:
            first = next(it, b"")
        except Exception:
            r.close()
            raise
        if not first:
            r.close()
            raise DownloadError("MEGA returned an empty response.")

        code = _mega_refusal(first)
        if code is not None:
            r.close()
            if code == -17:
                raise DownloadError(
                    "MEGA transfer quota exceeded for this IP. "
                    "Enable MEGA_PROXY at the top of v6_simple.py "
                    "(deploy the Cloudflare Worker in MEGA_WORKER_JS, "
                    "paste the URL) or wait for the rolling 6-hour "
                    "window to reset.")
            raise DownloadError(
                f"MEGA refused the stream: {_MERR.get(code, f'error {code}')}")

        # If the first chunk is absurdly small and doesn't look like media,
        # include the hex in the error so it's diagnosable from the client.
        if (start == 0 and len(first) < 512
                and not _looks_like_media(first)):
            hex_head = first[:64].hex(" ")
            r.close()
            raise DownloadError(
                f"Upstream returned {len(first)} bytes, not media. "
                f"First bytes: {hex_head}")

        cv = (((iv[0] << 32) + iv[1]) << 64) + (bs // 16)
        cph = AES.new(a32_bytes(fk), AES.MODE_CTR,
                      counter=Counter.new(128, initial_value=cv))
        skip = start - bs

        def _gen():
            nonlocal skip
            try:
                for chunk in itertools.chain([first], it):
                    if not chunk:
                        continue
                    plain = cph.decrypt(chunk)
                    if skip:
                        if skip >= len(plain):
                            skip -= len(plain)
                            continue
                        plain = plain[skip:]
                        skip = 0
                    if plain:
                        yield plain
            finally:
                r.close()

        return _gen()

    def fetch_range(self, item: FileItem, start: int, end: int) -> bytes:
        return b"".join(self.stream_range(item, start, end))

    # -- download (proxy-aware; reuses stream_range so the -17 check applies) -

    def download(self, item: FileItem, dest: Path, ctx: Optional[dict] = None) -> None:
        info = mega_dl_info(item.source_id, item.extra.get("context"))
        total = int(info.get("s", item.size) or item.size)
        done = 0
        started = time.time()
        with dest.open("wb") as out:
            for chunk in self.stream_range(item, 0, total - 1):
                out.write(chunk)
                done += len(chunk)
                sys.stdout.write("\r" + progress(done, total, started))
                sys.stdout.flush()
        print()


def _looks_like_media(b: bytes) -> bool:
    if len(b) < 12:
        return False
    # mp4/mov
    if b[4:8] in (b"ftyp", b"moov", b"mdat", b"free", b"wide", b"skip"):
        return True
    # matroska / webm
    if b[:4] == b"\x1a\x45\xdf\xa3":
        return True
    # riff (avi/wav)
    if b[:4] == b"RIFF":
        return True
    # mp3 / id3
    if b[:3] == b"ID3" or b[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return True
    # flac
    if b[:4] == b"fLaC":
        return True
    # ogg
    if b[:4] == b"OggS":
        return True
    return False


SOURCES = {"dropbox": Dropbox(), "mega": Mega()}


# ---------------------------------------------------------------------------
# Local range server (feeds VLC)
# ---------------------------------------------------------------------------

class _RangeHandler(http.server.BaseHTTPRequestHandler):
    server_version = "V5Preview/1.0"

    def log_message(self, *a, **k):
        pass

    def do_HEAD(self):
        self._serve(True)

    def do_GET(self):
        self._serve(False)

    def _serve(self, head_only: bool) -> None:
        item: FileItem = self.server.item  # type: ignore[attr-defined]
        src = SOURCES[item.source_type]
        total = item.size
        if not total:
            self.send_error(500, "Unknown size.")
            return

        status = 200
        start, end = 0, total - 1
        rh = self.headers.get("Range")
        if rh and rh.startswith("bytes="):
            spec = rh[6:].split(",")[0].strip()
            a, _, b = spec.partition("-")
            try:
                if a == "" and b:
                    ln = int(b)
                    start = max(0, total - ln)
                    end = total - 1
                elif b == "":
                    start = int(a)
                    end = total - 1
                else:
                    start = int(a)
                    end = min(int(b), total - 1)
            except ValueError:
                self.send_error(416, "Malformed Range.")
                return
            if start > end or start >= total:
                self.send_error(416, "Range not satisfiable.")
                return
            status = 206

        # Build the generator eagerly so a -17 over-quota response is caught
        # before we commit to a 200/206 with Content-Length.
        try:
            gen = src.stream_range(item, start, end)
        except DownloadError as exc:
            self.send_error(503, str(exc)[:200])
            return
        except Exception:
            self.send_error(502, "upstream error")
            return

        self.send_response(status)
        self.send_header("Content-Type", item.mime or "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
        self.end_headers()
        if head_only:
            gen.close()
            return

        try:
            for piece in gen:
                self.wfile.write(piece)
        except OSError:
            pass            # player closed the connection (seek / stop)
        except Exception:
            pass
        finally:
            gen.close()


class _RangeServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        pass                # aborted connections are normal while seeking


def _stop_server(srv) -> None:
    try:
        srv.shutdown()
        srv.server_close()
    except Exception:
        pass


def start_range_server(item: FileItem):
    srv = _RangeServer(("127.0.0.1", 0), _RangeHandler)
    srv.item = item  # type: ignore[attr-defined]
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.1},
                         daemon=True)
    t.start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/stream"


# ---------------------------------------------------------------------------
# Soft dependencies
# ---------------------------------------------------------------------------

try:
    import tkinter as tk
    from tkinter import ttk, messagebox
    _TK = True
except Exception:
    tk = ttk = messagebox = None
    _TK = False

try:
    from PIL import Image, ImageTk
    _PIL = True
except Exception:
    Image = ImageTk = None
    _PIL = False

_VLC = None
_VLC_ERR = None
try:
    import vlc as _vlc_mod
    try:
        _VLC = _vlc_mod.Instance("--quiet", "--no-video-title-show")
        if _VLC is None:
            _VLC_ERR = "vlc.Instance() returned None."
    except Exception as exc:
        _VLC_ERR = f"libvlc init failed: {exc}"
        _VLC = None
except Exception as exc:
    _vlc_mod = None
    _VLC_ERR = f"python-vlc not installed: {exc}"

_VLC_OK = _VLC is not None


# ---------------------------------------------------------------------------
# Theme
# ---------------------------------------------------------------------------

DARK = {
    "bg": "#1a1a1c", "panel": "#232326", "panel_alt": "#2b2b30",
    "fg": "#e6e6ea", "fg_dim": "#9a9aa2",
    "accent": "#0e639c", "accent_h": "#1177bb",
    "border": "#3a3a42", "select": "#124a70",
}


def theme(root) -> None:
    st = ttk.Style(root)
    try:
        st.theme_use("clam")
    except Exception:
        pass
    root.configure(background=DARK["bg"])
    st.configure(".", background=DARK["panel"], foreground=DARK["fg"],
                 fieldbackground=DARK["panel_alt"], bordercolor=DARK["border"],
                 lightcolor=DARK["border"], darkcolor=DARK["border"])
    st.configure("TFrame", background=DARK["panel"])
    st.configure("TLabel", background=DARK["panel"], foreground=DARK["fg"])
    st.configure("Dim.TLabel", background=DARK["panel"], foreground=DARK["fg_dim"])
    st.configure("TButton", background=DARK["panel_alt"], foreground=DARK["fg"],
                 borderwidth=0, padding=(10, 6))
    st.map("TButton", background=[("active", DARK["accent_h"]),
                                  ("pressed", DARK["accent"])])
    st.configure("Accent.TButton", background=DARK["accent"], foreground="#fff",
                 borderwidth=0, padding=(14, 8))
    st.map("Accent.TButton", background=[("active", DARK["accent_h"]),
                                         ("pressed", DARK["accent"])])
    st.configure("TEntry", fieldbackground=DARK["panel_alt"],
                 foreground=DARK["fg"], insertcolor=DARK["fg"],
                 bordercolor=DARK["border"], padding=5)
    st.configure("TCombobox", fieldbackground=DARK["panel_alt"],
                 background=DARK["panel_alt"], foreground=DARK["fg"],
                 arrowcolor=DARK["fg"], padding=5)
    st.configure("Treeview", background=DARK["panel_alt"],
                 foreground=DARK["fg"], fieldbackground=DARK["panel_alt"],
                 rowheight=26, borderwidth=0)
    st.configure("Treeview.Heading", background=DARK["panel"],
                 foreground=DARK["fg_dim"], borderwidth=0, padding=6)
    st.map("Treeview", background=[("selected", DARK["select"])],
           foreground=[("selected", "#ffffff")])
    st.configure("TScale", background=DARK["panel"])
    st.configure("Horizontal.TScale", background=DARK["panel"])


# ---------------------------------------------------------------------------
# Preview panel
# ---------------------------------------------------------------------------

class PreviewPanel:
    """Owns whatever is currently drawn in the right-hand pane."""

    def __init__(self, host, browser):
        self.host = host
        self.browser = browser
        self.view = None
        self.range_srv = None
        # remembered across files
        self.settings = {"volume": 80, "muted": False, "rate": "1x",
                         "loop": False, "autonext": False}

    # -- teardown -----------------------------------------------------------

    def clear_player(self) -> None:
        if self.view is not None:
            try:
                self.view.close()
            except Exception:
                pass
            self.view = None
        if self.range_srv is not None:
            # shutdown() blocks up to the poll interval; keep the UI snappy.
            threading.Thread(target=_stop_server, args=(self.range_srv,),
                             daemon=True).start()
            self.range_srv = None

    def _wipe(self) -> None:
        self.clear_player()
        for w in self.host.winfo_children():
            w.destroy()

    def handle_key(self, e):
        v = self.view
        if v is None or v.closed:
            return None
        try:
            w = self.browser.root.focus_get()
        except Exception:
            w = None
        if isinstance(w, (tk.Entry, ttk.Entry, ttk.Combobox, tk.Text)):
            return None      # let people type in the search box
        return v.on_key(e)

    # -- dispatch -----------------------------------------------------------

    def show(self, item: FileItem) -> None:
        self._wipe()
        cat = item.category
        try:
            if cat == "video" or cat == "audio":
                self._video(item)
            elif cat == "image":
                self._image(item)
            elif cat == "text":
                self._text(item)
            else:
                self._info(item)
        except Exception as exc:
            self._error(item, str(exc))

    def _error(self, item: FileItem, msg: str) -> None:
        self._wipe()
        frame = ttk.Frame(self.host, padding=16)
        frame.pack(fill="both", expand=True)
        header = ttk.Frame(frame)
        header.pack(fill="x")
        ttk.Label(header, text="Preview unavailable",
                  font=("Segoe UI", 12, "bold")).pack(side="left")
        ttk.Button(header, text="Retry",
                   command=lambda: self.show(item)).pack(side="right")
        ttk.Label(frame, text=msg, wraplength=520, justify="left",
                  foreground="#e07070").pack(anchor="w", pady=(8, 12))
        self._info_rows(frame, item)

    # -- generic info -------------------------------------------------------

    def _info_rows(self, parent, item: FileItem) -> None:
        rows = [
            ("Name", item.name), ("Type", item.category),
            ("Size", item.display_size), ("Source", item.source_type),
            ("Extension", item.ext or "(none)"),
            ("MIME", item.mime or "unknown"),
            ("Duration", item.duration), ("Resolution", item.resolution),
        ]
        grid = ttk.Frame(parent, padding=(0, 6))
        grid.pack(fill="both", expand=True)
        for r, (label, val) in enumerate(rows):
            ttk.Label(grid, text=label + ":", foreground=DARK["fg_dim"]).grid(
                row=r, column=0, sticky="w", padx=(0, 12))
            ttk.Label(grid, text=val, wraplength=460, justify="left").grid(
                row=r, column=1, sticky="w")

    def _info(self, item: FileItem) -> None:
        frame = ttk.Frame(self.host, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="File information",
                  font=("Segoe UI", 12, "bold")).pack(anchor="w")
        self._info_rows(frame, item)

    # -- image --------------------------------------------------------------

    def _image(self, item: FileItem) -> None:
        if not _PIL:
            self._error(item, "Image preview requires Pillow.\n\npip install Pillow")
            return
        src = SOURCES[item.source_type]
        try:
            end = min(item.size or 32 * 1024 * 1024, 32 * 1024 * 1024) - 1
            data = src.fetch_range(item, 0, max(end, 0))
        except Exception as exc:
            self._error(item, f"Could not load image:\n{exc}")
            return
        try:
            img = Image.open(io.BytesIO(data))
            img.load()
        except Exception as exc:
            self._error(item, f"Unsupported image:\n{exc}")
            return

        canvas = tk.Canvas(self.host, background="#111111", highlightthickness=0)
        canvas.pack(fill="both", expand=True)
        holder = {"photo": None}

        def redraw(_e=None):
            w = max(canvas.winfo_width(), 1)
            h = max(canvas.winfo_height(), 1)
            scale = min(w / img.width, h / img.height, 1.0)
            nw, nh = max(1, int(img.width * scale)), max(1, int(img.height * scale))
            resized = img.resize((nw, nh), Image.LANCZOS)
            holder["photo"] = ImageTk.PhotoImage(resized)
            canvas.delete("all")
            canvas.create_image(w // 2, h // 2, image=holder["photo"])

        canvas.bind("<Configure>", redraw)
        self.host.after(40, redraw)

    # -- text ---------------------------------------------------------------

    def _text(self, item: FileItem) -> None:
        src = SOURCES[item.source_type]
        frame = ttk.Frame(self.host, padding=6)
        frame.pack(fill="both", expand=True)
        txt = tk.Text(frame, wrap="none", background="#161618",
                      foreground="#dcdce0", insertbackground="#dcdce0",
                      font=("Consolas", 10), borderwidth=0)
        vs = ttk.Scrollbar(frame, orient="vertical", command=txt.yview)
        hs = ttk.Scrollbar(frame, orient="horizontal", command=txt.xview)
        txt.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        txt.grid(row=0, column=0, sticky="nsew")
        vs.grid(row=0, column=1, sticky="ns")
        hs.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        try:
            end = min(item.size or TEXT_BYTES, TEXT_BYTES) - 1
            data = src.fetch_range(item, 0, max(end, 0))
        except Exception as exc:
            txt.insert("1.0", f"Could not load text:\n{exc}")
            txt.configure(state="disabled")
            return
        truncated = bool(item.size and item.size > TEXT_BYTES)
        body = data.decode("utf-8", errors="replace")
        if item.ext == ".json":
            try:
                body = json.dumps(json.loads(body), indent=2)
            except Exception:
                pass
        txt.insert("1.0", body)
        if truncated:
            txt.insert("end",
                       f"\n\n… truncated at {format_size(TEXT_BYTES)} "
                       f"of {item.display_size}.")
        txt.configure(state="disabled")

    # -- video / audio ------------------------------------------------------

    def _video(self, item: FileItem) -> None:
        if not _VLC_OK:
            self._error(item,
                        "Inline playback requires VLC.\n\n"
                        f"{_VLC_ERR or ''}\n\n"
                        "Install VLC from videolan.org, then `pip install python-vlc`, "
                        "then restart this app.")
            return
        try:
            self.range_srv, url = start_range_server(item)
        except Exception as exc:
            self._error(item, f"Could not start range server:\n{exc}")
            return
        try:
            self.view = VideoView(self, item, url)
        except Exception as exc:
            self._error(item, f"VLC playback failed:\n{exc}")


def ms_to_clock(ms) -> str:
    if ms is None or ms < 0:
        return "0:00"
    sec = int(ms) // 1000
    h, rem = divmod(sec, 3600)
    m, s_ = divmod(rem, 60)
    return f"{h}:{m:02d}:{s_:02d}" if h else f"{m}:{s_:02d}"


def embed_player(player, widget) -> None:
    widget.update_idletasks()
    wid = widget.winfo_id()
    if sys.platform.startswith("win"):
        player.set_hwnd(wid)
    elif sys.platform == "darwin":
        player.set_nsobject(wid)       # embedding on macOS Tk is unreliable
    else:
        player.set_xwindow(wid)


class SeekBar:
    """Click / drag timeline with a hover time tooltip."""
    H = 28

    def __init__(self, parent, on_seek, on_scrub=None):
        self.on_seek = on_seek
        self.on_scrub = on_scrub
        self.frac = 0.0
        self.length = 0
        self.drag = False
        self.hover = None
        self.cv = tk.Canvas(parent, height=self.H, background=DARK["panel"],
                            highlightthickness=0, cursor="hand2")
        self.cv.bind("<Configure>", lambda e: self.draw())
        self.cv.bind("<ButtonPress-1>", self._press)
        self.cv.bind("<B1-Motion>", self._move)
        self.cv.bind("<ButtonRelease-1>", self._release)
        self.cv.bind("<Motion>", self._hov)
        self.cv.bind("<Leave>", self._leave)

    def _frac_at(self, x: int) -> float:
        w = max(self.cv.winfo_width(), 1)
        return min(1.0, max(0.0, (x - 8) / max(w - 16, 1)))

    def set(self, frac: float, length: int) -> None:
        self.length = length
        if self.drag:
            return
        self.frac = min(1.0, max(0.0, frac))
        self.draw()

    def draw(self) -> None:
        cv = self.cv
        cv.delete("all")
        w = max(cv.winfo_width(), 20)
        x0, x1, mid = 8, w - 8, 18
        cv.create_rectangle(x0, mid - 3, x1, mid + 3, fill=DARK["border"], outline="")
        xp = x0 + (x1 - x0) * self.frac
        cv.create_rectangle(x0, mid - 3, xp, mid + 3, fill=DARK["accent_h"], outline="")
        tip = self.frac if self.drag else self.hover
        if tip is not None and self.length > 0:
            xh = x0 + (x1 - x0) * tip
            if not self.drag:
                cv.create_line(xh, mid - 7, xh, mid + 7, fill=DARK["fg_dim"])
            cv.create_text(min(max(xh, 24), w - 24), 6,
                           text=ms_to_clock(int(self.length * tip)),
                           fill=DARK["fg"], font=("Segoe UI", 8))
        r = 7 if (self.drag or self.hover is not None) else 5
        cv.create_oval(xp - r, mid - r, xp + r, mid + r, fill="#ffffff", outline="")

    def _press(self, e):
        self.drag = True
        self.frac = self._frac_at(e.x)
        self.draw()
        if self.on_scrub:
            self.on_scrub(self.frac)

    def _move(self, e):
        if self.drag:
            self.frac = self._frac_at(e.x)
            self.draw()
            if self.on_scrub:
                self.on_scrub(self.frac)

    def _release(self, e):
        if not self.drag:
            return
        self.frac = self._frac_at(e.x)
        self.drag = False
        self.draw()
        self.on_seek(self.frac)

    def _hov(self, e):
        if not self.drag:
            self.hover = self._frac_at(e.x)
            self.draw()

    def _leave(self, _e):
        self.hover = None
        self.draw()


class VideoView:
    RATES = ["0.5x", "0.75x", "1x", "1.25x", "1.5x", "2x"]
    HINT = ("Space play/pause  ·  ←/→ seek 5s (Shift 30s)  ·  J/L 10s  ·  ↑/↓ volume  ·  "
            "F fullscreen  ·  S snapshot  ·  . frame step  ·  [ ] speed  ·  0-9 jump  ·  N/P next/prev")

    def __init__(self, panel, item: FileItem, url: str):
        self.panel = panel
        self.item = item
        self.host = panel.host
        self.root = panel.browser.root
        self.cfg = panel.settings
        self.closed = False
        self.fs = None
        self.tick_id = None
        self.applied = False
        self.meta_done = False
        self.end_handled = False
        self._toast_until = 0.0

        self.player = _VLC.media_player_new()
        media = _VLC.media_new(url)
        media.add_option(":network-caching=1500")
        self.player.set_media(media)
        self._build()
        self.host.after(80, self._attach_and_play)

    # -- UI -----------------------------------------------------------------

    def _build(self) -> None:
        h = self.host
        self.area = tk.Frame(h, background="#000000", takefocus=True)
        self.area.pack(fill="both", expand=True)
        if self.item.category == "audio":
            tk.Label(self.area, text=f"♪\n{self.item.name}", background="#000000",
                     foreground=DARK["fg_dim"], font=("Segoe UI", 14),
                     justify="center", wraplength=480).place(relx=0.5, rely=0.5,
                                                              anchor="center")
        self._bind_input(self.area)

        bar = ttk.Frame(h, padding=(8, 2, 8, 6))
        bar.pack(fill="x")

        top = ttk.Frame(bar)
        top.pack(fill="x")
        self.seek = SeekBar(top, self.seek_frac, self._scrub)
        self.seek.cv.pack(side="left", fill="x", expand=True)
        self.time_lbl = ttk.Label(top, text="0:00", style="Dim.TLabel",
                                  width=17, anchor="e")
        self.time_lbl.pack(side="left", padx=(10, 0))

        rb = ttk.Frame(bar)
        rb.pack(fill="x", pady=(2, 0))

        def btn(parent, text, cmd, w=5):
            b = ttk.Button(parent, text=text, width=w, command=cmd)
            b.pack(side="left", padx=1)
            return b

        btn(rb, "⏮", lambda: self.panel.browser.step(-1, defer=True), 3)
        btn(rb, "⏪ 10", lambda: self.seek_rel(-10), 6)
        self.play_btn = btn(rb, "Pause", self.toggle_play, 7)
        btn(rb, "10 ⏩", lambda: self.seek_rel(10), 6)
        btn(rb, "⏭", lambda: self.panel.browser.step(1, defer=True), 3)
        btn(rb, "Frame", self.frame_step, 6)
        ttk.Label(rb, text="Speed", style="Dim.TLabel").pack(side="left", padx=(10, 4))
        self.speed = ttk.Combobox(rb, values=self.RATES, width=5, state="readonly")
        self.speed.set(self.cfg["rate"] if self.cfg["rate"] in self.RATES else "1x")
        self.speed.pack(side="left")
        self.speed.bind("<<ComboboxSelected>>", self._speed_changed)

        rc = ttk.Frame(bar)
        rc.pack(fill="x", pady=(4, 0))
        self.loop_var = tk.BooleanVar(value=self.cfg["loop"])
        self.next_var = tk.BooleanVar(value=self.cfg["autonext"])
        ttk.Checkbutton(rc, text="Loop", variable=self.loop_var,
                        command=self._flags_changed).pack(side="left", padx=(0, 8))
        ttk.Checkbutton(rc, text="Auto-next", variable=self.next_var,
                        command=self._flags_changed).pack(side="left", padx=(0, 8))
        if self.item.category == "video":
            btn(rc, "Snapshot", self.snapshot, 9)
            btn(rc, "Fullscreen", self.toggle_fullscreen, 10)
        self.mute_btn = btn(rc, "Unmute" if self.cfg["muted"] else "Mute",
                            self.toggle_mute, 7)
        self.vol = ttk.Scale(rc, from_=0, to=150, orient="horizontal", length=130,
                             command=self._vol_cmd)
        self.vol.set(self.cfg["volume"])
        self.vol.pack(side="left", padx=6, fill="x", expand=True)

        self.status = ttk.Label(bar, text=self.HINT, style="Dim.TLabel",
                                wraplength=540, justify="left")
        self.status.pack(fill="x", pady=(6, 0))

    def _bind_input(self, w) -> None:
        w.bind("<Button-1>", self._area_click)
        w.bind("<Double-Button-1>", lambda e: self.toggle_fullscreen())
        w.bind("<MouseWheel>", lambda e: self.adjust_volume(5 if e.delta > 0 else -5))
        w.bind("<Button-4>", lambda e: self.adjust_volume(5))
        w.bind("<Button-5>", lambda e: self.adjust_volume(-5))

    def _area_click(self, e) -> None:
        e.widget.focus_set()
        self.toggle_play()

    # -- lifecycle ----------------------------------------------------------

    def _safe(self, fn):
        try:
            return fn()
        except Exception:
            return None

    def _attach_and_play(self) -> None:
        if self.closed:
            return
        try:
            embed_player(self.player, self.area)
            self.player.video_set_mouse_input(False)   # let Tk see clicks / wheel
            self.player.video_set_key_input(False)
            self.player.play()
        except Exception as exc:
            self.toast(f"Playback error: {exc}", 6000)
        self.tick_id = self.host.after(250, self._tick)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.tick_id is not None:
            self._safe(lambda: self.host.after_cancel(self.tick_id))
        if self.fs is not None:
            self._safe(self.fs[0].destroy)
            self.fs = None
        self._safe(self.player.stop)
        self._safe(self.player.release)

    def _state(self):
        try:
            return self.player.get_state()
        except Exception:
            return None

    def toast(self, text: str, ms: int = 1400) -> None:
        self._toast_until = time.time() + ms / 1000.0
        self.status.configure(text=text)

    # -- periodic update ----------------------------------------------------

    def _tick(self) -> None:
        if self.closed:
            return
        S = _vlc_mod.State
        st = self._state()
        try:
            length = max(self.player.get_length(), 0)
            cur = max(self.player.get_time(), 0)
        except Exception:
            length = cur = 0

        if st == S.Playing and not self.applied:
            self.applied = True
            self._apply_settings()

        self.seek.set((cur / length) if length else 0.0, length)
        if not self.seek.drag:
            self.time_lbl.configure(
                text=(f"{ms_to_clock(cur)} / {ms_to_clock(length)}" if length
                      else ms_to_clock(cur)))

        if st == S.Ended:
            self.play_btn.configure(text="Replay")
        elif st in (S.Paused, S.Stopped):
            self.play_btn.configure(text="Play")
        else:
            self.play_btn.configure(text="Pause")

        if time.time() >= self._toast_until:
            label = {S.Opening: "Opening…", S.Buffering: "Buffering…",
                     S.Paused: "Paused", S.Ended: "Finished",
                     S.Error: "Playback error — try Retry or a different file"}.get(st, "")
            self.status.configure(text=label or self.HINT)

        if not self.meta_done and length > 0:
            changed = False
            if self.item.duration == "Unknown":
                self.item.duration = ms_to_clock(length)
                changed = True
            if self.item.category == "video" and self.item.resolution == "Unknown":
                size = self._safe(lambda: self.player.video_get_size(0))
                if size and size[0] > 0 and size[1] > 0:
                    self.item.resolution = f"{size[0]}x{size[1]}"
                    changed = True
            if changed:
                self.panel.browser.update_meta(self.item)
            if self.item.category == "audio" or self.item.resolution != "Unknown":
                self.meta_done = True

        if st == S.Ended and not self.end_handled:
            self.end_handled = True
            if self.loop_var.get():
                self.restart()
            elif self.next_var.get():
                self.panel.browser.step(1, media_only=True, defer=True)
        elif st != S.Ended:
            self.end_handled = False

        self.tick_id = self.host.after(250, self._tick)

    def _apply_settings(self) -> None:
        self._safe(lambda: self.player.audio_set_volume(int(self.cfg["volume"])))
        self._safe(lambda: self.player.audio_set_mute(bool(self.cfg["muted"])))
        self._safe(lambda: self.player.set_rate(float(self.cfg["rate"].rstrip("x"))))

    # -- transport ----------------------------------------------------------

    def restart(self) -> None:
        try:
            self.player.stop()
            self.player.play()
        except Exception:
            pass
        self.applied = False
        self.end_handled = False

    def toggle_play(self) -> None:
        S = _vlc_mod.State
        st = self._state()
        if st in (S.Ended, S.Stopped, S.Error):
            self.restart()
        elif st == S.Paused:
            self._safe(lambda: self.player.set_pause(0))
        else:
            self._safe(lambda: self.player.set_pause(1))

    def _set_time(self, ms: float) -> None:
        S = _vlc_mod.State
        ms = max(0, int(ms))
        if self._state() in (S.Ended, S.Stopped):
            self.restart()
            self.host.after(500, lambda: self._safe(lambda: self.player.set_time(ms)))
        else:
            self.player.set_time(ms)

    def seek_rel(self, sec: int) -> None:
        try:
            length = self.player.get_length()
            cur = self.player.get_time()
            if cur < 0:
                return
            target = cur + sec * 1000
            if length > 0:
                target = min(target, max(length - 500, 0))
            self._set_time(target)
            self.toast(f"{'+' if sec > 0 else ''}{sec}s   {ms_to_clock(target)}")
        except Exception:
            pass

    def seek_frac(self, frac: float) -> None:
        try:
            length = self.player.get_length()
            if length > 0:
                self._set_time(length * frac)
                self.toast(f"Jump to {ms_to_clock(length * frac)}")
        except Exception:
            pass

    def _scrub(self, frac: float) -> None:
        length = self._safe(self.player.get_length) or 0
        if length > 0:
            self.time_lbl.configure(
                text=f"{ms_to_clock(length * frac)} / {ms_to_clock(length)}")

    def frame_step(self) -> None:
        self._safe(lambda: self.player.set_pause(1))
        self.host.after(40, lambda: self._safe(self.player.next_frame))

    # -- audio / speed ------------------------------------------------------

    def _vol_cmd(self, v) -> None:
        self.cfg["volume"] = int(float(v))
        self._safe(lambda: self.player.audio_set_volume(self.cfg["volume"]))

    def adjust_volume(self, delta: int) -> None:
        v = max(0, min(150, int(self.cfg["volume"]) + delta))
        self.vol.set(v)
        self._vol_cmd(v)
        self.toast(f"Volume {v}%")

    def toggle_mute(self) -> None:
        self.cfg["muted"] = not self.cfg["muted"]
        self._safe(lambda: self.player.audio_set_mute(bool(self.cfg["muted"])))
        self.mute_btn.configure(text="Unmute" if self.cfg["muted"] else "Mute")
        self.toast("Muted" if self.cfg["muted"] else "Unmuted")

    def _speed_changed(self, _e=None) -> None:
        self._apply_rate(self.speed.get())
        self.area.focus_set()

    def _apply_rate(self, txt: str) -> None:
        self.cfg["rate"] = txt
        self._safe(lambda: self.player.set_rate(float(txt.rstrip("x"))))
        self.toast(f"Speed {txt}")

    def cycle_rate(self, d: int) -> None:
        cur = self.cfg["rate"] if self.cfg["rate"] in self.RATES else "1x"
        i = max(0, min(len(self.RATES) - 1, self.RATES.index(cur) + d))
        self.speed.set(self.RATES[i])
        self._apply_rate(self.RATES[i])

    def _flags_changed(self) -> None:
        self.cfg["loop"] = bool(self.loop_var.get())
        self.cfg["autonext"] = bool(self.next_var.get())

    # -- extras -------------------------------------------------------------

    def snapshot(self) -> None:
        try:
            out = Path.home() / "Pictures" / "VideoDownloader"
            out.mkdir(parents=True, exist_ok=True)
            cur = max(self.player.get_time(), 0)
            stem = safe_filename(Path(self.item.name).stem)[:60]
            path = unique_path(out / f"{stem}_{ms_to_clock(cur).replace(':', '-')}.png")
            rc = self.player.video_take_snapshot(0, str(path), 0, 0)
            self.toast(f"Snapshot saved: {path}" if rc == 0 else "Snapshot failed", 3500)
        except Exception as exc:
            self.toast(f"Snapshot failed: {exc}", 3500)

    def toggle_fullscreen(self) -> None:
        if self.item.category != "video":
            return
        try:
            if self.fs is None:
                top = tk.Toplevel(self.root)
                top.configure(background="#000000")
                top.attributes("-fullscreen", True)
                area = tk.Frame(top, background="#000000", takefocus=True)
                area.pack(fill="both", expand=True)
                self._bind_input(area)
                top.protocol("WM_DELETE_WINDOW", self.toggle_fullscreen)
                self.fs = (top, area)
                top.focus_force()
                area.focus_set()
                self.root.after(100, lambda: self._safe(
                    lambda: embed_player(self.player, area)))
            else:
                top, _area = self.fs
                self.fs = None
                embed_player(self.player, self.area)
                top.destroy()
                self.area.focus_set()
        except Exception as exc:
            self.toast(f"Fullscreen failed: {exc}", 3000)

    # -- keyboard -----------------------------------------------------------

    def on_key(self, e):
        k = e.keysym
        ch = (e.char or "").lower()
        shift = bool(e.state & 0x1)
        try:
            w = self.root.focus_get()
        except Exception:
            w = None
        focused = (w is self.area) or (self.fs is not None)

        if k == "space":
            if focused:
                self.toggle_play()
                return "break"
            return None                      # let the file list toggle its checkbox
        if k == "Escape" and self.fs is not None:
            self.toggle_fullscreen()
            return "break"
        if k in ("Left", "Right"):
            self.seek_rel((-1 if k == "Left" else 1) * (30 if shift else 5))
            return "break"
        if k in ("Up", "Down") and focused:
            self.adjust_volume(5 if k == "Up" else -5)
            return "break"
        if k == "Home":
            self._set_time(0)
            return "break"
        if ch == "j":
            self.seek_rel(-10)
        elif ch == "l":
            self.seek_rel(10)
        elif ch == "k":
            self.toggle_play()
        elif ch == "m":
            self.toggle_mute()
        elif ch == "f":
            self.toggle_fullscreen()
        elif ch == "s":
            if self.item.category == "video":
                self.snapshot()
        elif ch == ".":
            self.frame_step()
        elif ch == "[":
            self.cycle_rate(-1)
        elif ch == "]":
            self.cycle_rate(1)
        elif ch == "n":
            self.panel.browser.step(1, defer=True)
        elif ch == "p":
            self.panel.browser.step(-1, defer=True)
        elif ch and ch in "0123456789":
            self.seek_frac(int(ch) / 10.0)
        else:
            return None
        return "break"


# ---------------------------------------------------------------------------
# Browser
# ---------------------------------------------------------------------------

class Browser:
    def __init__(self, items: list, title: str = "Select Files"):
        if not _TK:
            raise DownloadError("Tkinter not available in this Python build.")
        self.items = [i for i in items if not i.is_folder]
        self.selected = []
        self.vars: dict = {}
        self.filtered = list(self.items)
        self._shown = None
        self._refreshing = False
        self.root = tk.Tk()
        self.root.title(title)
        self.root.geometry("1180x720")
        self.root.minsize(900, 560)
        self.root.protocol("WM_DELETE_WINDOW", self.cancel)
        theme(self.root)
        self._build()
        self.root.bind_all("<Key>", self.preview.handle_key)

    # -- layout -------------------------------------------------------------

    def _build(self) -> None:
        r = self.root
        r.columnconfigure(0, weight=1)
        r.rowconfigure(1, weight=1)

        bar = ttk.Frame(r, padding=(14, 10))
        bar.grid(row=0, column=0, sticky="ew")
        ttk.Label(bar, text="MEDIA BROWSER",
                  font=("Segoe UI", 12, "bold")).grid(row=0, column=0, padx=(0, 16))
        ttk.Label(bar, text="Search:").grid(row=0, column=1, padx=(0, 6))
        self.search_var = tk.StringVar()
        ttk.Entry(bar, textvariable=self.search_var, width=26).grid(
            row=0, column=2, padx=(0, 12))
        self.search_var.trace_add("write", lambda *_: self._refresh())

        ttk.Label(bar, text="Type:").grid(row=0, column=3, padx=(0, 6))
        self.kind_var = tk.StringVar(value="all")
        kf = ttk.Combobox(bar, textvariable=self.kind_var, width=10,
                          state="readonly",
                          values=["all", "video", "audio", "image", "text",
                                  "pdf", "archive", "other"])
        kf.grid(row=0, column=4, padx=(0, 12))
        kf.bind("<<ComboboxSelected>>", lambda e: self._refresh())

        ttk.Label(bar, text="Sort:").grid(row=0, column=5, padx=(0, 6))
        self.sort_var = tk.StringVar(value="name")
        sf = ttk.Combobox(bar, textvariable=self.sort_var, width=10,
                          state="readonly", values=["name", "size", "type"])
        sf.grid(row=0, column=6, padx=(0, 12))
        sf.bind("<<ComboboxSelected>>", lambda e: self._refresh())

        ttk.Button(bar, text="Select All",
                   command=self.select_all).grid(row=0, column=7, padx=2)
        ttk.Button(bar, text="Clear",
                   command=self.clear_all).grid(row=0, column=8, padx=2)
        ttk.Button(bar, text="Invert",
                   command=self.invert).grid(row=0, column=9, padx=2)

        body = ttk.Panedwindow(r, orient="horizontal")
        body.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 6))
        left = ttk.Frame(body)
        right = ttk.Frame(body)
        body.add(left, weight=1)
        body.add(right, weight=1)

        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)
        cols = ("sel", "name", "size", "duration", "resolution")
        self.tree = ttk.Treeview(left, columns=cols, show="headings",
                                 selectmode="browse")
        heads = {"sel": "✓", "name": "Name", "size": "Size",
                 "duration": "Duration", "resolution": "Resolution"}
        widths = {"sel": 40, "name": 340, "size": 90,
                  "duration": 80, "resolution": 100}
        anchors = {"sel": "center", "name": "w", "size": "e",
                   "duration": "center", "resolution": "center"}
        for col in cols:
            self.tree.heading(col, text=heads[col])
            self.tree.column(col, width=widths[col],
                             minwidth=widths[col] if col != "name" else 180,
                             stretch=(col == "name"),
                             anchor=anchors[col])
        self.tree.grid(row=0, column=0, sticky="nsew")
        vs = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        vs.grid(row=0, column=1, sticky="ns")
        hs = ttk.Scrollbar(left, orient="horizontal", command=self.tree.xview)
        hs.grid(row=1, column=0, sticky="ew")
        self.tree.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Button-1>", self._on_click, add="+")
        self.tree.bind("<Double-1>", self._on_double)
        self.tree.bind("<space>", lambda e: self._toggle())

        right.rowconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)
        self.preview_host = ttk.Frame(right)
        self.preview_host.grid(row=0, column=0, sticky="nsew")
        self.preview = PreviewPanel(self.preview_host, self)
        self._show_empty()

        bottom = ttk.Frame(r, padding=(14, 8))
        bottom.grid(row=2, column=0, sticky="ew")
        bottom.columnconfigure(1, weight=1)
        ttk.Button(bottom, text="DOWNLOAD SELECTED", style="Accent.TButton",
                   command=self.finish).grid(row=0, column=0, sticky="w")
        self.status = ttk.Label(bottom, text="0 selected / 0 files",
                                style="Dim.TLabel")
        self.status.grid(row=0, column=1, sticky="w", padx=14)
        ttk.Button(bottom, text="Cancel",
                   command=self.cancel).grid(row=0, column=2, sticky="e")

        self._refresh()

    # -- state --------------------------------------------------------------

    @staticmethod
    def _cell(v: str) -> str:
        return v if v and v != "Unknown" else "—"

    def _var(self, item: FileItem):
        if item.identity not in self.vars:
            self.vars[item.identity] = tk.BooleanVar(value=False)
        return self.vars[item.identity]

    def _selected_item(self):
        sel = self.tree.selection()
        if not sel:
            return None
        try:
            return self.filtered[int(sel[0])]
        except (ValueError, IndexError):
            return None

    def _row_values(self, it: FileItem) -> tuple:
        return ("☑" if self._var(it).get() else "☐", it.name,
                self._cell(it.display_size), self._cell(it.duration),
                self._cell(it.resolution))

    def _refresh(self) -> None:
        q = self.search_var.get().strip().lower()
        k = self.kind_var.get()
        rows = [i for i in self.items if q in i.name.lower()]
        if k != "all":
            rows = [i for i in rows if i.category == k]
        sk = self.sort_var.get()
        if sk == "size":
            rows.sort(key=lambda i: i.size, reverse=True)
        elif sk == "type":
            rows.sort(key=lambda i: (i.category, i.name.lower()))
        else:
            rows.sort(key=lambda i: i.name.lower())

        prev = self._selected_item()          # remember by identity, not row index
        self._refreshing = True               # ignore select events while rebuilding
        self.filtered = rows
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        keep = None
        for idx, it in enumerate(rows):
            self.tree.insert("", "end", iid=str(idx), values=self._row_values(it))
            if prev is not None and it.identity == prev.identity:
                keep = str(idx)
        if keep is not None:
            self.tree.selection_set(keep)
            self.tree.see(keep)
        self._update_status()
        self.root.after_idle(self._end_refresh)

    def _end_refresh(self) -> None:
        self._refreshing = False
        if not self.tree.selection() and self._shown is not None:
            self._show_empty()

    def _sync_checks(self) -> None:
        for iid in self.tree.get_children():
            it = self.filtered[int(iid)]
            self.tree.set(iid, "sel", "☑" if self._var(it).get() else "☐")
        self._update_status()

    def update_meta(self, item: FileItem) -> None:
        for idx, it in enumerate(self.filtered):
            if it is item:
                iid = str(idx)
                if self.tree.exists(iid):
                    self.tree.set(iid, "duration", self._cell(item.duration))
                    self.tree.set(iid, "resolution", self._cell(item.resolution))
                return

    def step(self, d: int, media_only: bool = False, defer: bool = False) -> None:
        if defer:                              # don't destroy a button inside its own callback
            self.root.after(20, lambda: self.step(d, media_only))
            return
        sel = self.tree.selection()
        i = (int(sel[0]) if sel else -1) + d
        while 0 <= i < len(self.filtered):
            if not media_only or self.filtered[i].category in ("video", "audio"):
                iid = str(i)
                self.tree.selection_set(iid)
                self.tree.focus(iid)
                self.tree.see(iid)
                return
            i += d

    def _update_status(self) -> None:
        chosen = [i for i in self.items
                  if self.vars.get(i.identity, tk.BooleanVar(value=False)).get()]
        total = sum(i.size for i in chosen)
        self.status.configure(
            text=f"{len(chosen)} selected / {len(self.items)} files  ·  "
                 f"{format_size(total)}")

    def _show_empty(self) -> None:
        self._shown = None
        self.preview.clear_player()
        for w in self.preview_host.winfo_children():
            w.destroy()
        ttk.Label(self.preview_host, padding=40, foreground=DARK["fg_dim"],
                  text="Select a file to preview it here.",
                  justify="center").pack(expand=True)

    # -- events -------------------------------------------------------------

    def _on_select(self, _e=None) -> None:
        if self._refreshing:
            return
        sel = self.tree.selection()
        if not sel:
            self._show_empty()
            return
        item = self.filtered[int(sel[0])]
        if self._shown == item.identity:      # same file: leave the player alone
            return
        self._shown = item.identity
        self.preview.show(item)

    def _on_click(self, e) -> None:
        if self.tree.identify_region(e.x, e.y) != "cell":
            return
        if self.tree.identify_column(e.x) != "#1":
            return
        iid = self.tree.identify_row(e.y)
        if iid:
            self._toggle(iid)

    def _on_double(self, e) -> None:
        if self.tree.identify_region(e.x, e.y) != "cell":
            return
        if self.tree.identify_column(e.x) == "#1":
            return
        self._toggle()

    def _toggle(self, iid=None) -> None:
        if iid is None:
            sel = self.tree.selection()
            if not sel:
                return
            iid = sel[0]
        it = self.filtered[int(iid)]
        v = self._var(it)
        v.set(not v.get())
        self.tree.set(iid, "sel", "☑" if v.get() else "☐")
        self._update_status()

    def select_all(self) -> None:
        for i in self.filtered:
            self._var(i).set(True)
        self._sync_checks()

    def clear_all(self) -> None:
        for i in self.filtered:
            self._var(i).set(False)
        self._sync_checks()

    def invert(self) -> None:
        for i in self.filtered:
            v = self._var(i)
            v.set(not v.get())
        self._sync_checks()

    def finish(self) -> None:
        self.selected = [i for i in self.items
                         if self.vars.get(i.identity, tk.BooleanVar(value=False)).get()]
        if not self.selected:
            messagebox.showwarning("Nothing selected", "Select at least one file.")
            return
        self.preview.clear_player()
        self.root.destroy()

    def cancel(self) -> None:
        self.selected = []
        self.preview.clear_player()
        self.root.destroy()

    def run(self) -> list:
        self.root.mainloop()
        return self.selected


# ---------------------------------------------------------------------------
# Terminal
# ---------------------------------------------------------------------------

def proxy_status() -> str:
    if MEGA_PROXY_POOL.strip():
        n = len([u for u in MEGA_PROXY_POOL.split(",") if u.strip()])
        return f"pool of {n} Cloudflare Worker(s)"
    if MEGA_PROXY.strip():
        return MEGA_PROXY.strip()
    return "off (direct MEGA URLs)"


def boot() -> None:
    clear()
    banner()
    section("Environment check")
    info(f"Python      {sys.version.split()[0]}")
    info(f"Tkinter     {'ok' if _TK else 'missing — GUI selector disabled'}")
    info(f"Pillow      {'ok' if _PIL else 'missing — image preview disabled'}")
    info(f"python-vlc  {'ok' if _VLC_OK else (_VLC_ERR or 'missing — video preview disabled')}")
    section("MEGA quota bypass")
    info(f"Proxy       {proxy_status()}")


def menu() -> str:
    section("Main menu")
    for i, n in enumerate(SOURCES, 1):
        print(f"    {c(f'[{i}]', 'cyan')}  {n.capitalize()}")
    print(f"    {c('[q]', 'cyan')}  Quit")
    return ask("Select: ").lower()


def run_source(name: str) -> None:
    clear()
    banner(subtitle=f"{name}  ·  paste a public link")
    section(f"{name.capitalize()} public link")
    info("No account login required.")
    url = ask("Link: ")
    if not url:
        return
    src = SOURCES[name]
    ctx: dict = {}
    try:
        section("Fetching metadata")
        kind, items, ctx = src.scan(url)
        ok(f"Found {len(items)} item(s) — opening selector")
        if not _TK:
            err("Tkinter is not available; cannot open the selector.")
            return
        selected = Browser(items).run()
        if not selected:
            warn("Selection cancelled.")
            return
        clear()
        banner(subtitle=f"downloading {len(selected)} file(s)")
        out = Path.home() / "Downloads" / f"{name.capitalize()}_Videos"
        out.mkdir(parents=True, exist_ok=True)
        info(f"Output: {out}")
        failed = []
        for idx, item in enumerate(selected, 1):
            dest = unique_path(out / safe_filename(item.name))
            section(f"[{idx}/{len(selected)}]  {item.name}")
            try:
                src.download(item, dest, ctx)
                ok(f"Saved: {dest}")
            except KeyboardInterrupt:
                dest.unlink(missing_ok=True)
                raise
            except Exception as exc:
                dest.unlink(missing_ok=True)
                print()
                err(f"Failed: {exc}")
                failed.append(item.name)
        print()
        if failed:
            warn(f"{len(selected) - len(failed)} saved, {len(failed)} failed:")
            for n in failed:
                info(n)
        else:
            ok("Download complete.")
    except KeyboardInterrupt:
        print()
        warn("Cancelled.")
    except Exception as exc:
        err(str(exc))
    finally:
        zp = ctx.get("zip_path")
        if zp:
            try:
                Path(zp).unlink(missing_ok=True)
            except Exception:
                pass


def main() -> None:
    boot()
    input(c("\n  Press Enter to continue…", "dim"))
    while True:
        clear()
        banner()
        ch = menu()
        if ch in ("q", "quit", "exit"):
            clear()
            print(c("  Goodbye.", "dim"))
            return
        names = list(SOURCES.keys())
        if ch.isdigit() and 1 <= int(ch) <= len(names):
            run_source(names[int(ch) - 1])
        else:
            warn("Invalid choice.")
        input(c("\n  Press Enter to return to the menu…", "dim"))


if __name__ == "__main__":
    main()