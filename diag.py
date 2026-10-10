#!/usr/bin/env python3
"""diag.py — V6 diagnostics server (v1.2).

Reports are PRINTED TO THE PC TERMINAL as soon as they arrive. No /reports
page needed. Just watch this window after tapping "Send to PC" on the iPad.

Focus of this version: testing whether the iPad can play a real v6_web.py
stream URL. The "v6_web preview" section on the client takes the stream URL
from your running v6_web instance and plays it in <video> on the iPad.

Run:
    python diag.py --no-pin --port 8770
"""
from __future__ import annotations

import argparse
import atexit
import datetime as _dt
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

HAVE_CORE = False
core = None
try:
    import v6_simple as core  # type: ignore
    HAVE_CORE = all(hasattr(core, n) for n in
                    ("SOURCES", "safe_filename", "unique_path", "DownloadError"))
except Exception as _exc:
    print(f"[diag] v6_simple not importable ({_exc}); MEGA tests skipped.")

NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
PIN = ""
NO_PIN = False
TEST_MP4: Path | None = None
LOG: list = []
LOG_LOCK = threading.Lock()
START_TS = time.time()
REPORTS: list = []
REPORTS_LOCK = threading.RLock()


def find_tool(name):
    exe = shutil.which(name)
    if exe:
        return exe
    for cand in (HERE / f"{name}.exe", HERE / name):
        if cand.is_file():
            return str(cand)
    return None


def log(kind, **fields):
    e = {"t": round(time.time() - START_TS, 3), "kind": kind}
    e.update(fields)
    with LOG_LOCK:
        LOG.append(e)
        if len(LOG) > 5000:
            del LOG[:1000]
    short = " ".join(f"{k}={v}" for k, v in fields.items()
                     if k in ("msg", "path", "status", "err", "code",
                              "bytes", "note", "ua", "ip"))
    print(f"[diag {e['t']:7.3f}] {kind:<16} {short}", flush=True)


def print_report(entry):
    bar = "=" * 78
    thin = "-" * 78
    try:
        when = _dt.datetime.fromtimestamp(entry["time"]).strftime("%H:%M:%S")
    except Exception:
        when = "?"
    print("", flush=True)
    print(bar, flush=True)
    print(f"  REPORT FROM {entry['label']}   [{entry['ip']}]   {when}   id={entry['id']}",
          flush=True)
    print(f"  UA: {entry['ua'][:110]}", flush=True)
    print(bar, flush=True)
    s = entry.get("summary") or {}
    if s:
        for k in ("mega_url", "v6base", "v6stream", "platform", "dpr"):
            v = s.get(k)
            if v:
                print(f"    {k}: {v}", flush=True)
        print(thin, flush=True)
    print("", flush=True)
    print(entry.get("report") or "(empty)", flush=True)
    print("", flush=True)
    print(bar, flush=True)
    print("", flush=True)


def make_test_mp4(path):
    ffmpeg = find_tool("ffmpeg")
    if not ffmpeg:
        return False
    cmd = [ffmpeg, "-y", "-loglevel", "error",
           "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15:duration=3",
           "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
           "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
           "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "64k", "-ac", "2",
           "-movflags", "+faststart", "-t", "3", str(path)]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=30, creationflags=NOWIN)
    except Exception as exc:
        print(f"[diag] ffmpeg failed: {exc}")
        return False
    if r.returncode != 0 or not path.exists():
        print(f"[diag] ffmpeg exit {r.returncode}")
        return False
    return path.stat().st_size > 0


def parse_range(h, total):
    if not h or not h.startswith("bytes="):
        return 0, total - 1, False
    a, _, b = h[6:].split(",")[0].strip().partition("-")
    try:
        if a == "":
            n = int(b)
            if n <= 0:
                return None
            return max(0, total - n), total - 1, True
        start = int(a)
        end = total - 1 if b == "" else min(int(b), total - 1)
    except ValueError:
        return None
    if start > end or start >= total:
        return None
    return start, end, True


def sniff(b):
    if len(b) < 12:
        return "too short"
    if b[4:8] in (b"ftyp", b"moov", b"mdat"):
        return "mp4/mov"
    if b[:4] == b"\x1a\x45\xdf\xa3":
        return "matroska/webm"
    if b[:4] == b"RIFF":
        return "riff (avi/wav)"
    if b[:3] == b"ID3":
        return "mp3"
    if b[:4] == b"OggS":
        return "ogg"
    if b[:4] == b"fLaC":
        return "flac"
    if b[:4] == b"\x00\x00\x01\xba":
        return "mpeg"
    if b[:1] in (b"[", b"{"):
        return "json?"
    if b[:5].lower() in (b"<!doc", b"<html"):
        return "html"
    return "unknown"


def is_mega_error(b):
    if not b or b[:1] != b"[":
        return None
    try:
        arr = json.loads(b.decode("utf-8", "replace"))
    except Exception:
        return None
    if isinstance(arr, list) and arr and isinstance(arr[0], int) and arr[0] < 0:
        return arr[0]
    return None


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "V6Diag/1.2"
    timeout = 60
    disable_nagle_algorithm = True

    def log_message(self, *a):
        pass

    def _ip(self):
        return self.client_address[0]

    def _ua(self):
        return self.headers.get("User-Agent", "")

    def _authed(self):
        if NO_PIN:
            return True
        try:
            ck = SimpleCookie(self.headers.get("Cookie", ""))
            tok = ck["diag"].value if "diag" in ck else ""
        except Exception:
            tok = ""
        if tok and tok == PIN:
            return True
        q = parse_qs(urlparse(self.path).query).get("p", [""])[0]
        return q == PIN

    def _json(self, obj, status=200, extra=None):
        body = json.dumps(obj).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8",
                   extra=extra)

    def _send(self, status, body, ctype, extra=None):
        head = self.command == "HEAD"
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or []):
            self.send_header(k, v)
        self.end_headers()
        if not head and body:
            self.wfile.write(body)

    def do_HEAD(self): self._route()
    def do_GET(self):  self._route()

    def do_POST(self):
        p = urlparse(self.path).path
        if not self._authed():
            return self._json({"error": "login required"}, 401)
        if p == "/api/report":
            return self._api_report()
        return self._json({"error": "not found"}, 404)

    def _route(self):
        p = urlparse(self.path).path
        q = parse_qs(urlparse(self.path).query)

        if p == "/":
            return self._page()
        if p == "/api/ping":
            return self._json({
                "ok": True, "server_time": time.time(), "server": "V6Diag",
                "version": "1.2", "have_core": HAVE_CORE,
                "have_test_mp4": bool(TEST_MP4 and TEST_MP4.exists()),
                "have_ffmpeg": bool(find_tool("ffmpeg")),
            })
        if p == "/api/echo":
            return self._json({
                "client_ip": self._ip(), "user_agent": self._ua(),
                "headers": dict(self.headers),
            })
        if p == "/api/log":
            with LOG_LOCK:
                return self._json({"log": list(LOG)})
        if p == "/api/log/clear":
            with LOG_LOCK:
                LOG.clear()
            return self._json({"ok": True})

        if not self._authed():
            return self._json({"error": "login required"}, 401)

        if p == "/api/test-file":
            return self._serve_test_file()
        if p == "/api/test-file-nocsp":
            return self._serve_test_file(nocsp=True)
        if p == "/api/test-file-plain":
            return self._serve_test_file(plain=True)

        if p == "/api/mega-scan":
            return self._mega_scan(q)
        if p == "/api/mega-probe":
            return self._mega_probe(q)
        if p == "/api/mega-stream":
            return self._mega_stream(q)

        return self._json({"error": "not found", "path": p}, 404)

    # -- local test file ---------------------------------------------------

    def _serve_test_file(self, nocsp=False, plain=False):
        if not (TEST_MP4 and TEST_MP4.exists()):
            return self._json({"error": "no test mp4"}, 500)
        total = TEST_MP4.stat().st_size
        rng = parse_range(self.headers.get("Range"), total)
        if rng is None:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{total}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        start, end, partial = rng
        tag = "plain" if plain else ("nocsp" if nocsp else "full")
        log("test-file", path=tag, status=206 if partial else 200,
            bytes=end - start + 1, ip=self._ip(), ua=self._ua()[:50])
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(end - start + 1))
        if not plain:
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "private, max-age=600")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
        self.end_headers()
        if self.command == "HEAD":
            return
        try:
            remaining = end - start + 1
            with TEST_MP4.open("rb") as f:
                f.seek(start)
                while remaining > 0:
                    chunk = f.read(min(262144, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except OSError:
            self.close_connection = True

    # -- MEGA --------------------------------------------------------------

    def _mega_scan(self, q):
        if not HAVE_CORE:
            return self._json({"error": "v6_simple not importable"}, 500)
        url = q.get("url", [""])[0].strip()
        if not url:
            return self._json({"error": "missing url"}, 400)
        t0 = time.time()
        try:
            kind, items, ctx = core.SOURCES["mega"].scan(url)
            files = [i for i in items if not i.is_folder]
            log("mega-scan", status=200, bytes=len(files), ip=self._ip())
            return self._json({
                "ok": True, "kind": kind, "count": len(files),
                "first": [{"idx": n, "name": i.name, "size": i.size,
                           "has_iv": bool(i.extra.get("iv")),
                           "has_key": bool(i.source_key)}
                          for n, i in enumerate(files[:3])],
                "elapsed_ms": round((time.time() - t0) * 1000),
            })
        except Exception as exc:
            log("mega-scan", status=500, err=str(exc)[:100], ip=self._ip())
            return self._json({"ok": False, "error": str(exc)}, 500)

    def _mega_probe(self, q):
        if not HAVE_CORE:
            return self._json({"error": "v6_simple not importable"}, 500)
        url = q.get("url", [""])[0].strip()
        idx = int(q.get("idx", ["0"])[0])
        if not url:
            return self._json({"error": "missing url"}, 400)
        t0 = time.time()
        try:
            kind, items, ctx = core.SOURCES["mega"].scan(url)
            files = [i for i in items if not i.is_folder]
            if not files:
                return self._json({"ok": False, "error": "no files"}, 404)
            item = files[idx] if 0 <= idx < len(files) else files[0]
            data = b"".join(core.SOURCES["mega"].stream_range(item, 0, 4095))
            s = sniff(data)
            log("mega-probe", status=200, bytes=len(data), note=s, ip=self._ip())
            return self._json({
                "ok": True,
                "item": {"name": item.name, "size": item.size},
                "got_bytes": len(data), "sniff": s,
                "head_hex": data[:16].hex(" "),
                "is_mega_error": is_mega_error(data),
                "elapsed_ms": round((time.time() - t0) * 1000),
                "proxy": getattr(core, "MEGA_PROXY", ""),
            })
        except Exception as exc:
            log("mega-probe", status=500, err=str(exc)[:100], ip=self._ip())
            return self._json({"ok": False, "error": str(exc)}, 500)

    def _mega_stream(self, q):
        if not HAVE_CORE:
            return self._json({"error": "v6_simple not importable"}, 500)
        url = q.get("url", [""])[0].strip()
        idx = int(q.get("idx", ["0"])[0])
        if not url:
            return self._json({"error": "missing url"}, 400)
        try:
            kind, items, ctx = core.SOURCES["mega"].scan(url)
            files = [i for i in items if not i.is_folder]
            if not files:
                return self._json({"ok": False, "error": "no files"}, 404)
            item = files[idx] if 0 <= idx < len(files) else files[0]
        except Exception as exc:
            return self._json({"ok": False, "error": str(exc)}, 500)

        total = item.size
        if total <= 0:
            return self._json({"error": "unknown size"}, 500)
        rng = parse_range(self.headers.get("Range"), total)
        if rng is None:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{total}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        start, end, partial = rng

        try:
            gen = core.SOURCES["mega"].stream_range(item, start, end)
        except Exception as exc:
            log("mega-stream", status=503, err=str(exc)[:100], ip=self._ip())
            return self._json({"error": str(exc)}, 503)

        log("mega-stream", status=206 if partial else 200,
            bytes=end - start + 1, ip=self._ip(), ua=self._ua()[:50])

        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
        self.send_header("Cache-Control", "private, max-age=600")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if self.command == "HEAD":
            gen.close()
            return
        try:
            sent = 0
            for piece in gen:
                self.wfile.write(piece)
                sent += len(piece)
            if sent != end - start + 1:
                self.close_connection = True
        except OSError:
            self.close_connection = True
        except Exception:
            self.close_connection = True
        finally:
            gen.close()

    # -- reports -----------------------------------------------------------

    def _api_report(self):
        try:
            n = int(self.headers.get("Content-Length", 0) or 0)
            if n > 512 * 1024:
                return self._json({"error": "too large"}, 413)
            raw = self.rfile.read(n) if n else b""
            payload = json.loads(raw.decode("utf-8") or "{}")
        except Exception as exc:
            return self._json({"error": f"bad body: {exc}"}, 400)

        rid = uuid.uuid4().hex[:8]
        entry = {
            "id": rid,
            "label": (payload.get("label") or self._guess_label())[:60],
            "ip": self._ip(),
            "ua": self._ua(),
            "time": time.time(),
            "report": str(payload.get("report") or "")[:200_000],
            "summary": payload.get("summary") or {},
        }
        with REPORTS_LOCK:
            REPORTS.append(entry)
            if len(REPORTS) > 40:
                del REPORTS[:len(REPORTS) - 40]
        log("report-in", ip=self._ip(), ua=self._ua()[:40],
            note=f"id={rid} label={entry['label']} bytes={len(entry['report'])}")
        print_report(entry)
        return self._json({"ok": True, "id": rid})

    def _guess_label(self):
        ua = self._ua()
        for k in ("iPad", "iPhone", "Android"):
            if k in ua:
                return k
        ip = self._ip()
        if ip in ("127.0.0.1", "::1"):
            return "PC (browser)"
        return f"Device {ip}"

    def _page(self):
        body = PAGE.encode("utf-8")
        self._send(200, body, "text/html; charset=utf-8")


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>V6 Diagnostics</title><style>
:root{--bg:#111114;--panel:#1e1e22;--alt:#2a2a30;--fg:#e6e6ea;--dim:#8a8a92;--ok:#4caf7d;--bad:#e07070;--warn:#e0b060;--acc:#0e639c}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--fg);font:15px -apple-system,system-ui,Segoe UI,sans-serif}
body{padding:14px;padding-bottom:env(safe-area-inset-bottom)}
h1{font-size:19px;margin:0 0 4px}
.dim{color:var(--dim)}.sm{font-size:13px}
.card{background:var(--panel);border-radius:12px;padding:14px;margin-bottom:12px}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:8px}
input,button,textarea{font:inherit;color:var(--fg);background:var(--alt);border:1px solid #3a3a42;border-radius:8px;padding:8px 10px}
input{flex:1;min-width:180px;box-sizing:border-box}
button{cursor:pointer}
button.pri{background:var(--acc);border-color:var(--acc);color:#fff}
button.ok{background:#2e7d32;border-color:#2e7d32;color:#fff}
button:disabled{opacity:.5;cursor:default}
textarea{width:100%;min-height:260px;font:12px ui-monospace,Menlo,Consolas,monospace;white-space:pre;overflow:auto}
.res{padding:6px 0;border-bottom:1px solid #2a2a30;font-size:14px}
.res .name{font-weight:600}
.res .stat{float:right;font-size:12px}
.ok{color:var(--ok)}.bad{color:var(--bad)}.warn{color:var(--warn)}
pre.out{background:#0c0c0e;padding:8px;border-radius:6px;font-size:11px;max-height:220px;overflow:auto;margin:4px 0 0;white-space:pre-wrap;word-break:break-all}
.legend{font-size:12px;color:var(--dim);margin-top:6px}
code{background:var(--alt);padding:1px 5px;border-radius:4px;font-size:12px}
</style></head><body>
<h1>V6 Diagnostics</h1>
<div class="dim sm" id="dev"></div>

<div class="card">
  <div class="row"><input id="url" placeholder="MEGA link (optional, for MEGA tests)"></div>
  <div class="row">
    <button class="pri" id="run">Run all tests</button>
    <button id="clear">Clear log</button>
    <button id="copy">Copy report</button>
  </div>
  <div class="legend">Run on PC, then iPad. After tests finish, tap <b>Send to PC</b> below. The report appears instantly in the PC terminal window.</div>
</div>

<div class="card">
  <div><b>Test v6_web.py preview (the important one)</b></div>
  <div class="legend">
    Open the Video Downloader on the iPad, tap a video, then tap <b>Open</b> next to it — a new tab opens with the stream URL. Copy that whole URL into the field below and tap the button. This plays the exact URL v6_web uses, in the exact way Safari plays it.
  </div>
  <div class="row" style="margin-top:8px">
    <input id="v6stream" placeholder="http://192.168.1.122:8765/stream/SID/IDX?t=...&d=..." inputmode="url" autocapitalize="off" autocorrect="off">
  </div>
  <div class="row">
    <button class="pri" id="v6test">Play v6_web stream on this device</button>
    <span class="dim sm" id="v6status"></span>
  </div>
  <pre class="out" id="v6state">—</pre>
</div>

<div class="card" id="res">
  <div class="res dim">Run tests to begin.</div>
</div>

<div class="card" id="sendcard" hidden>
  <div><b>Send to PC</b></div>
  <div class="legend">Pushes the full report to the PC terminal. One tap.</div>
  <div class="row" style="margin-top:8px">
    <input id="label" placeholder="Label">
    <button class="pri" id="send">Send to PC</button>
    <span class="dim sm" id="sendstatus"></span>
  </div>
</div>

<div class="card">
  <div class="row"><b>Full log</b><span class="dim sm" id="logStatus"></span></div>
  <textarea id="log" readonly></textarea>
</div>

<script>
const $=s=>document.querySelector(s);
const qs=new URLSearchParams(location.search);
const P=qs.get('p')||'';
const Q=P?('?p='+encodeURIComponent(P)):'';
let report=[];
const push=l=>{report.push(l);$('#log').value=report.join('\n');$('#log').scrollTop=1e9};
const esc=s=>String(s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));

function row(name,status,ok,detail){
  const r=document.createElement('div');
  r.className='res';
  const c=ok===true?'ok':(ok===false?'bad':'warn');
  r.innerHTML='<span class="name">'+esc(name)+'</span><span class="stat '+c+'">'+
              esc(status)+'</span>'+(detail?'<div class="dim sm">'+esc(detail)+'</div>':'');
  $('#res').appendChild(r);
}

function reset(){$('#res').innerHTML='';report=[];$('#log').value='';}

function deviceInfo(){
  const p=[];
  p.push('UA: '+navigator.userAgent);
  p.push('platform: '+navigator.platform);
  const v=document.createElement('video');
  p.push('mp4/h264: '+v.canPlayType('video/mp4; codecs="avc1.42E01E"'));
  p.push('mp4/hevc: '+v.canPlayType('video/mp4; codecs="hvc1.1.6.L93.B0"'));
  $('#dev').textContent=p.join(' | ');
  const lbl=$('#label');
  if(lbl&&!lbl.value){
    lbl.value = /iPad/.test(navigator.userAgent) ? 'iPad'
              : /iPhone/.test(navigator.userAgent) ? 'iPhone'
              : /Android/.test(navigator.userAgent) ? 'Android'
              : 'Browser';
  }
}

async function timedJson(url, init){
  const t0=performance.now();
  try{
    const r=await fetch(url, init);
    const text=await r.text();
    let j=null;try{j=JSON.parse(text)}catch(e){}
    return {status:r.status, text, json:j, ms:Math.round(performance.now()-t0)};
  }catch(e){return {error:String(e), ms:Math.round(performance.now()-t0)};}
}

async function testVideo(url, timeoutMs){
  return new Promise(resolve=>{
    const v=document.createElement('video');
    v.preload='metadata'; v.muted=true; v.playsInline=true;
    const done=r=>{try{v.removeAttribute('src');v.load();}catch(e){}resolve(r);};
    const t=setTimeout(()=>done({ok:false,err:'timeout',
        readyState:v.readyState,networkState:v.networkState}), timeoutMs||15000);
    v.addEventListener('loadedmetadata',()=>{clearTimeout(t);
      done({ok:true,duration:v.duration,w:v.videoWidth,h:v.videoHeight,
            readyState:v.readyState,networkState:v.networkState});},{once:true});
    v.addEventListener('error',()=>{clearTimeout(t);const e=v.error;
      done({ok:false,err:'error',code:e&&e.code,message:e&&e.message,
            readyState:v.readyState,networkState:v.networkState});},{once:true});
    v.src=url;
  });
}

async function test1(){
  push('');push('=== 1. Ping ===');
  const r=await timedJson('/api/ping');
  if(r.error){row('1. Ping','FAIL',false,r.error);push('FAIL '+r.error);return}
  const j=r.json||{};
  row('1. Ping','OK '+r.ms+'ms',true,
      'core='+j.have_core+' ffmpeg='+j.have_ffmpeg+' mp4='+j.have_test_mp4);
  push('OK '+r.ms+'ms '+JSON.stringify(j));
}
async function test2(){
  push('');push('=== 2. Echo ===');
  const r=await timedJson('/api/echo');
  if(r.error){row('2. Echo','FAIL',false,r.error);return}
  row('2. Echo','OK',true,'ip='+(r.json||{}).client_ip);
  push('ua='+(r.json||{}).user_agent);
}
async function testFileHEAD(){
  push('');push('=== 3. Test file HEAD ===');
  const t0=performance.now();
  try{
    const r=await fetch('/api/test-file'+Q,{method:'HEAD'});
    const ms=Math.round(performance.now()-t0);
    const h={};r.headers.forEach((v,k)=>{h[k]=v});
    row('3. Test file HEAD',r.status+' '+ms+'ms',r.ok,
        (h['content-type']||'?')+' '+(h['content-length']||'?')+'B');
    push('status='+r.status+' ms='+ms);
  }catch(e){row('3. Test file HEAD','FAIL',false,String(e));}
}
async function testFileRange(){
  push('');push('=== 4. Test file Range ===');
  const t0=performance.now();
  try{
    const r=await fetch('/api/test-file'+Q,{headers:{Range:'bytes=0-1023'}});
    const buf=await r.arrayBuffer();
    const ms=Math.round(performance.now()-t0);
    const hex=Array.from(new Uint8Array(buf).slice(0,12)).map(b=>b.toString(16).padStart(2,'0')).join(' ');
    row('4. Test file Range',r.status+' '+ms+'ms',r.ok,buf.byteLength+'B head:'+hex);
    push('bytes='+buf.byteLength+' head='+hex);
  }catch(e){row('4. Test file Range','FAIL',false,String(e));}
}
async function testFilePlay(){
  push('');push('=== 5. Test file <video> ===');
  const res=await testVideo('/api/test-file'+Q);
  row('5. Test file <video>', res.ok?('OK dur='+res.duration.toFixed(2)+'s'):('FAIL '+res.err),
      res.ok, res.ok?('w='+res.w+' h='+res.h):('code='+res.code+' '+res.message));
  push('result='+JSON.stringify(res));
}
async function testFilePlayNoCSP(){
  push('');push('=== 6. Test file <video> (no CSP) ===');
  const res=await testVideo('/api/test-file-nocsp'+Q);
  row('6. Test file <video> nocsp', res.ok?'OK':('FAIL '+res.err), res.ok,
      res.ok?'':('code='+res.code+' '+res.message));
  push('result='+JSON.stringify(res));
}
async function testFilePlayPlain(){
  push('');push('=== 7. Test file <video> (minimal headers) ===');
  const res=await testVideo('/api/test-file-plain'+Q);
  row('7. Test file <video> minimal', res.ok?'OK':('FAIL '+res.err), res.ok,
      res.ok?'':('code='+res.code+' '+res.message));
  push('result='+JSON.stringify(res));
}
async function testMegaScan(url){
  push('');push('=== 8. MEGA scan ===');
  if(!url){row('8. MEGA scan','SKIP',null,'no url');return}
  const r=await timedJson('/api/mega-scan?url='+encodeURIComponent(url));
  if(r.error||!r.json||!r.json.ok){row('8. MEGA scan','FAIL',false,(r.json&&r.json.error)||r.error||'fail');return}
  const j=r.json;
  row('8. MEGA scan','OK',true,'count='+j.count+' first='+(j.first[0]?j.first[0].name:'?'));
  push('count='+j.count+' first='+JSON.stringify(j.first[0]||{}));
}
async function testMegaProbe(url){
  push('');push('=== 9. MEGA probe ===');
  if(!url){row('9. MEGA probe','SKIP',null,'no url');return}
  const r=await timedJson('/api/mega-probe?url='+encodeURIComponent(url)+'&idx=0');
  if(r.error||!r.json||!r.json.ok){row('9. MEGA probe','FAIL',false,(r.json&&r.json.error)||r.error||'fail');return}
  const j=r.json;
  row('9. MEGA probe','OK',j.got_bytes>0,'bytes='+j.got_bytes+' sniff='+j.sniff);
  push('got='+j.got_bytes+' sniff='+j.sniff+' head='+j.head_hex);
}
async function testMegaPlay(url){
  push('');push('=== 10. MEGA <video> ===');
  if(!url){row('10. MEGA <video>','SKIP',null,'no url');return}
  const u='/api/mega-stream?url='+encodeURIComponent(url)+'&idx=0';
  const res=await testVideo(u);
  row('10. MEGA <video>', res.ok?('OK dur='+res.duration.toFixed(2)+'s'):('FAIL '+res.err),
      res.ok, res.ok?('w='+res.w+' h='+res.h):('code='+res.code+' '+res.message));
  push('result='+JSON.stringify(res));
}

// --- v6_web preview test (the one that matters) -------------------------

async function v6Test(){
  const stream=$('#v6stream').value.trim();
  const st=$('#v6status');
  if(!stream){st.textContent='paste stream URL first';return;}
  st.textContent='playing…';
  push('');push('=== V6_web preview ===');
  push('stream_url = '+stream);

  const res=await testVideo(stream, 20000);
  $('#v6state').textContent=JSON.stringify(res,null,2);
  row('V6. v6_web <video>',
      res.ok?('OK dur='+res.duration.toFixed(2)+'s'):('FAIL '+res.err),
      res.ok,
      res.ok?('w='+res.w+' h='+res.h):
            ('code='+res.code+' msg='+(res.message||'')
             +' rs='+res.readyState+' ns='+res.networkState));
  push('result='+JSON.stringify(res));

  // derive thumb urls from stream url and test one
  try{
    const u=new URL(stream, location.href);
    const m=u.pathname.match(/^\/stream\/([0-9a-f]+)\/(\d+)/);
    if(m){
      u.pathname='/thumb/'+m[1]+'/'+m[2]+'/5';
      u.searchParams.set('n','10');
      const tu=u.toString();
      push('thumb_url = '+tu);
      const img=new Image();
      const ok=await new Promise(resolve=>{
        const t=setTimeout(()=>resolve({ok:false,err:'timeout'}),8000);
        img.onload=()=>{clearTimeout(t);resolve({ok:true,w:img.naturalWidth,h:img.naturalHeight})};
        img.onerror=()=>{clearTimeout(t);resolve({ok:false,err:'load error'})};
        img.src=tu;
      });
      row('V6. thumb <img>', ok.ok?('OK '+ok.w+'x'+ok.h):('FAIL '+ok.err), ok.ok,
          ok.ok?'':tu.slice(0,90));
      push('thumb_result='+JSON.stringify(ok));
    } else {
      push('could not derive thumb url from stream url');
    }
  }catch(e){ push('thumb derive failed: '+String(e)); }

  st.textContent='done';
}

async function runAll(){
  $('#run').disabled=true;
  reset();
  $('#sendcard').hidden=false;
  deviceInfo();
  push('=== Device ===');
  push('UA: '+navigator.userAgent);
  push('platform: '+navigator.platform+' dpr='+devicePixelRatio);
  const url=$('#url').value.trim();
  push('MEGA URL: '+(url||'(none)'));
  push('v6 stream: '+($('#v6stream').value.trim()||'(none)'));
  push('time: '+new Date().toISOString());

  await test1(); await test2();
  await testFileHEAD(); await testFileRange();
  await testFilePlay(); await testFilePlayNoCSP(); await testFilePlayPlain();
  await testMegaScan(url); await testMegaProbe(url); await testMegaPlay(url);
  if($('#v6stream').value.trim()) await v6Test();

  push('');push('=== Done ===');
  $('#run').disabled=false;
  $('#logStatus').textContent='ready — tap Copy or Send to PC';
}

async function sendReport(){
  const label=($('#label').value||'device').slice(0,60);
  const summary={
    ua:navigator.userAgent, platform:navigator.platform,
    dpr:devicePixelRatio, time:new Date().toISOString(),
    mega_url:$('#url').value.trim(),
    v6stream:$('#v6stream').value.trim(),
  };
  const body={label, report:$('#log').value, summary};
  $('#send').disabled=true;
  $('#sendstatus').textContent='sending…';
  try{
    const r=await fetch('/api/report',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify(body)});
    const j=await r.json();
    $('#sendstatus').textContent=j.ok?('sent! id='+j.id+' — check PC terminal'):('err: '+(j.error||'?'));
  }catch(e){$('#sendstatus').textContent='failed: '+String(e);}
  finally{$('#send').disabled=false;}
}

$('#run').onclick=runAll;
$('#v6test').onclick=v6Test;
$('#clear').onclick=async()=>{reset();await fetch('/api/log/clear');$('#logStatus').textContent='';};
$('#copy').onclick=async()=>{
  const t=$('#log').value;
  try{await navigator.clipboard.writeText(t);$('#logStatus').textContent='copied!';}
  catch(e){$('#log').select();document.execCommand('copy');$('#logStatus').textContent='copied';}
};
$('#send').onclick=sendReport;
$('#url').addEventListener('keydown',e=>{if(e.key==='Enter')runAll()});
deviceInfo();
</script></body></html>"""


def lan_ips():
    ips=[]
    try:
        s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
        s.connect(("10.255.255.255",1)); ips.append(s.getsockname()[0]); s.close()
    except Exception: pass
    try:
        for ai in socket.getaddrinfo(socket.gethostname(),None,socket.AF_INET):
            ip=ai[4][0]
            if not ip.startswith("127.") and ip not in ips: ips.append(ip)
    except Exception: pass
    return ips


def main():
    global PIN, NO_PIN, TEST_MP4
    ap=argparse.ArgumentParser(description="V6 diagnostics server.")
    ap.add_argument("--host",default="0.0.0.0")
    ap.add_argument("--port",type=int,default=8770)
    ap.add_argument("--pin",default=None)
    ap.add_argument("--no-pin",action="store_true")
    a=ap.parse_args()
    NO_PIN=a.no_pin
    PIN=str(a.pin) if a.pin else f"{secrets.randbelow(10**6):06d}"

    tmp=Path(tempfile.gettempdir())/"v6diag_test.mp4"
    if tmp.exists():
        try: tmp.unlink()
        except Exception: pass
    if make_test_mp4(tmp):
        TEST_MP4=tmp
        print(f"[diag] test mp4: {tmp} ({tmp.stat().st_size} bytes)")
    else:
        print("[diag] no test mp4 — ffmpeg not found")

    srv=ThreadingHTTPServer((a.host,a.port),H)
    srv.daemon_threads=True
    def _cleanup():
        try: srv.shutdown(); srv.server_close()
        except Exception: pass
        try:
            if TEST_MP4 and TEST_MP4.exists(): TEST_MP4.unlink()
        except Exception: pass
    atexit.register(_cleanup)
    threading.Thread(target=srv.serve_forever,kwargs={"poll_interval":0.25},daemon=True).start()

    print()
    print("="*64)
    print("  V6 DIAGNOSTICS  v1.2")
    print("="*64)
    print(f"  Diag (PC)     http://127.0.0.1:{a.port}/")
    for ip in lan_ips():
        print(f"  Diag (iPad)   http://{ip}:{a.port}/")
    print()
    print("  Reports dump to THIS terminal when the iPad taps 'Send to PC'.")
    print("  Keep this window visible while you test.")
    print()
    print("  Focus test: paste your v6_web stream URL into the 'Test v6_web'")
    print("  field on the iPad and tap 'Play v6_web stream'.")
    print()
    print("  Ctrl+C to stop.")
    print()
    try:
        while True: time.sleep(1)
    except KeyboardInterrupt: print()


if __name__=="__main__":
    main()