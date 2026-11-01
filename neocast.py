#!/usr/bin/env python3
"""NeoCast: stream movies, music, YouTube, live audio and your screen to an old
Apple TV (AirPlay 1, 720p/30fps) from a Windows PC or a Mac.

  python neocast.py portal            # web UI at http://localhost:8080
  python neocast.py video movie.mkv   # CLI; converts to 720p30 if needed
"""
import argparse, asyncio, functools, hashlib, http.client, json, logging, mimetypes, re, shutil, socket, subprocess, sys
import threading, time, uuid, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("neocast")

import pyatv
from pyatv.const import Protocol

WIN, MAC = sys.platform == "win32", sys.platform == "darwin"
if WIN:  # pyatv/aiohttp want the selector loop on Windows
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

WANTED = {Protocol.AirPlay, Protocol.RAOP}   # never DMAP, it crashes on Apple TV 2
HOME = Path.home() / "NeoCast"
CACHE, CFG_FILE = HOME / "cache", HOME / "settings.json"
UI_PORT, MEDIA_PORT, AIRPLAY_PORT = 8080, 8787, 7000
VIDEO = {".mp4", ".m4v", ".mkv", ".avi", ".mov", ".webm", ".ts", ".wmv", ".flv", ".mpg"}
AUDIO = {".mp3", ".m4a", ".flac", ".wav", ".ogg", ".aac"}
MIME = {".mp4": "video/mp4", ".m3u8": "application/vnd.apple.mpegurl", ".ts": "video/mp2t"}

# host: your Apple TV's IP (blank = auto-discover)
cfg = {"host": "192.168.1.108", "library": [str(Path.home() / ("Videos" if WIN else "Movies")),
                                            str(Path.home() / "Music")]}
state = {"status": "idle", "now": None, "error": None, "device": None,
         "paused": False, "position": 0, "duration": 0, "resume_verified": False}
SESSION = str(uuid.uuid4())   # X-Apple-Session-ID, one per process
TOKENS, HITS, INDEX, JOBS = {}, {}, {}, []   # token->path, token->fetch count, id->path
LOOP = task = None
CFG_LOCK = threading.RLock()
XLOCK = threading.Lock()   # one ffmpeg conversion at a time


def load_cfg():
    try:
        cfg.update(json.loads(CFG_FILE.read_text()))
    except (OSError, ValueError):
        pass


def save_cfg():
    CACHE.mkdir(parents=True, exist_ok=True)
    with CFG_LOCK:
        tmp = CFG_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, indent=2))
        tmp.replace(CFG_FILE)


def need(tool):
    if not shutil.which(tool):
        hint = "winget install ffmpeg" if WIN else "brew install ffmpeg"
        raise RuntimeError(f"{tool} not found on PATH ({hint})")


def lan_ip(target):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((target, 9))
        return s.getsockname()[0]
    finally:
        s.close()


# ---------------- media server (Range support) ----------------
def parse_range(header, size):
    """-> (start, end, status). 200 = whole file, 206 = partial, 416 = unsatisfiable."""
    start, end, code = 0, size - 1, 200
    m = re.match(r"bytes=(\d*)-(\d*)$", header or "")
    if m:
        a, b = m.groups()
        if a:
            start = int(a)
            if b:
                end = min(int(b), size - 1)
        elif b:
            start = max(size - int(b), 0)
        code = 206
    return start, end, (416 if start > end else code)


class Media(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _serve(self, head):
        m = re.match(r"/m/([a-f0-9]+)/([^/?]*)", self.path)
        root = TOKENS.get(m.group(1)) if m else None
        path = (root / Path(m.group(2)).name) if root and root.is_dir() else root
        if not path or not path.is_file():
            return self.send_error(404)
        size = path.stat().st_size
        start, end, code = parse_range(self.headers.get("Range"), size)
        if code == 416:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            return self.end_headers()
        self.send_response(code)
        self.send_header("Content-Type", MIME.get(path.suffix.lower())
                         or mimetypes.guess_type(path.name)[0] or "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if head:
            return
        HITS[m.group(1)] = HITS.get(m.group(1), 0) + 1
        try:
            with open(path, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(262144, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def do_GET(self): self._serve(False)
    def do_HEAD(self): self._serve(True)


def register(path):
    tok = uuid.uuid4().hex[:10]
    TOKENS[tok] = Path(path)
    return tok


def start_media_server():
    try:
        srv = ThreadingHTTPServer(("0.0.0.0", MEDIA_PORT), Media)
    except OSError as e:
        sys.exit(f"Media port {MEDIA_PORT} unavailable ({e}). Use --port.")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    if WIN:
        print(f"Windows firewall (admin shell, once): netsh advfirewall firewall add rule "
              f"name=NeoCast dir=in action=allow protocol=TCP localport={MEDIA_PORT} profile=private")
    return srv


# ---------------- probe / convert (Apple TV 2: H.264 Main L3.1, 720p, 30fps, AAC) ----------------
PROBE_CACHE = {}


def probe(path):
    need("ffprobe")
    p = Path(path).resolve()
    try:
        mtime = p.stat().st_mtime_ns
    except OSError:
        mtime = 0
    if p in PROBE_CACHE and PROBE_CACHE[p][0] == mtime:
        return PROBE_CACHE[p][1]
    raw = subprocess.check_output(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format",
         str(p)])
    data = json.loads(raw)
    PROBE_CACHE[p] = (mtime, data)
    return data


def fps_of(v):
    try:
        n, d = v.get("avg_frame_rate", "0/1").split("/")
        return float(n) / float(d) if float(d) else 0.0
    except (ValueError, ZeroDivisionError, AttributeError):
        return 0.0


def check_compatibility(info, sub=None, audio=0):
    """
    Apple TV 2 hardware playback specifications:
      - Video: H.264 (Main, Baseline, or Constrained Baseline profile, level <= 3.1,
               max 1280x720, max 30 fps)
      - Audio: AAC stereo (selected track)
      - Container: MP4/M4V/MOV
      - Subtitles: burn-in requires video transcoding

    Returns a comprehensive dict:
      mode: 'direct' | 'remux' | 'audio_transcode' | 'video_transcode'
      reasons: list of reasons why conversion or remuxing is required
      video_reencode: bool
      audio_reencode: bool
      source_text: str (e.g. "536p · 24 fps · HEVC")
      output_text: str (e.g. "536p · 24 fps · H.264 / AAC")
      conversion_text: str (e.g. "HEVC → H.264")
      summary: str
      source_container: str
      source_acodec: str
      out_width: int
      out_height: int
      out_fps: float
    """
    v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    audios = [s for s in info.get("streams", []) if s.get("codec_type") == "audio"]
    fmt = info.get("format", {})
    fmt_name = fmt.get("format_name", "").lower()
    container_name = fmt_name.split(",")[0].upper() if fmt_name else "UNKNOWN"

    if not v:
        return {
            "mode": "direct", "reasons": [], "video_reencode": False, "audio_reencode": False,
            "source_text": "Audio only", "output_text": "AirPlay Audio", "conversion_text": "None",
            "summary": "Audio only", "source_container": container_name, "source_acodec": "",
            "out_width": 0, "out_height": 0, "out_fps": 0.0, "width": 0, "height": 0,
            "fps": 0.0, "vcodec": "", "acodec": ""
        }

    vcodec = v.get("codec_name", "").lower()
    profile = v.get("profile", "")
    level_raw = v.get("level")
    level = None
    if level_raw is not None:
        try:
            level = int(level_raw)
        except (ValueError, TypeError):
            pass
    w = int(v.get("width") or 0)
    h = int(v.get("height") or 0)
    fps = fps_of(v)

    video_reasons = []
    if vcodec not in ("h264", "avc1"):
        video_reasons.append(f"video codec ({vcodec.upper()} is not supported; requires H.264)")
    if profile and profile not in ("Main", "Baseline", "Constrained Baseline"):
        video_reasons.append(f"H.264 profile ({profile} is not supported; requires Main or Baseline)")
    if level is not None and level > 31:
        lvl_str = f"{level/10:.1f}" if level > 10 else f"{level}"
        video_reasons.append(f"H.264 level ({lvl_str} exceeds Apple TV 2 limit 3.1)")
    if w > 1280 or h > 720:
        video_reasons.append(f"dimensions ({w}x{h} exceeds 1280x720 limit)")
    if fps > 30.5:
        video_reasons.append(f"frame rate ({round(fps, 1)} fps exceeds 30 fps limit)")
    if sub is not None:
        video_reasons.append("subtitle burn-in requested")

    selected_audio = audios[audio] if (audios and 0 <= audio < len(audios)) else (audios[0] if audios else None)
    acodec = selected_audio.get("codec_name", "").lower() if selected_audio else ""
    audio_reasons = []
    if selected_audio:
        if acodec not in ("aac", "mp4a"):
            audio_reasons.append(f"audio codec ({acodec.upper() if acodec else 'unknown'} requires AAC)")

    container_reasons = []
    is_mp4 = any(ext in fmt_name for ext in ("mp4", "m4v", "mov"))
    if not is_mp4:
        container_reasons.append(f"container ({container_name} is not MP4)")

    if video_reasons:
        mode = "video_transcode"
        video_reencode = True
        audio_reencode = True
        reasons = video_reasons + container_reasons + audio_reasons
    elif audio_reasons:
        mode = "audio_transcode"
        video_reencode = False
        audio_reencode = True
        reasons = audio_reasons + container_reasons
    elif container_reasons:
        mode = "remux"
        video_reencode = False
        audio_reencode = False
        reasons = container_reasons
    else:
        mode = "direct"
        video_reencode = False
        audio_reencode = False
        reasons = []

    # Calculate output dimensions: preserve source resolution when fitting inside 1280x720
    if w > 0 and h > 0:
        if w > 1280 or h > 720:
            factor = min(1280.0 / w, 720.0 / h)
            out_w = int(round(w * factor / 2.0)) * 2
            out_h = int(round(h * factor / 2.0)) * 2
        else:
            out_w = int(w / 2) * 2
            out_h = int(h / 2) * 2
    else:
        out_w = min(w, 1280) if w else 1280
        out_h = min(h, 720) if h else 720

    out_fps = 30.0 if fps > 30.5 else fps

    # Build source details string
    fps_label = f"{round(fps)} fps" if fps else ""
    src_parts = [f"{h}p" if h else "", fps_label, vcodec.upper() if vcodec else ""]
    src_core = " · ".join([p for p in src_parts if p])
    if acodec:
        src_core += f" / {acodec.upper()}"
    if not is_mp4:
        src_core += f" ({container_name})"
    source_text = src_core

    # Build prepared-output details string
    out_fps_label = f"{round(out_fps)} fps" if out_fps else ""
    out_parts = [f"{out_h}p" if out_h else "", out_fps_label, "H.264 / AAC"]
    output_text = " · ".join([p for p in out_parts if p])

    # Build specific conversion required string
    if mode == "direct":
        conversion_text = "None (plays directly)"
    elif mode == "remux":
        conversion_text = f"Container remux: {container_name} → MP4 (no video re-encoding)"
    elif mode == "audio_transcode":
        conversion_text = f"Audio conversion: {acodec.upper()} → AAC (video stream copied)"
    else:
        c_parts = []
        if vcodec not in ("h264", "avc1"):
            c_parts.append(f"{vcodec.upper()} → H.264")
        elif profile and profile not in ("Main", "Baseline", "Constrained Baseline"):
            c_parts.append(f"{profile} → Main profile")
        elif level is not None and level > 31:
            lvl_val = level / 10.0 if level > 10 else float(level)
            c_parts.append(f"Level {lvl_val:.1f} → 3.1")
        if w > 1280 or h > 720:
            c_parts.append(f"{w}x{h} → {out_w}x{out_h}")
        if fps > 30.5:
            c_parts.append(f"{round(fps)} fps → 30 fps")
        if sub is not None:
            c_parts.append("Burn subtitles")
        if not c_parts:
            c_parts.append(f"{vcodec.upper()} → H.264")
        conversion_text = " · ".join(c_parts)

    if mode == "direct":
        summary = f"Source: {source_text} · Plays directly"
    else:
        summary = f"Source: {source_text}\nConversion required: {conversion_text}\nOutput: {output_text}"

    return {
        "mode": mode,
        "reasons": reasons,
        "video_reencode": video_reencode,
        "audio_reencode": audio_reencode,
        "source_text": source_text,
        "output_text": output_text,
        "conversion_text": conversion_text,
        "summary": summary,
        "source_container": container_name,
        "source_acodec": acodec.upper() if acodec else "",
        "out_width": out_w,
        "out_height": out_h,
        "out_fps": out_fps,
        "width": w,
        "height": h,
        "fps": fps,
        "vcodec": vcodec,
        "acodec": acodec
    }


def is_compatible(info):
    return check_compatibility(info)["mode"] == "direct"


def get_cache_path(src, sub=None, audio=0):
    src = Path(src).resolve()
    try:
        mtime = src.stat().st_mtime_ns
    except OSError:
        mtime = 0
    key = f"{src}|{mtime}|s{sub}|a{audio}"
    return CACHE / (hashlib.md5(key.encode()).hexdigest()[:12] + ".mp4")


def is_cached(src, sub=None, audio=0):
    try:
        p = get_cache_path(src, sub, audio)
        return p.exists() and p.stat().st_size > 0
    except OSError:
        return False


@functools.lru_cache(maxsize=1)
def has_libass():
    try:
        return " subtitles " in subprocess.run(["ffmpeg", "-hide_banner", "-filters"],
                                               capture_output=True, text=True).stdout
    except OSError:
        return False


def sub_file(src, track):
    """track -1 = sidecar .srt next to the file, N = embedded text track N.
    Returns a cache-relative name (the filter runs with cwd=CACHE, so no path escaping)."""
    if not has_libass():
        raise RuntimeError("this ffmpeg has no 'subtitles' filter (libass). Windows: use the "
                           "gyan.dev 'full' build; Mac: brew install ffmpeg-full")
    dst = CACHE / ("sub_" + hashlib.md5(f"{src}|{track}".encode()).hexdigest()[:10] + ".srt")
    if track < 0:
        shutil.copyfile(src.with_suffix(".srt"), dst)
    else:
        r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
                            "-map", f"0:s:{track}", str(dst)], capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError("no text subtitle track to burn in: " + r.stderr.strip()[-120:])
    return dst.name


def run_preparation(src, info, analysis, out, job, sub=None, audio=0):
    need("ffmpeg")
    mode = analysis["mode"]
    dur = float(info.get("format", {}).get("duration") or 0)
    v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    tmp = out.with_suffix(".part.mp4")
    tmp.unlink(missing_ok=True)

    if mode == "remux":
        label = "Remuxing container"
        job.update(stage="converting", label=label, pct=0)
        logger.info(f"[prepare] Remuxing container for '{src.name}' -> {out.name}")
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-progress", "pipe:1", "-nostats",
               "-i", str(src), "-map", "0:v:0", "-map", f"0:a:{audio}?",
               "-c:v", "copy", "-c:a", "copy", "-movflags", "+faststart",
               "-f", "mp4", str(tmp)]
    elif mode == "audio_transcode":
        label = "Converting audio"
        job.update(stage="converting", label=label, pct=0)
        logger.info(f"[prepare] Converting audio ({analysis['source_acodec']} -> AAC) for '{src.name}' -> {out.name}")
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-progress", "pipe:1", "-nostats",
               "-i", str(src), "-map", "0:v:0", "-map", f"0:a:{audio}?",
               "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-ac", "2",
               "-movflags", "+faststart", "-f", "mp4", str(tmp)]
    else:  # video_transcode
        label = "Converting video"
        job.update(stage="converting", label=label, pct=0)
        logger.info(f"[prepare] Transcoding video ({analysis['conversion_text']}) for '{src.name}' -> {out.name}")

        w, h = analysis["width"], analysis["height"]
        if w > 1280 or h > 720:
            vf = "scale='min(1280,iw)':'min(720,ih)':force_original_aspect_ratio=decrease,scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p"
        elif w % 2 != 0 or h % 2 != 0:
            vf = "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p"
        else:
            vf = "format=yuv420p"

        if sub is not None:
            vf = f"subtitles={sub_file(src, sub)}," + vf

        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-progress", "pipe:1", "-nostats",
               "-i", str(src), "-map", "0:v:0", "-map", f"0:a:{audio}?", "-vf", vf]

        if fps_of(v) > 30.5:
            cmd += ["-r", "30"]

        cmd += ["-c:v", "libx264", "-profile:v", "main", "-level", "3.1", "-preset", "veryfast",
                "-crf", "22", "-maxrate", "4M", "-bufsize", "6M",
                "-c:a", "aac", "-b:a", "160k", "-ac", "2", "-movflags", "+faststart",
                "-f", "mp4", str(tmp)]

    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=CACHE,
                         text=True, encoding="utf-8", errors="replace")
    tail = []
    for line in p.stdout:
        if line.startswith("out_time_ms=") and dur > 0:
            try:
                job["pct"] = min(99, int(line.split("=")[1]) / 1e6 / dur * 100)
            except ValueError:
                pass
        elif "=" not in line:
            tail = (tail + [line.strip()])[-3:]

    if p.wait() != 0:
        tmp.unlink(missing_ok=True)
        err = f"ffmpeg conversion failed: {' '.join(tail)}"
        logger.error(f"[prepare] {err}")
        raise RuntimeError(err)

    job["pct"] = 100
    tmp.replace(out)
    logger.info(f"[prepare] Conversion complete: {out.name} ({out.stat().st_size} bytes)")
    return out


def prepare(src, job=None, sub=None, audio=0):
    """Return a path the Apple TV can play, converting (and caching) if needed.
    sub = embedded subtitle track to burn in (None = off); audio = audio track index."""
    job = {} if job is None else job
    src = Path(src).expanduser().resolve()
    CACHE.mkdir(parents=True, exist_ok=True)
    if src.suffix.lower() in AUDIO:
        return src
    job["stage"] = "checking"
    info = probe(src)
    analysis = check_compatibility(info, sub, audio)

    if analysis["mode"] == "direct":
        logger.info(f"[prepare] Compatibility check passed for '{src.name}': direct playback")
        return src
    else:
        logger.info(f"[prepare] Compatibility check requires {analysis['mode']} for '{src.name}'. Reasons: {', '.join(analysis['reasons'])}")

    cached_out = get_cache_path(src, sub, audio)
    if cached_out.exists() and cached_out.stat().st_size > 0:
        logger.info(f"[prepare] Preparation mode selected: {analysis['mode']}. Reusing cached file: {cached_out.name}")
        return cached_out

    with XLOCK:
        if cached_out.exists() and cached_out.stat().st_size > 0:
            logger.info(f"[prepare] Preparation mode selected: {analysis['mode']}. Reusing cached file (post-lock): {cached_out.name}")
            return cached_out
        logger.info(f"[prepare] Preparation mode selected: {analysis['mode']}. Generating new file: {cached_out.name}")
        return run_preparation(src, info, analysis, cached_out, job, sub, audio)


def download(url, job):
    job.update(stage="downloading", pct=0)
    base = CACHE / f"yt_{uuid.uuid4().hex[:8]}"
    p = subprocess.Popen(
        [sys.executable, "-m", "yt_dlp", "-f",
         "bv*[height<=720][vcodec^=avc1]+ba[acodec^=mp4a]/b[height<=720]/b",
         "--merge-output-format", "mp4", "--newline", "--no-playlist",
         "-o", str(base) + ".%(ext)s", url],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace")
    tail = ""
    for line in p.stdout:
        m = re.search(r"\[download\]\s+([\d.]+)%", line)
        if m:
            job["pct"] = float(m.group(1))
        tail = line.strip() or tail
    if p.wait() != 0:
        raise RuntimeError(f"yt-dlp failed ({tail}). Try: pip install -U yt-dlp")
    job["pct"] = 0
    return next(CACHE.glob(base.name + ".*"))


# ---------------- capture (live audio / screen) ----------------
def audio_cmd(dev):
    if MAC:
        src = ["-f", "avfoundation", "-i", f":{dev or 'BlackHole 2ch'}"]
    elif WIN:
        src = ["-f", "dshow", "-i", f"audio={dev or 'CABLE Output (VB-Audio Virtual Cable)'}"]
    else:
        raise RuntimeError("live audio is supported on macOS and Windows only")
    return ["ffmpeg", "-loglevel", "error", *src, "-ac", "2", "-ar", "44100", "-f", "wav", "-"]


def screen_cmd(dev, out):
    if MAC:   # `devices` lists screens; "Capture screen 0" is usually index 1
        src = ["-f", "avfoundation", "-framerate", "30", "-capture_cursor", "1",
               "-i", f"{dev or '1'}:none"]
    elif WIN:
        src = ["-f", "gdigrab", "-framerate", "30", "-i", "desktop"]
    else:
        raise RuntimeError("screen sharing is supported on macOS and Windows only")
    return ["ffmpeg", "-y", "-loglevel", "error", *src,
            "-vf", "scale=-2:720,format=yuv420p", "-r", "30",
            "-c:v", "libx264", "-profile:v", "main", "-level", "3.1", "-preset", "ultrafast",
            "-tune", "zerolatency", "-g", "60", "-b:v", "3M", "-maxrate", "3M", "-bufsize", "6M",
            "-f", "hls", "-hls_time", "2", "-hls_list_size", "6",
            "-hls_flags", "delete_segments+independent_segments", str(out / "live.m3u8")]


def list_devices():
    cmd = (["-f", "avfoundation", "-list_devices", "true", "-i", ""] if MAC
           else ["-list_devices", "true", "-f", "dshow", "-i", "dummy"])
    need("ffmpeg")
    subprocess.run(["ffmpeg", *cmd])


# ---------------- Apple TV ----------------
async def discover(loop, timeout=5):
    """AirPlay devices on the LAN. Skips this machine's own AirPlay receiver."""
    found = await pyatv.scan(loop, timeout=timeout, protocol=WANTED)
    return [d for d in found if lan_ip(str(d.address)) != str(d.address)]


async def find(loop, host):
    if host:   # direct unicast lookup: fast and reliable
        found = await pyatv.scan(loop, timeout=6, protocol=WANTED, hosts=[host])
    else:
        found = await discover(loop, 6)
    if not found:
        raise RuntimeError("Apple TV not found. Same network? Check the IP in Settings, "
                           "or run: python neocast.py scan")
    return found[0]


async def run_on_tv(title, work):
    """Connect, run work(atv, my_ip), keep `state` honest. Never raises (except cancel)."""
    state.update(status="connecting", now=title, error=None, paused=False, position=0, duration=0)
    atv = None
    try:
        loop = asyncio.get_running_loop()
        conf = await find(loop, cfg["host"])
        state["device"] = str(conf.address)
        atv = await pyatv.connect(conf, loop)
        await work(atv, lan_ip(str(conf.address)))
        state.update(status="idle", now=None)
    except asyncio.CancelledError:
        state.update(status="idle", now=None)
        raise
    except Exception as e:
        state.update(status="error", error=str(e) or type(e).__name__)
    finally:
        if atv:
            atv.close()


def airplay_request(host, method, path, body=None, timeout=10):
    """Raw AirPlay 1 call on port 7000 (as djaysan/airplay-drop does). -> (status, text)."""
    c = http.client.HTTPConnection(host, AIRPLAY_PORT, timeout=timeout)
    try:
        h = {"User-Agent": "MediaControl/1.0", "X-Apple-Session-ID": SESSION}
        if body:
            h["Content-Type"] = "text/parameters"
        c.request(method, path, body, h)
        r = c.getresponse()
        return r.status, r.read().decode("utf-8", "replace")
    finally:
        c.close()


def parse_scrub(text):
    v = dict(re.findall(r"(\w+): ([\d.]+)", text))
    return float(v.get("position", 0)), float(v.get("duration", 0))


def control(method, path, timeout=4):
    host = state["device"]
    if not host or state["status"] != "playing":
        raise RuntimeError("nothing is playing")
    try:
        code, text = airplay_request(host, method, path, timeout=timeout)
    except (OSError, http.client.HTTPException) as e:
        raise RuntimeError(f"Apple TV unreachable: {e}")
    if code != 200:
        raise RuntimeError(f"Apple TV refused {path.split('?')[0]} (HTTP {code}); experimental")
    return text


def pause_toggle():
    control("POST", f"/rate?value={1 if state['paused'] else 0}.000000")
    state["paused"] = not state["paused"]


def seek(position=None, delta=None):
    if position is None:
        position = parse_scrub(control("GET", "/scrub"))[0] + delta
    control("POST", f"/scrub?position={max(position, 0):.3f}")


async def play_media(atv, url, tok, key=None, start=0, live=False):
    """Start media on Apple TV, verify initial playback position, issue and verify
    seeking with bounded retries if needed, and poll scrub status for UI and resume."""
    host, loop = state['device'], asyncio.get_running_loop()
    start = max(0.0, float(start or 0))
    logger.info(f"[play_media] Requested start position: {start:.3f}s (units: seconds) for key='{key}'")
    t = asyncio.ensure_future(atv.stream.play_url(url, position=0))
    pos = dur = 0.0
    reliable = False
    natural_end = False

    async def request(method, path):
        return await loop.run_in_executor(None, lambda: airplay_request(host, method, path, timeout=3))

    def record(p, d):
        nonlocal pos, dur, reliable
        pos, dur, reliable = p, d, True
        state.update(position=p, duration=d)
        if key and not live:
            set_saved_pos(key, p, d)

    try:
        for _ in range(60):
            if HITS.get(tok) or t.done():
                break
            await asyncio.sleep(0.25)
        if not HITS.get(tok):
            if t.done(): t.result()
            raise RuntimeError(f'Apple TV never fetched media. Check TCP {MEDIA_PORT} and the network.')
        state.update(status='connecting' if not live else 'playing', paused=False,
                     resume_target=start, resume_verified=False)
        if not live:
            deadline = loop.time() + 35
            last_seek = 0.0
            seek_sent = False
            verified = False
            seek_attempts = 0
            while loop.time() < deadline and not t.done():
                try:
                    code, text = await request('GET', '/scrub')
                    p, d = parse_scrub(text)
                    if code == 200 and d > 0:
                        target = min(start, max(0, d - 1))
                        if seek_attempts == 0 and not seek_sent:
                            logger.info(f"[play_media] Initial playback reading: position={p:.3f}s, duration={d:.3f}s (requested={start:.3f}s)")
                        if (start == 0 or seek_sent) and abs(p - target) <= 8:
                            record(p, d)
                            verified = True
                            break
                        if loop.time() - last_seek >= 4:
                            seek_attempts += 1
                            code, _ = await request('POST', f'/scrub?position={target:.3f}')
                            logger.info(f"[play_media] Seek attempt #{seek_attempts} to {target:.3f}s (HTTP {code}); observed {p:.3f}s")
                            seek_sent = code == 200
                            last_seek = loop.time()
                except (OSError, http.client.HTTPException):
                    pass
                await asyncio.sleep(1)
            if not verified:
                try: await request('POST', '/stop')
                except Exception: pass
                err_msg = f"Apple TV refused seek to {fmt_time(start)} ({start:.0f}s) or position could not be verified. Saved resume point was preserved."
                logger.error(f"[play_media] {err_msg}")
                raise RuntimeError(err_msg)
            state.update(status='playing', resume_verified=True)
            logger.info(f"[play_media] Verified playback position: {pos:.3f}s (requested={start:.3f}s)")
        polls = 0
        while not t.done():
            await asyncio.sleep(2)
            try:
                code, text = await request('GET', '/scrub')
            except (OSError, http.client.HTTPException):
                continue
            p, d = parse_scrub(text)
            if code == 200 and d > 0:
                if p == 0.0 and start > 30 and pos > 30:
                    logger.info(f"[play_media] Reading of 0.0s during playback ignored; keeping {pos:.1f}s")
                    continue
                if p > 0 or not reliable or pos < 8:
                    record(p, d)
                    polls += 1
                    if key and not live and polls % 3 == 0:
                        save_cfg()
        await t
        natural_end = True
    finally:
        t.cancel()
        await asyncio.gather(t, return_exceptions=True)
        if key and not live:
            if reliable and natural_end and pos >= dur - 15:
                logger.info(f"[play_media] Playback watched to end ({pos:.1f}/{dur:.1f}s). Clearing saved position for '{key}'")
                clear_saved_pos(key)
            elif reliable and pos > 0:
                logger.info(f"[play_media] Playback ended. Saving reliable position for '{key}': {pos:.1f}/{dur:.1f}s")
                set_saved_pos(key, pos, dur)
            else:
                logger.info(f"[play_media] Playback session ended without reliable progress ({pos:.1f}s); intentionally not overwriting saved resume point for '{key}'")
            save_cfg()


def file_work(path, key=None, start=0):
    async def w(atv, ip):
        if Path(path).suffix.lower() in AUDIO:
            state["status"] = "playing"
            return await atv.stream.stream_file(str(path))
        state["cur"] = add_index(Path(key)) if key else None
        tok = register(path)
        await play_media(atv, f"http://{ip}:{MEDIA_PORT}/m/{tok}/video.mp4", tok, key, start)
    return w


def live_work(dev):
    async def w(atv, ip):
        need("ffmpeg")
        ff = subprocess.Popen(audio_cmd(dev), stdout=subprocess.PIPE)
        try:
            state["status"] = "playing"
            await atv.stream.stream_file(ff.stdout)
        finally:
            ff.terminate()
    return w


def screen_work(dev):
    async def w(atv, ip):
        need("ffmpeg")
        out = CACHE / "live" / uuid.uuid4().hex[:8]
        out.mkdir(parents=True)
        log = open(out / "ff.log", "wb")
        ff = subprocess.Popen(screen_cmd(dev, out), stdin=subprocess.DEVNULL, stderr=log)
        try:
            for _ in range(60):
                if ff.poll() is not None:
                    raise RuntimeError("screen capture failed: " +
                                       (out / "ff.log").read_text(errors="replace")[-300:])
                if len(list(out.glob("*.ts"))) >= 2:
                    break
                await asyncio.sleep(0.5)
            tok = register(out)
            await play_media(atv, f"http://{ip}:{MEDIA_PORT}/m/{tok}/live.m3u8", tok, live=True)
        finally:
            ff.terminate()
            try:
                ff.wait(5)
            except subprocess.TimeoutExpired:
                ff.kill()
            log.close()
            shutil.rmtree(out, ignore_errors=True)
    return w


# ---------------- CLI ----------------
def cli_run(title, work):
    asyncio.run(run_on_tv(title, work))
    if state["status"] == "error":
        sys.exit(state["error"])


def add_index(p):
    i = hashlib.md5(str(p).encode()).hexdigest()[:10]
    INDEX[i] = p
    return i


def pick_file():
    """Native file dialog on the server side (the browser can't reveal file paths)."""
    if MAC:
        r = subprocess.run(["osascript", "-e", 'POSIX path of (choose file with prompt "Select a video")'],
                           capture_output=True, text=True)
    else:   # Windows (tkinter ships with python.org builds); subprocess keeps Tk off our threads
        code = ("import tkinter as t, tkinter.filedialog as f; r = t.Tk(); r.withdraw(); "
                "r.attributes('-topmost', True); print(f.askopenfilename())")
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    return r.stdout.strip()


def scan_library():
    INDEX.clear()
    for root in cfg["library"]:
        root = Path(root).expanduser()
        try:
            for p in root.rglob("*") if root.exists() else ():
                if p.suffix.lower() in VIDEO | AUDIO and p.is_file():
                    add_index(p)
        except OSError:
            pass
    for r in cfg.get("recent", []):   # files played from outside the library folders
        if Path(r["path"]).is_file():
            add_index(Path(r["path"]))


def selftest():
    assert parse_range(None, 100) == (0, 99, 200)
    assert parse_range("bytes=10-", 100) == (10, 99, 206)
    assert parse_range("bytes=10-19", 100) == (10, 19, 206)
    assert parse_range("bytes=-5", 100) == (95, 99, 206)
    assert parse_range("bytes=200-", 100)[2] == 416
    ok = {"format": {"format_name": "mov,mp4,m4a"}, "streams": [
        {"codec_type": "video", "codec_name": "h264", "profile": "Main", "level": 31,
         "width": 1280, "height": 720, "avg_frame_rate": "30000/1001"},
        {"codec_type": "audio", "codec_name": "aac"}]}
    assert is_compatible(ok)
    assert check_compatibility(ok)["mode"] == "direct"

    # Incompatible height
    ok_1080 = {"format": {"format_name": "mov,mp4,m4a"}, "streams": [
        {"codec_type": "video", "codec_name": "h264", "profile": "Main", "level": 31,
         "width": 1920, "height": 1080, "avg_frame_rate": "24/1"},
        {"codec_type": "audio", "codec_name": "aac"}]}
    c_1080 = check_compatibility(ok_1080)
    assert not is_compatible(ok_1080)
    assert c_1080["mode"] == "video_transcode"
    assert c_1080["out_width"] == 1280 and c_1080["out_height"] == 720
    assert c_1080["out_fps"] == 24.0

    # Incompatible fps
    ok_60fps = {"format": {"format_name": "mov,mp4,m4a"}, "streams": [
        {"codec_type": "video", "codec_name": "h264", "profile": "Main", "level": 31,
         "width": 1280, "height": 720, "avg_frame_rate": "60/1"},
        {"codec_type": "audio", "codec_name": "aac"}]}
    assert not is_compatible(ok_60fps)
    assert check_compatibility(ok_60fps)["mode"] == "video_transcode"
    assert check_compatibility(ok_60fps)["out_fps"] == 30.0

    # 536p HEVC: video transcode, preserves 536p and 24fps
    hevc_536 = {"format": {"format_name": "mov,mp4,m4a"}, "streams": [
        {"codec_type": "video", "codec_name": "hevc", "profile": "Main",
         "width": 1280, "height": 536, "avg_frame_rate": "24/1"},
        {"codec_type": "audio", "codec_name": "aac"}]}
    c_536 = check_compatibility(hevc_536)
    assert c_536["mode"] == "video_transcode"
    assert c_536["out_height"] == 536 and c_536["out_fps"] == 24.0
    assert "HEVC → H.264" in c_536["conversion_text"]
    assert "536p" in c_536["output_text"]

    # Remux: MKV container with compatible H.264 and AAC
    mkv_ok = {"format": {"format_name": "matroska,webm"}, "streams": [
        {"codec_type": "video", "codec_name": "h264", "profile": "Main", "level": 31,
         "width": 1280, "height": 720, "avg_frame_rate": "24/1"},
        {"codec_type": "audio", "codec_name": "aac"}]}
    c_mkv = check_compatibility(mkv_ok)
    assert c_mkv["mode"] == "remux"
    assert not c_mkv["video_reencode"] and not c_mkv["audio_reencode"]

    # Audio transcode: AC3 audio with compatible H.264
    ac3_ok = {"format": {"format_name": "mov,mp4,m4a"}, "streams": [
        {"codec_type": "video", "codec_name": "h264", "profile": "Main", "level": 31,
         "width": 1280, "height": 720, "avg_frame_rate": "24/1"},
        {"codec_type": "audio", "codec_name": "ac3"}]}
    c_ac3 = check_compatibility(ac3_ok)
    assert c_ac3["mode"] == "audio_transcode"
    assert not c_ac3["video_reencode"] and c_ac3["audio_reencode"]

    print("selftest ok")


def portal():
    import uvicorn
    from contextlib import asynccontextmanager
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import FileResponse, HTMLResponse

    @asynccontextmanager
    async def life(_):
        global LOOP
        LOOP = asyncio.get_running_loop()
        yield

    app = FastAPI(lifespan=life)

    async def start(title, work):
        global task
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        task = asyncio.create_task(run_on_tv(title, work))

    def run_job(title, prep, mk_work):
        job = {"title": title, "stage": "queued", "pct": 0, "error": None}
        JOBS.append(job)
        del JOBS[:-20]

        def go():
            try:
                work = mk_work(prep(job))
                job["stage"] = "starting"
                asyncio.run_coroutine_threadsafe(start(title, work), LOOP).result(20)
                job["stage"] = "done"
            except Exception as e:
                job.update(stage="error", error=str(e))
        threading.Thread(target=go, daemon=True).start()
        return {"ok": True}

    @app.get("/", response_class=HTMLResponse)
    def home():
        return (Path(__file__).parent / "ui.html").read_text(encoding="utf-8")

    @app.get("/api/library")
    def library():
        if not INDEX:
            scan_library()
        return [{"id": i, "name": p.name, "mb": round(p.stat().st_size / 1048576),
                 "audio": p.suffix.lower() in AUDIO, "resume": int(get_saved_pos(p)[0])}
                for i, p in sorted(INDEX.items(), key=lambda kv: kv[1].name.lower())]

    @app.post("/api/rescan")
    def rescan():
        scan_library()
        return {"ok": True, "count": len(INDEX)}

    @app.post("/api/play")
    def play(body: dict):
        fid = body.get("id")
        p = INDEX.get(fid)
        if not p:
            return {"ok": False, "error": "unknown file"}
        sub = body.get("sub")   # None = off, -1 = sidecar .srt, N = embedded track
        
        if body.get("restart"):
            start = 0.0
            set_saved_pos(p, 0.0, get_saved_pos(p)[1])
            save_cfg()
            logger.info(f"[/api/play] Restart requested for '{p.name}'. Starting at 0.0s")
        elif body.get("at") is not None:
            try:
                start = max(0.0, float(body["at"]))
            except (ValueError, TypeError):
                start = 0.0
            logger.info(f"[/api/play] Explicit position requested for '{p.name}': {start:.2f}s ({fmt_time(start)})")
        else:
            saved = get_saved_pos(p)
            start = max(0.0, float(saved[0])) if saved else 0.0
            logger.info(f"[/api/play] Resuming '{p.name}' at saved position: {start:.2f}s ({fmt_time(start)})")

        rec = cfg.setdefault("recent", [])
        rec[:] = [r for r in rec if r["path"] != str(p) and r["path"] != str(p.resolve())]
        rec.insert(0, {"path": str(p.resolve()), "t": time.time(), "sub": body.get("label")})
        del rec[12:]
        save_cfg()
        state["cur"] = fid
        return run_job(p.stem, lambda job: prepare(p, job, sub, int(body.get("audio") or 0)),
                       lambda ready: file_work(ready, str(p.resolve()), start))

    @app.post("/api/youtube")
    def youtube(body: dict):
        url = body.get("url", "")
        return run_job("YouTube: " + url[:60],
                       lambda job: prepare(download(url, job), job), file_work)

    @app.post("/api/live")
    def live(body: dict):
        return run_job("Live audio", lambda job: None,
                       lambda _: live_work(body.get("device")))

    @app.post("/api/screen")
    def screen(body: dict):
        return run_job("Screen share", lambda job: None,
                       lambda _: screen_work(body.get("device")))

    @app.post("/api/stop")
    async def stop():
        if task and not task.done():
            task.cancel()   # play_media saves the position on the way out
            await asyncio.gather(task, return_exceptions=True)
        host = state["device"] or cfg["host"]
        if host:   # belt and braces for RAOP/live streams
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, lambda: airplay_request(host, "POST", "/stop", timeout=2))
            except Exception:
                pass
        state.update(status="idle", now=None)
        return {"ok": True}

    def guarded(fn, *a):
        try:
            fn(*a)
            return {"ok": True}
        except RuntimeError as e:
            return {"ok": False, "error": str(e)}

    @app.post("/api/pause")
    def pause():
        return guarded(pause_toggle)

    @app.post("/api/seek")
    def seek_to(body: dict):
        return guarded(lambda: seek(body.get("position"), body.get("delta")))

    @app.get("/api/status")
    def status():
        try:
            lan = lan_ip(cfg["host"] or "192.168.1.1")
        except OSError:
            lan = None
        return {**state, "host": cfg["host"], "device_name": cfg.get("device_name", ""),
                "lan": lan, "media_port": MEDIA_PORT, "jobs": JOBS[-5:],
                "missing": [t for t in ("ffmpeg", "ffprobe") if not shutil.which(t)]}

    @app.get("/api/recent")
    def recent():
        out = []
        for r in cfg.get("recent", []):
            p = Path(r["path"])
            if p.is_file():
                pos = get_saved_pos(p)
                out.append({"id": add_index(p), "name": p.stem, "size": p.stat().st_size,
                            "t": r["t"], "sub": r.get("sub"), "pos": pos[0], "dur": pos[1]})
        return out

    @app.get("/api/thumb/{fid}")
    def thumb(fid: str):
        p = INDEX.get(fid)
        out = CACHE / "thumbs" / (fid + ".jpg")
        if p and p.suffix.lower() in VIDEO and not out.exists() and shutil.which("ffmpeg"):
            out.parent.mkdir(parents=True, exist_ok=True)
            for ss in ("60", "5", "0"):
                subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", ss, "-i", str(p),
                                "-frames:v", "1", "-vf", "scale=240:-2", str(out)],
                               capture_output=True)
                if out.exists() and out.stat().st_size:
                    break
                out.unlink(missing_ok=True)
        if not out.exists():
            raise HTTPException(404)
        return FileResponse(out)

    @app.get("/api/tracks/{fid}")
    def tracks(fid: str, audio: int = 0, sub: int | None = None):
        p = INDEX.get(fid)
        if not p:
            return {"ok": False, "error": "unknown file"}
        resume = int(get_saved_pos(p)[0])
        if p.suffix.lower() in AUDIO:
            return {"ok": True, "id": fid, "subs": [], "audios": [], "direct": True,
                    "cached": False, "mode": "direct", "video": "audio only",
                    "source_text": "Audio only", "conversion_text": "", "output_text": "AirPlay Audio",
                    "summary": "Music, streamed over AirPlay audio", "burn_ok": False, "resume": resume,
                    "reasons": []}
        try:
            info = probe(p)
        except (RuntimeError, subprocess.CalledProcessError) as e:
            return {"ok": False, "error": f"could not read file: {e}"}
        subs, auds, v = [], [], None
        for st in info.get("streams", []):
            t = st.get("tags", {})
            base = {"lang": t.get("language", "und"), "title": t.get("title", ""),
                    "codec": st.get("codec_name", "")}
            if st.get("codec_type") == "video" and not v:
                v = st
            elif st.get("codec_type") == "subtitle":
                subs.append({**base, "i": len(subs), "text": base["codec"] not in
                             ("hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub")})
            elif st.get("codec_type") == "audio":
                auds.append({**base, "i": len(auds), "ch": st.get("channels")})
        if p.with_suffix(".srt").exists():
            subs.insert(0, {"i": -1, "lang": "ext", "title": p.with_suffix(".srt").name,
                            "codec": "srt", "text": True})

        analysis = check_compatibility(info, sub, audio)
        cached = is_cached(p, sub, audio)
        vtxt = f"{v.get('height', '?')}p {round(fps_of(v))}fps {v.get('codec_name', '')}" if v else "no video"
        return {
            "ok": True,
            "id": fid,
            "subs": subs,
            "audios": auds,
            "direct": analysis["mode"] == "direct",
            "cached": cached,
            "mode": analysis["mode"],
            "reasons": analysis["reasons"],
            "video": vtxt,
            "burn_ok": has_libass(),
            "resume": resume,
            "source_text": analysis["source_text"],
            "conversion_text": analysis["conversion_text"] if analysis["mode"] != "direct" else "",
            "output_text": analysis["output_text"],
            "summary": analysis["summary"]
        }

    @app.post("/api/match")
    def match(body: dict):
        for i, p in INDEX.items():
            if p.name == body.get("name") and p.stat().st_size == body.get("size"):
                return {"id": i, "name": p.stem}
        return {"id": None}

    @app.post("/api/pick")
    def pick():
        path = pick_file()
        if not path:
            return {"id": None}
        p = Path(path)
        if p.suffix.lower() not in VIDEO | AUDIO:
            return {"id": None, "error": "not a video or audio file"}
        return {"id": add_index(p), "name": p.stem}

    @app.get("/api/devices")
    async def devices():
        try:
            found = await discover(asyncio.get_running_loop())
        except Exception:
            found = []
        return [{"name": d.name, "ip": str(d.address)} for d in found]

    @app.post("/api/host")
    def set_host(body: dict):
        cfg["host"], cfg["device_name"] = body.get("host", ""), body.get("name", "")
        state["device"] = None
        save_cfg()
        return {"ok": True}

    def cache_bytes():
        return sum(f.stat().st_size for f in CACHE.rglob("*") if f.is_file()) if CACHE.exists() else 0

    @app.get("/api/cache")
    def cache_size():
        return {"bytes": cache_bytes()}

    @app.post("/api/cache/clear")
    def cache_clear():
        if state["status"] in ("playing", "connecting") or any(
                j["stage"] not in ("done", "error") for j in JOBS):
            return {"ok": False, "error": "Stop playback and wait for conversions to finish first."}
        CACHE.mkdir(parents=True, exist_ok=True)
        freed = cache_bytes()
        for f in CACHE.iterdir():
            shutil.rmtree(f, ignore_errors=True) if f.is_dir() else f.unlink(missing_ok=True)
        TOKENS.clear()
        return {"ok": True, "freed": freed, "bytes": cache_bytes()}

    @app.get("/api/settings")
    def get_settings():
        return cfg

    @app.post("/api/settings")
    def set_settings(body: dict):
        cfg["host"] = str(body.get("host", "")).strip()
        cfg["library"] = [x.strip() for x in body.get("library", []) if x.strip()]
        save_cfg()
        scan_library()
        return {"ok": True}

    start_media_server()
    print(f"NeoCast portal: http://localhost:{UI_PORT}  (media server on :{MEDIA_PORT})")
    threading.Timer(1.5, lambda: webbrowser.open(f"http://localhost:{UI_PORT}")).start()
    uvicorn.run(app, host="127.0.0.1", port=UI_PORT, log_level="warning")


def main():
    global MEDIA_PORT
    load_cfg()
    p = argparse.ArgumentParser(prog="neocast")
    p.add_argument("--host", help="Apple TV IP, e.g. 192.168.1.108 (saved in settings)")
    p.add_argument("--port", type=int, default=MEDIA_PORT, help="media server port")
    sub = p.add_subparsers(dest="cmd", required=True)
    for n in ("scan", "info", "devices", "portal", "selftest"):
        sub.add_parser(n)
    sub.add_parser("audio").add_argument("file")
    v = sub.add_parser("video")
    v.add_argument("file")
    v.add_argument("--sub", type=int, metavar="N", help="burn in embedded subtitle track N (-1 = sidecar .srt)")
    v.add_argument("--audio-track", type=int, default=0, metavar="N")
    v.add_argument("--restart", action="store_true", help="ignore saved resume position")
    sub.add_parser("live").add_argument("--device")
    sub.add_parser("screen").add_argument("--device")
    args = p.parse_args()
    MEDIA_PORT = args.port
    if args.host:
        cfg["host"] = args.host

    try:
        if args.cmd == "selftest":
            return selftest()
        if args.cmd == "devices":
            return list_devices()
        if args.cmd == "portal":
            return portal()
        if args.cmd == "scan":
            for d in asyncio.run(_scan_all()):
                print(f"{d.name:25} {d.address}  {d.device_info.model}  "
                      f"{d.device_info.version or ''}")
            return
        if args.cmd == "info":
            conf = asyncio.run(_find_here())
            print("Name   :", conf.name, "\nAddress:", conf.address,
                  "\nModel  :", conf.device_info.model, "\nServices:")
            for s in conf.services:
                print(f"  - {s.protocol.name:10} port {s.port}  pairing={s.pairing.name}")
            return
        if args.cmd in ("audio", "video"):
            path = Path(args.file).expanduser().resolve()
            if not path.is_file():
                sys.exit(f"File not found: {path}")
            if args.cmd == "video":
                key = str(path)
                info = probe(path)
                analysis = check_compatibility(info, args.sub, args.audio_track)
                print(f"Source: {analysis['source_text']}")
                if analysis["mode"] != "direct":
                    print(f"Conversion required: {analysis['conversion_text']}")
                    print(f"Output: {analysis['output_text']}")
                else:
                    print("Direct playback: media is fully compatible")
                ready = prepare(path, {}, args.sub, args.audio_track)
                saved = get_saved_pos(path)
                start_media_server()
                start = 0 if args.restart or not saved else saved[0]
                cli_run(path.name, file_work(ready, key, start))
            else:
                cli_run(path.name, file_work(path))
        elif args.cmd == "live":
            cli_run("Live audio", live_work(args.device))
        elif args.cmd == "screen":
            start_media_server()
            cli_run("Screen share", screen_work(args.device))
    except RuntimeError as e:
        sys.exit(str(e))
    except KeyboardInterrupt:
        print("\nStopped.")


async def _scan_all():
    return await pyatv.scan(asyncio.get_running_loop(), timeout=6)


async def _find_here():
    return await find(asyncio.get_running_loop(), cfg["host"])


if __name__ == "__main__":
    main()
