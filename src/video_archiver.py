#!/usr/bin/env python3
"""
Video Archiver - saves video links (YouTube and 1000+ other sites) as MP3 or HD MP4.

"Build Video Archiver.bat" turns this file into a single "Video Archiver.exe" that
carries its own yt-dlp, FFmpeg and Deno. On first start the exe unpacks them to
LOCALAPPDATA/VideoArchiver/tools; yt-dlp keeps itself up to date from there.
"""

import base64
import glob
import hashlib
import html
import http.client
import io
import json
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import uuid
from pathlib import Path

_local = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
DATA_DIR = Path(os.environ.get("YTD_DATA_DIR") or Path(_local) / "VideoArchiver")
OLD_DATA_DIR = Path(_local) / "YTArchiver"      # the app was called "YT Archiver" before


def migrate_old_data():
    """Bring settings, keys, the list of downloaded files and unfinished uploads over
    from when the app was called YT Archiver. Returns a note, or None if nothing to do."""
    if os.environ.get("YTD_DATA_DIR") or DATA_DIR.exists() or not OLD_DATA_DIR.is_dir():
        return None
    try:
        OLD_DATA_DIR.rename(DATA_DIR)
        return "Moved your settings, keys and upload progress over from YT Archiver."
    except OSError:
        pass
    try:   # something still uses the old folder (e.g. YT Archiver is open): copy instead
        shutil.copytree(OLD_DATA_DIR, DATA_DIR,
                        ignore=shutil.ignore_patterns("tools", "tools.new", ".instance.lock"))
        return (f"Copied your settings, keys and upload progress over from YT Archiver. The old folder "
                f"{OLD_DATA_DIR} is still there; you can delete it.")
    except OSError as exc:
        shutil.rmtree(DATA_DIR, ignore_errors=True)
        return (f"Couldn't bring your old YT Archiver settings over ({exc.strerror or exc}). Close YT "
                "Archiver and start Video Archiver again.")
TOOLS = DATA_DIR / "tools"
BUNDLE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
EXE = ".exe" if sys.platform == "win32" else ""
YTDLP = TOOLS / ("yt-dlp" + EXE)
DENO = TOOLS / ("deno" + EXE)
FFMPEG_DIR = TOOLS / "ffmpeg"
REQUIRED_TOOLS = [YTDLP, DENO, FFMPEG_DIR / ("ffmpeg" + EXE), FFMPEG_DIR / ("ffprobe" + EXE)]
SETTINGS_FILE = DATA_DIR / "settings.json"
UPDATE_STAMP = DATA_DIR / ".last_update_check"
DEFAULT_OUT = Path.home() / "Downloads" / "Video Archiver"
STAGING_NAME = ".yta-incomplete"   # unfinished downloads live here, never next to your files
UPDATE_EVERY_DAYS = 3
MIN_DENO = (2, 3, 0)               # oldest Deno current yt-dlp accepts
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW

# Only one user of the tools at a time: a download job or an update, never both.
TOOL_LOCK = threading.Lock()

PLAYLIST_CONFIRM_OVER = 25   # ask before downloading more videos than this
PLAYLIST_MAX = 1000          # never queue more than this from one link
PLAYLIST_SLEEP = ["--sleep-requests", "1", "--sleep-interval", "2"]  # gentler on YouTube

WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                    *(f"LPT{i}" for i in range(1, 10))}

# "0" is the best VBR setting. YouTube audio is ~130-160 kbps, so higher
# bitrates can't add quality; VBR 0 just keeps conversion loss lowest.
MP3_QUALITIES = {"High (VBR, recommended)": "0", "Standard 192 kbps": "192K",
                 "Small 128 kbps": "128K"}
MP4_QUALITIES = {"1080p Full HD": 1080, "720p HD": 720,
                 "1440p (newer devices only)": 1440, "2160p 4K (newer devices only)": 2160}
MP3_HINT = ("YouTube audio is about 130-160 kbps, so no setting adds quality. "
            "High keeps the conversion loss lowest.")
MP4_HINT_HD = "H.264 video with AAC audio. Plays on practically everything."
MP4_HINT_UHD = ("1440p and 4K only exist as VP9 or AV1. VLC and browsers play them; "
                "some TVs, phones and Windows players need extra codecs or can't play them.")

COMMON_ARGS = ["--ignore-config", "--encoding", "utf-8", "--color", "never"]
LIVE_FILTER = "!is_live & live_status!=?is_upcoming & live_status!=?post_live"
LIVE_STATES = ("is_live", "is_upcoming", "post_live")
PROGRESS_TPL = ("download:@@P %(progress.downloaded_bytes)s|%(progress.total_bytes)s|"
                "%(progress.total_bytes_estimate)s|%(progress.speed)s|%(progress.eta)s|"
                "%(info.vcodec)s|%(info.acodec)s")
POSTPROC_TPL = "postprocess:@@PP %(progress.postprocessor)s|%(progress.status)s"


# --------------------------------------------------------------------------
# Process helpers, single instance, leftovers
# --------------------------------------------------------------------------

def tool_env():
    env = os.environ.copy()
    env["PATH"] = str(TOOLS) + os.pathsep + env.get("PATH", "")  # lets yt-dlp find Deno
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def proc_kwargs():
    kw = {"env": tool_env(), "creationflags": NO_WINDOW}
    if sys.platform != "win32":
        kw["start_new_session"] = True
    return kw


def kill_tree(proc):
    """Stop yt-dlp together with any FFmpeg it started."""
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, creationflags=NO_WINDOW)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def run_tool(exe, args, timeout):
    return subprocess.run([str(exe), *args], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, stdin=subprocess.DEVNULL, **proc_kwargs())


_instance_handle = None


def acquire_single_instance():
    """True if no other Video Archiver is running. Two copies could otherwise
    unpack or update the tools at the same time and break them."""
    global _instance_handle
    try:
        if sys.platform == "win32":
            import ctypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.CreateMutexW.restype = ctypes.c_void_p
            _instance_handle = k32.CreateMutexW(None, False, "Local\\VideoArchiver.SingleInstance")
            return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS
        import fcntl
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _instance_handle = open(DATA_DIR / ".instance.lock", "w")
        fcntl.flock(_instance_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False
    except Exception:
        return True  # if the check itself fails, don't lock the user out


def clean_stale_bundles():
    """Delete copies of this exe that earlier runs unpacked to TEMP and never
    removed (after a crash or a forced close). Only folders with our own marker
    files are touched, and only while no other copy of the app is running."""
    mine = getattr(sys, "_MEIPASS", None)
    if not mine:
        return 0
    removed = 0
    for d in Path(tempfile.gettempdir()).glob("_MEI*"):
        try:
            if (d.is_dir() and os.path.normcase(str(d)) != os.path.normcase(mine)
                    and (d / "yta_bundle_id.txt").is_file() and (d / "tools.tar.xz").is_file()
                    and time.time() - d.stat().st_mtime > 600):
                shutil.rmtree(d, ignore_errors=True)
                removed += not d.exists()
        except OSError:
            pass
    return removed


# --------------------------------------------------------------------------
# Built-in tools
# --------------------------------------------------------------------------

def _bundle_ident():
    try:
        return (BUNDLE / "yta_bundle_id.txt").read_text().strip()
    except OSError:
        return "unknown"


def missing_tools():
    return [p.name for p in REQUIRED_TOOLS if not p.exists()]


def tools_need_unpack():
    if not (BUNDLE / "tools.tar.xz").exists():
        return False
    if missing_tools():
        return True
    try:
        return (TOOLS / ".bundle_id").read_text().strip() != _bundle_ident()
    except OSError:
        return True


def unpack_tools(force=False):
    """Unpack the tools shipped inside the exe when any are missing, the exe is
    newer, or force is set (repair). Returns None or an error message."""
    if not (BUNDLE / "tools.tar.xz").exists():
        missing = missing_tools()
        return f"Missing tools: {', '.join(missing)}. Run the builder again." if missing else None
    if not force and not tools_need_unpack():
        return None
    new = TOOLS.with_name("tools.new")
    try:
        shutil.rmtree(new, ignore_errors=True)
        new.mkdir(parents=True)
        with tarfile.open(BUNDLE / "tools.tar.xz", "r:xz") as tar:
            if hasattr(tarfile, "data_filter"):
                tar.extractall(new, filter="data")
            else:
                tar.extractall(new)
        (new / ".bundle_id").write_text(_bundle_ident())
        if TOOLS.exists():
            shutil.rmtree(TOOLS)
        new.rename(TOOLS)
    except OSError as exc:
        shutil.rmtree(new, ignore_errors=True)
        return (f"Couldn't unpack the built-in tools ({exc.strerror or exc}). "
                "If your antivirus blocked a file, allow it and use Tools > Repair.")
    try:
        UPDATE_STAMP.unlink()  # check for a newer yt-dlp right away
    except OSError:
        pass
    return None


def ytdlp_version():
    try:
        r = run_tool(YTDLP, ["--version"], 90)
        lines = r.stdout.strip().splitlines()
        return lines[0] if r.returncode == 0 and lines else None
    except Exception:
        return None


def deno_version():
    """(major, minor, patch) or None."""
    try:
        m = re.search(r"deno (\d+)\.(\d+)\.(\d+)", run_tool(DENO, ["--version"], 60).stdout)
        return tuple(int(x) for x in m.groups()) if m else None
    except Exception:
        return None


def update_due():
    try:
        return time.time() - UPDATE_STAMP.stat().st_mtime > UPDATE_EVERY_DAYS * 86400
    except OSError:
        return True


def is_nightly(version):
    """Nightly builds have a fourth number (build time): 2026.04.01.232854."""
    return bool(version and re.fullmatch(r"\d{4}\.\d{2}\.\d{2}\.\d+", str(version).strip()))


def version_label(version):
    return f"{version} (nightly)" if is_nightly(version) else str(version)


def ytdlp_channel():
    return "nightly" if load_settings().get("ytdlp_channel") == "nightly" else "stable"


def update_ytdlp():
    """Let yt-dlp update itself on the chosen channel (it checks its own release
    checksums). Call with TOOL_LOCK held. Returns (message or None, worked)."""
    channel = ytdlp_channel()
    retry = "Couldn't check for a yt-dlp update. Will try again next start."
    before = ytdlp_version()

    def attempt():
        r = run_tool(YTDLP, ["--update-to", channel], 180)
        if r.returncode != 0 and is_cert_error(r.stdout + r.stderr):
            r = run_tool(YTDLP, ["--compat-options", "no-certifi", "--update-to", channel], 180)
        return r
    try:
        r = attempt()
        if r.returncode == 0 and channel == "stable" and is_nightly(ytdlp_version()):
            # yt-dlp may not step back from a newer nightly to an older stable:
            # put the app's own stable yt-dlp back (nothing else), then update that.
            problem = restore_bundled_ytdlp()
            if problem:
                return problem, False
            r = attempt()
    except Exception:
        return retry, False
    if r.returncode != 0:
        return retry, False
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPDATE_STAMP.touch()
    after = ytdlp_version()
    if after and after != before:
        return f"yt-dlp is now {version_label(after)}.", True
    return None, True


# --------------------------------------------------------------------------
# Sites yt-dlp doesn't handle (yet): stream links, generic scanner, plugins
# --------------------------------------------------------------------------

def parse_link_line(line):
    """'URL | referer=PAGE | title=TITLE' -> (URL, {"referer": ..., "title": ...})."""
    parts = [p.strip() for p in str(line).split(" | ")]
    url, opts = parts[0], {}
    for part in parts[1:]:
        key, sep, value = part.partition("=")
        key = key.strip().lower()
        if sep and key in ("referer", "title") and value.strip():
            opts[key] = value.strip()
    return url, opts


def is_stream_url(url):
    return bool(re.search(r"\.(m3u8|mpd)(?:$|[?#])", str(url).split("?")[0] + "?", re.I))


def stream_id(url):
    """Stable id for a stream link (they're all called 'master' or 'index')."""
    return "s-" + hashlib.md5(str(url).split("?")[0].encode("utf-8")).hexdigest()[:10]


def stream_title(url, page=None):
    """Best guess at a title when none was given: the page's or stream's path."""
    junk = {"master", "index", "playlist", "manifest", "chunklist", "video", "stream", "hls", "dash",
            "main", "media", "mpd", "m3u8", "v", "embed", "watch", "player"}
    for link in (page, url):
        m = re.match(r"https?://([^/?#]+)([^?#]*)", str(link or ""))
        if not m:
            continue
        for seg in reversed([x for x in m.group(2).split("/") if x]):
            seg = re.sub(r"\.(m3u8|mpd|html?|php|aspx?)$", "", urllib.parse.unquote(seg), flags=re.I)
            words = re.sub(r"[-_+.]+", " ", seg).strip()
            if words and words.lower() not in junk and re.search(r"[^\W\d_]{2,}", words):
                return words[:120]
    m = re.match(r"https?://(?:www\.)?([^/?#:]+)", str(page or url))
    return f"Stream from {m.group(1)}" if m else "Stream"


def safe_title(title):
    t = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(title)).strip(" .")
    return t[:120].strip(" .") or "stream"


def needs_generic(errors):
    """True when a site's own yt-dlp extractor broke, so the generic page scanner
    (--force-generic-extractor) is worth a try."""
    skip = re.compile(r"(?i)sign in|log ?in|private|http error 40[34]|geo|your country|members|"
                      r"drm|age-restricted|confirm your age|certificate|premium|paid")
    broke = re.compile(r"(?i)unable to extract|no video formats|please report this issue|"
                       r"keyerror|nonetype|unable to find|unexpected|could not find")
    for e in errors:
        m = re.search(r"\[([\w:.-]+)\]", e)
        if m and m.group(1).lower() != "generic" and not skip.search(e) and broke.search(e):
            return True
    return False


def plugins_dir():
    """yt-dlp's own plugin folder; the app's yt-dlp loads plugins from here."""
    base = os.environ.get("APPDATA") or str(Path.home() / ".config")
    return Path(base) / "yt-dlp" / "plugins"


# ---- the "teach a new site" wizard ----
# Every plugin it writes is this same, tested code plus a few settings (SITE)
# learned from an example page. The app runs this code itself on the example
# page before saving, so it knows in advance whether the plugin will work.

PLUGIN_CORE = r"""
import html as _html
import re as _re
from urllib.parse import urljoin as _urljoin, urlparse as _urlparse

_MEDIA = ("m3u8", "mpd", "mp4", "webm", "m4a", "mp3")
_STREAM_RE = _re.compile(r'''(?:https?:)?(?:\\?/){2}[^\s"'<>()]+?\.(?:m3u8|mpd|mp4|webm|m4a|mp3)'''
                         r'''(?:\?[^\s"'<>()]*)?(?=[\s"'<>()\\]|$)''', _re.I)
_REL_RE = _re.compile(r'''["'](/[^"'\s<>]+?\.(?:m3u8|mpd|mp4|webm)(?:\?[^"'\s<>]*)?)["']''', _re.I)
_SRC_RE = _re.compile(r'''<(?:video|source)\b[^>]*?\bsrc\s*=\s*["']([^"']+)["']''', _re.I)
_JUNK_RE = _re.compile(r"(?i)preview|thumb|sprite|teaser|trailer|poster|/ads?/|advert|banner")
_EMBED_RE = _re.compile(r"(?i)youtube(?:-nocookie)?\.com/embed/|player\.vimeo\.com/video/|dailymotion\.com/embed/|"
                        r"rumble\.com/embed/|odysee\.com/\$/embed/|streamable\.com/[eo]/|bitchute\.com/embed/|"
                        r"/videos/embed/|player\.twitch\.tv|facebook\.com/plugins/video|ok\.ru/videoembed|"
                        r"vk\.com/video_ext|/embed|player")


def _clean(u, page_url):
    for a, b in (("\\/", "/"), ("\\u0026", "&"), ("\\u002F", "/"), ("\\u002f", "/"),
                 ("\\u003D", "="), ("\\u003d", "=")):
        u = u.replace(a, b)
    u = _html.unescape(u).strip().rstrip("\\")
    if u.startswith("//"):
        u = "https:" + u
    return _urljoin(page_url, u)


def stream_ext(u):
    path = _urlparse(u).path.lower()
    for e in _MEDIA:
        if path.endswith("." + e):
            return e
    return ""


def page_meta(webpage, name):
    for tag in _re.findall(r"<meta\b[^>]*>", webpage, _re.I):
        attrs = {k.lower(): v for k, v in _re.findall(r'''([\w:-]+)\s*=\s*["']([^"']*)["']''', tag)}
        if name in (attrs.get("property"), attrs.get("name"), attrs.get("itemprop")) and attrs.get("content"):
            return _html.unescape(attrs["content"]).strip()
    return None


def page_title(webpage, fallback):
    for name in ("og:title", "twitter:title"):
        v = page_meta(webpage, name)
        if v:
            return v
    m = _re.search(r"<title[^>]*>(.*?)</title>", webpage, _re.I | _re.S)
    if m and m.group(1).strip():
        return _html.unescape(_re.sub(r"\s+", " ", m.group(1))).strip()
    return fallback


def find_streams(webpage, page_url):
    found = []

    def add(u, need_ext=False):
        if not u or u.startswith(("blob:", "data:", "javascript:")):
            return
        u = _clean(u, page_url)
        if u.startswith("http") and u not in found and (stream_ext(u) or not need_ext):
            found.append(u)
    for m in _STREAM_RE.finditer(webpage):
        add(m.group(0))
    for m in _REL_RE.finditer(webpage):
        add(m.group(1))
    for m in _SRC_RE.finditer(webpage):
        add(m.group(1))
    for name in ("og:video:secure_url", "og:video:url", "og:video", "twitter:player:stream"):
        add(page_meta(webpage, name), need_ext=True)
    for m in _re.finditer(r'"contentUrl"\s*:\s*"([^"]+)"', webpage):
        add(m.group(1), need_ext=True)
    return found


def find_embeds(webpage, page_url):
    out = []
    for rx in (r'''<iframe\b[^>]*?\bsrc\s*=\s*["']([^"']+)["']''', r'"embedUrl"\s*:\s*"([^"]+)"'):
        for m in _re.finditer(rx, webpage, _re.I):
            u = _clean(m.group(1), page_url)
            if u.startswith("http") and u not in out and _EMBED_RE.search(u) and not _JUNK_RE.search(u):
                out.append(u)
    return out


def pick(streams, site):
    order = {e: i for i, e in enumerate(_MEDIA)}

    def key(item):
        i, u = item
        return (bool(site.get("prefer_host")) and (_urlparse(u).hostname or "") != site["prefer_host"],
                bool(site.get("prefer_ext")) and stream_ext(u) != site["prefer_ext"],
                bool(_JUNK_RE.search(u)), order.get(stream_ext(u), 9), i)
    return sorted(enumerate(streams), key=key)[0][1]


def choose(webpage, page_url, video_id, site):
    # 1) an address rebuilt from the video's id or a value on the page
    if site.get("template"):
        value = video_id
        if site.get("value_regex"):
            m = _re.search(site["value_regex"], webpage)
            value = m.group(1) if m else None
        if value:
            return "stream", site["template"].replace("{value}", value)
    # 2) an address written in the page
    streams = find_streams(webpage, page_url)
    if streams:
        return "stream", pick(streams, site)
    # 3) a player embedded from another site
    if site.get("embeds", True):
        embeds = find_embeds(webpage, page_url)
        if embeds:
            return "embed", embeds[0]
    return None, None
"""

PLUGIN_CLASS = r"""

from yt_dlp.extractor.common import InfoExtractor


class @@CLASS@@(InfoExtractor):
    IE_NAME = "yta:" + SITE["name"]
    _VALID_URL = SITE["pages"]

    def _real_extract(self, url):
        video_id = self._match_id(url)
        webpage = self._download_webpage(url, video_id)
        kind, target = choose(webpage, url, video_id, SITE)
        if kind == "embed":
            return self.url_result(target)
        if not target:
            self.raise_no_formats("No video address found on this page. Run Video Archiver's "
                                  "Tools > Teach Video Archiver a new site again with this link.", expected=True)
        headers = {"Referer": url}
        ext = stream_ext(target)
        if ext == "m3u8":
            formats = self._extract_m3u8_formats(target, video_id, "mp4", m3u8_id="hls", headers=headers)
        elif ext == "mpd":
            formats = self._extract_mpd_formats(target, video_id, mpd_id="dash", headers=headers)
        else:
            formats = [{"url": target, "ext": ext or "mp4", "http_headers": headers}]
        thumb = page_meta(webpage, "og:image")
        return {
            "id": video_id,
            "title": page_title(webpage, video_id),
            "description": page_meta(webpage, "og:description") or page_meta(webpage, "description"),
            "thumbnail": _urljoin(url, thumb) if thumb else None,
            "formats": formats,
            "http_headers": headers,
        }
"""

_core = {}


def plugin_core():
    """The plugin's own page-reading code, loaded into the app for dry runs."""
    if not _core:
        exec(compile(PLUGIN_CORE, "<plugin core>", "exec"), _core)
    return _core


class PageError(Exception):
    pass


BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")


def fetch_page(url):
    """(html, final address) of a web page, fetched like a browser would."""
    import gzip
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA, "Accept-Language": "en,de;q=0.8",
                                               "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read(5_000_000)
            if (r.headers.get("Content-Encoding") or "").lower() == "gzip":
                data = gzip.decompress(data)
            return data.decode(r.headers.get_content_charset() or "utf-8", "replace"), r.geturl()
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise PageError(f"The site refused to show the page to the app (error {exc.code}). It may need you "
                            "to be signed in, or it blocks programs. The stream-link route still works.")
        raise PageError(f"The site answered with error {exc.code}. Check the link.")
    except (OSError, ValueError) as exc:
        raise PageError(f"Couldn't open the page ({getattr(exc, 'reason', exc)}).")


def _site_base(url):
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    host = re.sub(r"^(www|m)\.", "", host)
    base = re.sub(r"[^a-z0-9]", "", host.rsplit(".", 1)[0].split(".")[-1]) or "site"
    return ("site" + base) if base[0].isdigit() else base


def guess_pattern(url):
    """'https://www.site.de/video/123/my-title' -> 'site.de/video/*/*' (* = changes per video)."""
    p = urllib.parse.urlsplit(url.strip())
    host = re.sub(r"^www\.", "", (p.hostname or "").lower())
    segs = [x for x in p.path.split("/") if x]
    generic = {"watch", "video", "videos", "play", "view", "player", "embed", "v", "media",
               "index.php", "watch.php", "video.php", "view.php", "play.php"}
    if p.query and (not segs or segs[-1].lower() in generic):
        for k, v in urllib.parse.parse_qsl(p.query):
            if re.fullmatch(r"[\w-]{3,}", v) and (re.search(r"\d", v) or len(v) >= 6):
                return f"{host}/{'/'.join(segs)}?{k}=*" if segs else f"{host}/?{k}=*"
    if not segs:
        return host + "/*"
    out = ["*" if i == len(segs) - 1 or re.search(r"\d", x) or len(x) > 24 else x for i, x in enumerate(segs)]
    return host + "/" + "/".join(out)


def wildcard_to_regex(pattern, example):
    """'site.de/video/*' -> yt-dlp _VALID_URL. The * whose example value has digits
    (or else the last *) becomes the video id."""
    pat = re.sub(r"^(?:[a-z]+://)?(?:www\.)?", "", str(pattern).strip(), flags=re.I)
    if "*" not in pat or "/" not in pat:
        raise ValueError("Put a * where each video's own part of the link goes, e.g. meine-seite.de/video/*")
    parts = re.split(r"(\*)", pat)
    loose = "".join("([^/?#&]+)" if x == "*" else re.escape(x) for x in parts)
    m = re.match(r"https?://(?:www\.)?" + loose, str(example).strip(), re.I)
    if not m:
        raise ValueError("Your video link doesn't fit this pattern. Check the part before the first *.")
    values = m.groups()
    idx = next((i for i, v in enumerate(values) if re.search(r"\d", v)), len(values) - 1)
    out, n = [], -1
    for x in parts:
        if x == "*":
            n += 1
            out.append("(?P<id>[^/?#&]+)" if n == idx else "[^/?#&]+")
        else:
            out.append(re.escape(x))
    return r"https?://(?:www\.)?" + "".join(out)


_SIGNED_RE = re.compile(r"(?i)(?:^|[&?])(?:token|sig|signature|expires?|exp|hdnts|policy|key-pair-id|hmac|auth|hash|st|e)=")


def learn_template(webpage, page_id, stream):
    """The stream address isn't in the page: find out whether it's built from the
    video's id or from a value on the page, so it can be rebuilt for other videos."""
    base, _, query = stream.partition("?")
    signed = bool(query and _SIGNED_RE.search("?" + query))
    tail = "" if signed or not query else "?" + query
    if page_id and len(page_id) >= 3 and page_id in base:
        return {"template": base.replace(page_id, "{value}") + tail, "value_regex": None,
                "how": "the video's id from its link", "signed": signed}
    tokens = {t for t in re.split(r"[/_.=&:-]", base.split("://", 1)[-1])
              if len(t) >= 4 and (re.search(r"\d", t) or len(t) >= 10)}
    for tok in sorted(tokens, key=len, reverse=True):
        for m in re.finditer(re.escape(tok), webpage):
            before = webpage[max(0, m.start() - 80):m.start()]
            km = re.search(r'''["']?([A-Za-z_][\w-]{1,40})["']?\s*[:=]\s*["']?$''', before)
            if not km:
                continue
            key = re.escape(km.group(1))
            rx = r'''["']?''' + key + r'''["']?\s*[:=]\s*["']?([\w-]+)'''
            hit = re.search(rx, webpage)
            if hit and hit.group(1) == tok:
                return {"template": base.replace(tok, "{value}") + tail, "value_regex": rx,
                        "how": f"the value \"{km.group(1)}\" on the page", "signed": signed}
    return None


def analyze_site(link, link2="", stream="", fetch=fetch_page):
    """Work out how a plugin can find the videos of a site. Returns a dict:
    ok, lines (to show), pattern, site (settings), stream_line (fallback)."""
    core = plugin_core()
    link, link2, stream = link.strip(), (link2 or "").strip(), (stream or "").strip()
    res = {"ok": False, "lines": [], "pattern": "", "site": {}, "stream_line": None}
    say = res["lines"].append
    if not re.match(r"https?://", link):
        say("Paste a full link to one video page, starting with http:// or https://.")
        return res
    try:
        webpage, _ = fetch(link)
    except PageError as exc:
        say(str(exc))
        if stream:
            res["stream_line"] = f"{stream} | referer={link}"
        return res
    res["pattern"] = pattern = guess_pattern(link)
    page_id = re.match(wildcard_to_regex(pattern, link), link, re.I).group("id")
    site = {"prefer_host": None, "prefer_ext": None, "template": None, "value_regex": None, "embeds": True}
    title = core["page_title"](webpage, page_id)
    say(f"Page: {title}")

    if stream:
        if not re.match(r"https?://", stream):
            say("The stream address has to start with http:// or https://.")
            return res
        streams = core["find_streams"](webpage, link)
        same = [u for u in streams if u.split("?")[0] == stream.split("?")[0]]
        if same:
            site.update(prefer_host=urllib.parse.urlsplit(stream).hostname,
                        prefer_ext=core["stream_ext"](stream) or None)
            say("Found your stream address written in the page, so other videos' pages will have theirs too.")
        else:
            learned = learn_template(webpage, page_id, stream)
            if not learned:
                say("Your stream address isn't in the page and isn't built from anything on it. The site "
                    "probably asks its server for it with JavaScript, so a plugin can't find it for other videos.")
                say("You can still download this one video: press \"Use as stream link\".")
                res["stream_line"] = f"{stream} | referer={link}"
                return res
            site.update(template=learned["template"], value_regex=learned["value_regex"])
            say(f"Your stream address isn't in the page, but it's built from {learned['how']}, "
                "so it can be rebuilt for every video.")
            if learned["signed"]:
                say("Warning: the address contained a time-limited key, which was left out. If the site "
                    "needs that key, the plugin won't work; then use stream links per video.")
            res["stream_line"] = f"{stream} | referer={link}"
    else:
        streams = core["find_streams"](webpage, link)
        embeds = core["find_embeds"](webpage, link)
        if streams:
            chosen = core["pick"](streams, site)
            site.update(prefer_host=urllib.parse.urlsplit(chosen).hostname,
                        prefer_ext=core["stream_ext"](chosen) or None)
            kind = {"m3u8": "an HLS stream (.m3u8)", "mpd": "a DASH stream (.mpd)"}.get(
                core["stream_ext"](chosen), "a video file")
            say(f"Found {kind} on {site['prefer_host']} in the page"
                + (f" ({len(streams)} addresses in total; using the best one)." if len(streams) > 1 else "."))
        elif embeds:
            host = urllib.parse.urlsplit(embeds[0]).hostname
            say(f"The video is a player embedded from {host}. The plugin will hand it to yt-dlp's "
                "support for that site.")
        else:
            say("I couldn't find the video's address in the page: the site loads it with JavaScript.")
            say("Please give me the stream address, then press Analyze again:")
            for i, step in enumerate(("Open the video page in your browser and press F12.",
                                      "Click the Network tab and type m3u8 into its filter box (if nothing "
                                      "shows up later, try mpd instead).",
                                      "Play the video.",
                                      "Right-click the first line that appears > Copy > Copy URL.",
                                      "Paste it into \"Stream address\" here."), 1):
                say(f"  {i}. {step}")
            return res

    kind, target = core["choose"](webpage, link, page_id, site)
    if not target:
        say("Something doesn't add up: the plugin's own test on this page found nothing.")
        return res
    say(f"Test on this page: found {target[:110]}{'...' if len(target) > 110 else ''}")
    res.update(ok=True, site=site)
    if link2:
        try:
            m = re.match(wildcard_to_regex(pattern, link), link2, re.I)
            if not m:
                say("Second link: it doesn't fit the pattern below. Change the pattern so both links fit.")
            else:
                page2, _ = fetch(link2)
                _, target2 = core["choose"](page2, link2, m.group("id"), site)
                t2 = core["page_title"](page2, m.group("id"))
                if target2 and target2 != target:
                    say(f"Second video ({t2}): found its own address too. Good sign.")
                elif target2:
                    say(f"Second video ({t2}): found the SAME address as the first one. The site may show "
                        "a general clip (an ad or trailer) on every page; try giving the stream address.")
                else:
                    say(f"Second video ({t2}): found nothing. Try giving the stream address of the first video.")
        except (PageError, ValueError) as exc:
            say(f"Second link: {exc}")
    return res


def write_site_plugin(pattern, link, site):
    """Save the plugin into yt-dlp's plugin folder. Earlier plugins made for the
    same site are kept as .py.bak. Returns the file's path."""
    import pprint
    regex = wildcard_to_regex(pattern, link)
    base = _site_base(link)
    folder = plugins_dir() / f"yta_{base}" / "yt_dlp_plugins" / "extractor"
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob("*.py"):
        old.replace(old.with_name(old.name + ".bak"))
    settings = {**site, "name": base, "pages": regex}
    host = urllib.parse.urlsplit(link).hostname or base
    src = ('"""yt-dlp plugin for ' + host + ", made by Video Archiver's site wizard.\n\n"
           "To change it, run Tools > Teach Video Archiver a new site again rather than\n"
           'editing this file. To remove it, delete this folder.\n"""\n'
           "SITE = " + pprint.pformat(settings, width=100) + "\n"
           + PLUGIN_CORE + PLUGIN_CLASS.replace("@@CLASS@@", f"Yta{base.capitalize()}IE"))
    compile(src, "plugin", "exec")       # never write a file that can't load
    path = folder / f"yta_{base}.py"
    path.write_text(src, encoding="utf-8")
    return path


_extractor_cache = []


def search_extractors(query):
    """Names from 'yt-dlp --list-extractors' containing the query. Call with TOOL_LOCK held."""
    if not _extractor_cache:
        r = run_tool(YTDLP, ["--list-extractors"], 120)
        _extractor_cache.extend(l.strip() for l in r.stdout.splitlines() if l.strip())
    q = str(query).strip().lower()
    q = re.sub(r"^(?:[a-z]+://)?(?:www\.)?", "", q).split("/")[0]
    if "." in q:                                   # meineseite.de -> meineseite
        q = q.rsplit(".", 1)[0].split(".")[-1]
    key = re.sub(r"[^a-z0-9]", "", q)
    if not key:
        return [], len(_extractor_cache)
    hits = [n for n in _extractor_cache if key in re.sub(r"[^a-z0-9]", "", n.lower())]
    return hits, len(_extractor_cache)


def check_link(line, cookies=False):
    """Test a link with yt-dlp in verbose mode. Call with TOOL_LOCK held.
    cookies: yt-dlp cookie arguments (or True for Firefox). Returns (report text, full log)."""
    url, opts = parse_link_line(line)
    base = ["-v", "--simulate", "--no-playlist", "-I", "1:1", "--print",
            "@@C %(extractor_key)s|%(title)s|%(resolution)s|%(ext)s|%(duration_string)s", *pot_args()]
    if isinstance(cookies, (list, tuple)):
        base += list(cookies)
    elif cookies:
        base += ["--cookies-from-browser", "firefox"]
    if opts.get("referer"):
        base += ["--referer", opts["referer"]]
    log, notes = [], []

    def run(extra=()):
        r = run_tool(YTDLP, [*COMMON_ARGS, *base, *extra, "--", url], 180)
        out = (r.stdout or "") + "\n" + (r.stderr or "")
        log.append(f"$ yt-dlp {' '.join([*extra])} -v {url}\n{out}")
        return out
    out = run()
    if is_cert_error(out) and "@@C " not in out:
        notes.append("A certificate check failed; tried again with Windows' certificate list.")
        out = run(["--compat-options", "no-certifi"])
    errors = [l for l in out.splitlines() if l.startswith("ERROR:")]
    if "@@C " not in out and needs_generic(errors):
        notes.append("yt-dlp's support for this site failed, so the generic page scanner was tried too.")
        out2 = run(["--force-generic-extractor"])
        if "@@C " in out2:
            out, errors = out2, []

    report = []
    m = re.search(r"\[debug\] yt-dlp version (.+)", out)
    if m:
        report.append("yt-dlp:     " + m.group(1).strip()[:90])
    plug = [l.split("]", 1)[1].strip() for l in out.splitlines()
            if l.startswith("[debug]") and "plugin" in l.lower()]
    report.append("Plugins:    " + ("; ".join(plug) if plug else "none loaded"))
    results = [l[4:].split("|") for l in out.splitlines() if l.startswith("@@C ")]
    if results:
        ie, title, res, ext, dur = (results[0] + [""] * 5)[:5]
        report += [f"Result:     WORKS. Handled by: {ie}", f"Title:      {title}",
                   f"Best file:  {res} {ext}" + (f", {dur}" if dur and dur != "NA" else "")]
        if ie == "Generic":
            report.append("Note:       no dedicated support; the generic page scanner found the video.")
    else:
        msg = clean_error(errors[-1]) if errors else "yt-dlp found nothing to download."
        report += ["Result:     DOES NOT WORK", f"Reason:     {msg}", "", "What you can try:"]
        low = " ".join(errors).lower()
        tips = []
        if "unsupported url" in low:
            tips.append("Search the site in the box above: if it's not listed, yt-dlp doesn't know it.")
        if not m or "nightly" not in m.group(1).lower():
            tips.append("Tools > yt-dlp channel > Nightly: newest fixes and sites, often helps.")
        tips += ["Tools > Teach Video Archiver a new site: give it this link and it builds and tests "
                 "a plugin for the site, no programming needed.",
                 "Just this one video: F12 > Network, filter 'm3u8' or 'mpd', play the video, right-click "
                 "the request > Copy URL, then Tools > Add a stream link."]
        report += [f"  {i}. {t}" for i, t in enumerate(tips, 1)]
    if notes:
        report += [""] + notes
    return "\n".join(report), "\n\n".join(log)


def update_deno():
    """Let Deno upgrade itself. Call with TOOL_LOCK held."""
    before = deno_version()
    try:
        r = run_tool(DENO, ["upgrade"], 300)
    except Exception:
        return "Couldn't update Deno right now."
    after = deno_version()
    if r.returncode != 0 or not after:
        return "Couldn't update Deno right now."
    v = ".".join(map(str, after))
    return f"Deno updated to {v}." if after != before else f"Deno {v} is already the newest."


# --------------------------------------------------------------------------
# Settings, folders, names, messages
# --------------------------------------------------------------------------

def load_settings():
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


_settings_frozen = False


def save_settings(**changes):
    if _settings_frozen:
        return
    try:
        data = load_settings()
        data.update(changes)
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass  # not being able to remember a setting shouldn't break anything


def folder_problem(path):
    """Why the folder can't be used, or None. Never creates anything."""
    if not str(path).strip():
        return "Choose a folder first."
    folder = Path(path).expanduser()
    if not folder.is_absolute():
        return "Please use a full folder path, e.g. D:\\Music."
    if not folder.exists():
        return None  # the caller asks before creating it
    if not folder.is_dir():
        return "That path is a file, not a folder."
    try:
        probe = folder / ".ytdl_write_test"
        probe.write_text("ok")
        probe.unlink()
    except OSError as exc:
        return f"Can't save to this folder: {exc.strerror or exc}"
    return None


def create_folder(path):
    try:
        Path(path).expanduser().mkdir(parents=True, exist_ok=True)
        return None
    except OSError as exc:
        return f"Couldn't create the folder: {exc.strerror or exc}"


def safe_folder(name):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name or "").strip().rstrip(". ")
    name = name[:80].rstrip(". ")
    if name.split(".")[0].strip().upper() in WINDOWS_RESERVED:
        name = "_" + name
    return name or "Playlist"


def is_cert_error(text):
    low = str(text).lower()
    return "certificate_verify_failed" in low or "certificate verify failed" in low


def clean_error(exc):
    msg = str(exc).replace("ERROR: ", "").strip()
    msg = re.sub(r"^\[[^\]]+\]\s*[\w-]+:\s*", "", msg)
    first = msg.splitlines()[0][:300] if msg else "Unknown error"
    low = msg.lower()
    if "sign in to confirm" in low or "not a bot" in low:
        return ("YouTube wants to check you're not a bot. Wait a while and try again, or turn on "
                "Options > Use my Firefox cookies (Firefox must be signed in to YouTube).")
    if "could not find firefox" in low or ("cookies" in low and "firefox" in low):
        return "Couldn't read Firefox cookies. Is Firefox installed and was it opened at least once?"
    if "private video" in low or "video unavailable" in low:
        return "This video is private, removed, or not available in your country."
    if is_cert_error(msg):
        if "expired" in low or "not yet valid" in low:
            return ("A secure-connection check failed because of a date mismatch. Check that your "
                    "PC's date, time and time zone are correct, then try again.")
        return ("The video server's security certificate couldn't be verified, even with Windows' own "
                "certificate list. Usually this is antivirus \"HTTPS scanning\" / \"web shield\" or a "
                "school/work network filter. Try another network, or add an exception for this app "
                "(or yt-dlp) in your antivirus. If it's only one site, its server is misconfigured; "
                "try again later.")
    if "unsupported url" in low:
        return "yt-dlp doesn't know this site, or the link isn't a page with a video on it."
    if ("login" in low or "log in" in low or "sign in" in low or "logged-in" in low
            or "account" in low and "required" in low):
        return ("This site only shows the video to signed-in users. Sign in on Firefox, then turn "
                "on Options > Use my Firefox cookies.")
    if "http error 403" in low or "forbidden" in low:
        return ("The site refused the download (403). Try Tools > Update yt-dlp, or turn on "
                "Options > Use my Firefox cookies.")
    if "no video formats found" in low or "no video could be found" in low:
        return "There's no downloadable video on this page (it may be a photo or text post)."
    if "requested format is not available" in low:
        return "No matching format for this video. Try again later or choose another quality."
    if "ffmpeg" in low and ("not found" in low or "not installed" in low):
        return "FFmpeg is missing. Use Tools > Repair built-in tools."
    if "postprocessing" in low or "ffmpeg" in low or "conversion failed" in low:
        return "Converting the file failed: " + first
    return first


def fmt_bytes(n):
    if not n:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def describe_progress(d):
    """Turn progress numbers into (percent or None, short text)."""
    done = d.get("downloaded_bytes") or 0
    total = d.get("total_bytes") or d.get("total_bytes_estimate")
    pct = min(done / total * 100, 100) if total else None
    speed, eta = d.get("speed"), d.get("eta")
    parts = [f"{fmt_bytes(done)} of {fmt_bytes(total)}"]
    if speed:
        parts.append(f"{fmt_bytes(speed)}/s")
    if eta is not None:
        parts.append(f"{int(eta) // 60}:{int(eta) % 60:02d} left")
    return pct, ", ".join(parts)


def _num(text):
    try:
        return float(text)
    except ValueError:
        return None


def _rmtree_retry(path):
    for _ in range(8):
        shutil.rmtree(path, ignore_errors=True)
        if not Path(path).exists():
            return
        time.sleep(0.4)  # Windows can hold a killed process's files for a moment


def _hide(path):
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.kernel32.SetFileAttributesW(str(path), 0x2)  # FILE_ATTRIBUTE_HIDDEN
        except Exception:
            pass


# --------------------------------------------------------------------------
# Own icon, install location, shortcuts, uninstall
# (the builder imports these, so the installer and uninstaller always agree)
# --------------------------------------------------------------------------

APP_NAME = "Video Archiver"
APP_ID = "VideoArchiver"
INSTALL_DIR = Path(_local) / "Programs" / APP_NAME
INSTALL_EXE = INSTALL_DIR / (APP_NAME + ".exe")
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall" + "\\" + APP_ID
APP_BLURB = "Save videos from YouTube and 1000+ sites, and archive them on archive.org"

# The app's icon: a teal floppy disk with an upload arrow.
ICON_ROWS = ["KKKKKKKKKKKKKK..",
             "KTTSSSSSSSSTTTK.",
             "KTTSSKKSSSSTTTTK",
             "KTTSSKKSSSSTTTTK",
             "KTTSSSSSSSSTTTTK",
             "KTTTTTTTTTTTTTTK",
             "KTTTTTTTTTTTTTTK",
             "KTWWWWWWWWWWWWTK",
             "KTWWWWWAAWWWWWTK",
             "KTWWWWAAAAWWWWTK",
             "KTWWWAAAAAAWWWTK",
             "KTWWAAAAAAAAWWTK",
             "KTWWWWWAAWWWWWTK",
             "KTWWWWWAAWWWWWTK",
             "KTWWWWWWWWWWWWTK",
             "KKKKKKKKKKKKKKKK"]
ICON_COLORS = {"K": "#000000", "T": "#008080", "S": "#C0C0C0", "W": "#FFFFFF", "A": "#007000"}


def _rgb(hex_color):
    return tuple(int(hex_color[i:i + 2], 16) for i in (1, 3, 5))


def icon_png(scale):
    """The icon as PNG bytes, scaled up with crisp pixels."""
    import struct
    import zlib
    size = len(ICON_ROWS) * scale
    raw = bytearray()
    for row in ICON_ROWS:
        line = bytearray([0])
        for ch in row:
            line += (bytes([*_rgb(ICON_COLORS[ch]), 255]) if ch in ICON_COLORS else b"\0\0\0\0") * scale
        raw += bytes(line) * scale

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))


def _icon_dib(scale):
    """Classic 32-bit bitmap icon image (best for the small sizes)."""
    import struct
    size = len(ICON_ROWS) * scale
    pixels, mask_row = bytearray(), ((size + 31) // 32) * 4
    mask = bytearray(mask_row * size)
    for y in range(size - 1, -1, -1):          # bitmaps are stored bottom-up
        row = ICON_ROWS[y // scale]
        for x in range(size):
            ch = row[x // scale]
            if ch in ICON_COLORS:
                r, g, b = _rgb(ICON_COLORS[ch])
                pixels += bytes([b, g, r, 255])
            else:
                pixels += b"\0\0\0\0"
                mask[(size - 1 - y) * mask_row + x // 8] |= 0x80 >> (x % 8)
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, len(pixels) + len(mask), 0, 0, 0, 0)
    return header + bytes(pixels) + bytes(mask)


def icon_ico():
    """A Windows .ico with 16, 32, 48 and 256 pixel versions."""
    import struct
    images = [(16, _icon_dib(1)), (32, _icon_dib(2)), (48, _icon_dib(3)), (256, icon_png(16))]
    out, offset = bytearray(struct.pack("<HHH", 0, 1, len(images))), 6 + 16 * len(images)
    for size, data in images:
        out += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    for _, data in images:
        out += data
    return bytes(out)


def known_folder(name, fallback):
    if sys.platform == "win32":
        try:
            r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                                f"[Environment]::GetFolderPath('{name}')"],
                               capture_output=True, text=True, timeout=60, creationflags=NO_WINDOW)
            if r.stdout.strip():
                return Path(r.stdout.strip())
        except Exception:
            pass
    return fallback


def app_shortcuts():
    """Where the desktop and Start menu shortcuts live."""
    return [known_folder("Desktop", Path.home() / "Desktop") / f"{APP_NAME}.lnk",
            known_folder("Programs", Path.home()) / f"{APP_NAME}.lnk"]


def _ps(script, **env):
    """Run PowerShell with paths passed as environment variables (no quoting trouble)."""
    return subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                          capture_output=True, text=True, timeout=60, creationflags=NO_WINDOW,
                          env={**os.environ, **env})


def make_shortcut(lnk, target):
    try:
        lnk.parent.mkdir(parents=True, exist_ok=True)
        r = _ps("$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:YTA_LNK);"
                "$s.TargetPath=$env:YTA_EXE; $s.WorkingDirectory=$env:YTA_DIR;"
                "$s.IconLocation=$env:YTA_EXE + ',0'; $s.Description=$env:YTA_DESC; $s.Save()",
                YTA_LNK=str(lnk), YTA_EXE=str(target), YTA_DIR=str(Path(target).parent),
                YTA_DESC=APP_BLURB)
        return r.returncode == 0 and lnk.exists()
    except Exception:
        return False


def shortcut_is_ours(lnk, exe=None):
    """True only if the shortcut points at the installed app (or at exe)."""
    if not lnk.exists() or sys.platform != "win32":
        return False
    try:
        target = _ps("(New-Object -ComObject WScript.Shell).CreateShortcut($env:YTA_LNK).TargetPath",
                     YTA_LNK=str(lnk)).stdout.strip()
        return bool(target) and os.path.normcase(target) == os.path.normcase(str(exe or INSTALL_EXE))
    except Exception:
        return False


OLD_APP_NAME = "YT Archiver"
OLD_INSTALL_DIR = Path(_local) / "Programs" / OLD_APP_NAME
OLD_INSTALL_EXE = OLD_INSTALL_DIR / (OLD_APP_NAME + ".exe")
OLD_UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall" + "\\" + "YTArchiver"


def remove_old_install(say=lambda text: None, desktop=None, programs=None, is_ours=None):
    """Remove the app as installed under its old name, YT Archiver: its shortcuts (only
    if they point at the old exe), its Settings > Apps entry and the old exe. Its
    settings are moved, not deleted (migrate_old_data). Returns what couldn't be removed."""
    is_ours = is_ours or shortcut_is_ours
    desktop = desktop or known_folder("Desktop", Path.home() / "Desktop")
    programs = programs or known_folder("Programs", Path.home())
    left = []
    for lnk in (desktop / f"{OLD_APP_NAME}.lnk", programs / f"{OLD_APP_NAME}.lnk"):
        if is_ours(lnk, OLD_INSTALL_EXE):
            try:
                lnk.unlink()
                say(f"      Removed the old shortcut {lnk.name}")
            except OSError:
                left.append(str(lnk))
    bare = desktop / f"{OLD_APP_NAME}.exe"          # the very first version put this on the desktop
    for exe in (bare, OLD_INSTALL_EXE):
        if exe.is_file():
            try:
                exe.unlink()
                say(f"      Removed the old {exe.name}")
            except OSError:
                left.append(str(exe))
    try:
        OLD_INSTALL_DIR.rmdir()                     # only if nothing else is in it
    except OSError:
        pass
    if sys.platform == "win32":
        import winreg
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, OLD_UNINSTALL_KEY)
            say("      Removed YT Archiver from Settings > Apps")
        except FileNotFoundError:
            pass
        except OSError:
            left.append("YT Archiver in Settings > Apps")
    return left


def uninstall_files():
    """Remove shortcuts, the Apps entry, tools, settings and keys, then the exe
    itself once this program has closed. Downloads and uploads are left alone.
    Returns a list of things that couldn't be removed."""
    global _settings_frozen
    _settings_frozen = True
    problems = []
    try:
        (plugins_dir() / POT_PLUGIN_NAME).unlink()
    except FileNotFoundError:
        pass
    except OSError:
        problems.append(str(plugins_dir() / POT_PLUGIN_NAME))
    for lnk in app_shortcuts():
        if shortcut_is_ours(lnk):
            try:
                lnk.unlink()
            except OSError:
                problems.append(str(lnk))
    if sys.platform == "win32":
        import winreg
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
        except FileNotFoundError:
            pass
        except OSError:
            problems.append("the entry in Settings > Apps")
    # Only ever delete our own default data folder, never a custom YTD_DATA_DIR.
    if DATA_DIR.name == APP_ID and not os.environ.get("YTD_DATA_DIR"):
        _rmtree_retry(DATA_DIR)
        if DATA_DIR.exists():
            problems.append(str(DATA_DIR))
    if sys.platform == "win32" and INSTALL_EXE.exists():
        # A running exe can't delete itself: a hidden helper waits until it has
        # closed. The folder is only removed if nothing else is in it.
        script = ("Start-Sleep -Seconds 2; for($i=0; $i -lt 60; $i++){ try {"
                  " if(Test-Path -LiteralPath $env:YTA_EXE){ Remove-Item -LiteralPath $env:YTA_EXE -Force -ErrorAction Stop };"
                  " break } catch { Start-Sleep -Seconds 1 } };"
                  " try { [IO.Directory]::Delete($env:YTA_DIR) } catch {}")
        try:
            subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                             env={**os.environ, "YTA_EXE": str(INSTALL_EXE), "YTA_DIR": str(INSTALL_DIR)},
                             creationflags=NO_WINDOW | 0x00000200,  # new process group
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, close_fds=True)
        except OSError:
            problems.append(str(INSTALL_EXE))
    return problems


UNINSTALL_QUESTION = (f"Uninstall {APP_NAME}?\n\nThis removes the app, its shortcuts, its built-in "
                      "tools, its settings and your saved archive.org keys from this computer.\n\n"
                      "Your downloaded files and your archive.org uploads "
                      "are not touched.")


def uninstall_report(problems):
    if not problems:
        return f"{APP_NAME} has been removed."
    return (f"{APP_NAME} has been removed, except for these, which you can delete by hand:\n\n"
            + "\n".join(problems))


def run_uninstall():
    """Started by Settings > Apps > Uninstall (the exe with --uninstall)."""
    import tkinter as tk
    from tkinter import messagebox
    root = tk.Tk()
    root.withdraw()
    if not acquire_single_instance():
        messagebox.showinfo(APP_NAME, f"Close {APP_NAME} first, then uninstall again.")
    elif messagebox.askyesno(f"Uninstall {APP_NAME}", UNINSTALL_QUESTION, icon="warning"):
        messagebox.showinfo(APP_NAME, uninstall_report(uninstall_files()))
    root.destroy()


class Cancelled(Exception):
    pass


# --------------------------------------------------------------------------
# archive.org: keys, items, uploads (plain S3-style API, no extra packages)
# --------------------------------------------------------------------------

IA_S3_HOST = "s3.us.archive.org"
IA_KEYS_PAGE = "https://archive.org/account/s3.php"
IA_KEYS_FILE = DATA_DIR / "ia_keys.bin"
IA_UA = "Video-Archiver/1.0"
IA_MAX_HEADER = 6000        # keep each metadata header well under server limits
IA_MAX_TAGS = 25          # archive.org "subject" (topic) fields per item
IA_RETRIES = 4              # archive.org answers 503 "slow down" when it's busy
META_FIELDS = ("id", "title", "uploader", "channel", "upload_date", "description",
               "webpage_url", "tags", "categories", "language", "thumbnail",
               # which site, so items and topics say "Vimeo", "TikTok", ...
               "extractor_key", "webpage_url_domain",
               # topic-rich fields some sites fill (SoundCloud, Bandcamp, TV/news sites...)
               "genres", "genre", "artists", "artist", "album", "series", "cast", "location")


def meta_print_args(stage=""):
    """--print args that output one '@@M field <json>' line per field ("NA" if missing)."""
    args = []
    for field in META_FIELDS:
        args += ["--print", f"{stage}@@M {field} %({field})j"]
    return args


def parse_meta_line(line, into):
    field, _, value = line[4:].partition(" ")
    if field in META_FIELDS:
        try:
            into[field] = json.loads(value)
        except ValueError:
            pass  # "NA": yt-dlp doesn't know this field for the video


class IAError(Exception):
    def __init__(self, msg, rejected=False):
        super().__init__(msg)
        self.rejected = rejected  # True when archive.org said the keys are wrong
        self.account = False      # True when archive.org refuses the whole account


def _dpapi(data, protect):
    """Encrypt/decrypt with Windows DPAPI: only your Windows account can read it back."""
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    fn.argtypes = [ctypes.POINTER(BLOB), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                   ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(BLOB)]
    fn.restype = wintypes.BOOL
    k32.LocalFree.argtypes = [ctypes.c_void_p]
    buf = ctypes.create_string_buffer(data, len(data))
    src = BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = BLOB()
    if not fn(ctypes.byref(src), None, None, None, None, 0x1, ctypes.byref(out)):  # UI_FORBIDDEN
        raise OSError(ctypes.get_last_error(), "Windows couldn't protect/unprotect the keys")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        k32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


def save_ia_keys(access, secret):
    raw = json.dumps({"access": access, "secret": secret}).encode("utf-8")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        IA_KEYS_FILE.write_bytes(_dpapi(raw, True))
    else:
        IA_KEYS_FILE.write_bytes(raw)
        os.chmod(IA_KEYS_FILE, 0o600)


def load_ia_keys():
    """(access, secret) or None."""
    try:
        raw = IA_KEYS_FILE.read_bytes()
        if sys.platform == "win32":
            raw = _dpapi(raw, False)
        d = json.loads(raw.decode("utf-8"))
        if d.get("access") and d.get("secret"):
            return d["access"], d["secret"]
    except Exception:
        pass
    return None


def forget_ia_keys():
    try:
        IA_KEYS_FILE.unlink()
    except OSError:
        pass


def _ia_auth(keys):
    return f"LOW {keys[0]}:{keys[1]}"


def clean_prefix(text):
    """Letters, digits, dot, dash, underscore; starts with a letter or digit."""
    p = re.sub(r"[^A-Za-z0-9._-]+", "-", text or "").strip("-._").lower()
    return p[:60].strip("-._")


def ia_identifier(prefix, vid, site="youtube"):
    """<prefix>-<id> for YouTube (so earlier uploads are found again), and
    <prefix>-<site>-<id> for other sites, whose ids can clash (Vimeo 123 vs Twitch 123)."""
    prefix = prefix or "yt"
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", str(vid)) or "video"   # YouTube ids pass unchanged
    if site and site != "youtube":
        ident = f"{prefix}-{site}-{safe}"
    else:
        ident = f"{prefix}-{safe}"
    if safe != str(vid) or len(ident) > 100:
        # odd characters were replaced or the name is too long: a short fingerprint
        # of the real id keeps two different videos from ending up in one item
        tag = hashlib.md5(str(vid).encode("utf-8")).hexdigest()[:8]
        ident = ident[:100 - 9].rstrip("._-") + "-" + tag
    if not ident[0].isalnum():
        ident = "v" + ident
    return ident[:100]


def ia_check_auth(keys):
    """Ask archive.org whether the keys work. Returns (screen name, account email)."""
    req = urllib.request.Request(f"https://{IA_S3_HOST}/?check_auth=1",
                                 headers={"Authorization": _ia_auth(keys), "User-Agent": IA_UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            info = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise IAError("archive.org didn't accept these keys. Copy both again from "
                          + IA_KEYS_PAGE, rejected=True)
        raise IAError(f"archive.org answered with error {exc.code}.")
    except (OSError, ValueError) as exc:
        raise IAError(f"Couldn't reach archive.org ({getattr(exc, 'reason', exc)}).")
    if not isinstance(info, dict) or "authorized" not in info:
        raise IAError("archive.org gave an answer this app doesn't understand.")
    if not info.get("authorized"):
        raise IAError("archive.org didn't accept these keys. Copy both again from "
                      + IA_KEYS_PAGE, rejected=True)
    return str(info.get("screenname") or ""), str(info.get("username") or "")


def ia_item(identifier):
    """The item's metadata record, or {} if the identifier is free."""
    req = urllib.request.Request("https://archive.org/metadata/" + urllib.parse.quote(identifier),
                                 headers={"User-Agent": IA_UA})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except (OSError, ValueError) as exc:
        raise IAError(f"Couldn't check archive.org ({getattr(exc, 'reason', exc)}).")
    return data if isinstance(data, dict) else {}


def _uri(value):
    return "uri(" + urllib.parse.quote(value, safe="") + ")"


# ---- which site a video comes from ----

SITE_NAMES = {"youtube": "YouTube", "tiktok": "TikTok", "vimeo": "Vimeo", "soundcloud": "SoundCloud",
              "twitch": "Twitch", "bandcamp": "Bandcamp", "twitter": "Twitter", "instagram": "Instagram",
              "facebook": "Facebook", "dailymotion": "Dailymotion", "bilibili": "Bilibili",
              "niconico": "Niconico", "reddit": "Reddit", "rumble": "Rumble", "odysee": "Odysee",
              "lbry": "Odysee", "peertube": "PeerTube", "mixcloud": "Mixcloud", "bitchute": "BitChute",
              "kick": "Kick", "vk": "VK", "streamable": "Streamable", "imgur": "Imgur",
              "tumblr": "Tumblr", "pinterest": "Pinterest", "bluesky": "Bluesky", "archiveorg": "",
              "applepodcasts": "Apple Podcasts", "spotify": "Spotify", "ted": "TED",
              "nebula": "Nebula", "floatplane": "Floatplane", "kickstarter": "Kickstarter",
              "coub": "Coub", "weibo": "Weibo", "douyin": "Douyin", "xiaohongshu": "Xiaohongshu",
              "nhk": "NHK", "bbc": "BBC", "ard": "ARD", "zdf": "ZDF", "arte": "Arte", "cbc": "CBC"}


def site_key(info):
    """'youtube', 'vimeo', 'tiktok'... from yt-dlp's extractor name; the domain for
    sites handled by the generic extractor; '' if unknown."""
    key = str(info.get("extractor_key") or info.get("ie_key") or "").lower()
    if key and key != "generic":
        key = re.sub(r"[^a-z0-9]", "", key)
        for name in sorted(SITE_NAMES, key=len, reverse=True):   # "twitchvod" -> "twitch"
            if key.startswith(name):
                return name
        return key[:20]
    domain = str(info.get("webpage_url_domain") or "")
    if not domain:
        m = re.match(r"https?://([^/?#]+)", str(info.get("webpage_url") or info.get("url") or ""))
        domain = m.group(1) if m else ""
    domain = re.sub(r"^(www|m)\.", "", domain.lower().split("@")[-1].split(":")[0])
    labels = domain.split(".")
    if "youtu" in labels or "youtube" in labels or "youtube-nocookie" in labels:
        return "youtube"                  # youtu.be, music.youtube.com, m.youtube.com...
    for label in labels[:-1]:             # vimeo.com -> vimeo, like yt-dlp's own name
        if label in SITE_NAMES:
            return label
    return re.sub(r"[^a-z0-9]+", "-", domain).strip("-")[:30]


def site_label(info):
    """Human name of the site for topics ('' when it wouldn't help anyone)."""
    key = site_key(info)
    if key in SITE_NAMES:
        return SITE_NAMES[key]
    raw = str(info.get("extractor_key") or info.get("ie_key") or "")
    if raw and raw.lower() != "generic":
        return re.sub(r"(Vod|Video|Videos|Clips?|Post|Live|Stream|Embed|Track|Playlist|Album|IE)$", "", raw) or raw
    return ""


def is_youtube(info):
    return site_key(info) == "youtube"


# ---- leaving the source site out of what gets published ----

SITE_DOMAINS = {"youtube": ("youtube.com", "youtu.be", "youtube-nocookie.com"), "tiktok": ("tiktok.com",),
                "vimeo": ("vimeo.com",), "soundcloud": ("soundcloud.com", "snd.sc"), "twitch": ("twitch.tv",),
                "bandcamp": ("bandcamp.com",), "twitter": ("twitter.com", "x.com"),
                "instagram": ("instagram.com", "instagr.am"), "facebook": ("facebook.com", "fb.com", "fb.watch"),
                "dailymotion": ("dailymotion.com", "dai.ly"), "bilibili": ("bilibili.com", "b23.tv"),
                "reddit": ("reddit.com", "redd.it"), "niconico": ("nicovideo.jp", "nico.ms"),
                "rumble": ("rumble.com",), "odysee": ("odysee.com",), "mixcloud": ("mixcloud.com",),
                "bitchute": ("bitchute.com",), "kick": ("kick.com",), "vk": ("vk.com", "vkvideo.ru"),
                "streamable": ("streamable.com",), "imgur": ("imgur.com",), "tumblr": ("tumblr.com",),
                "pinterest": ("pinterest.com", "pin.it"), "bluesky": ("bsky.app",),
                "spotify": ("spotify.com", "spotify.link"), "ted": ("ted.com",), "nebula": ("nebula.tv",),
                "floatplane": ("floatplane.com",)}

# Hashtags that only exist on one site, so they give it away
SITE_HASHTAGS = {"youtube": ("shorts", "short", "ytshorts", "youtubeshorts", "yt", "youtuber"),
                 "tiktok": ("fyp", "foryou", "foryoupage", "fy", "fypシ", "tiktokviral", "tiktoker"),
                 "instagram": ("reels", "reel", "instareels", "reelsinstagram", "instagood"),
                 "twitch": ("twitchclips", "twitchstreamer"), "facebook": ("fbreels",)}

# Lines that only advertise the source site ("Subscribe to my channel!", "Follow me on TikTok")
_PROMO_RE = re.compile(r"(?i)\b(subscrib\w*|abonnier\w*|follow\w*|folg\w*|channel|kanal|"
                       r"watch\s+(?:more|the\s+full|it)|check\s+(?:out|me|us)|link\s+in\s+(?:bio|description)|"
                       r"like\s+(?:and|&)|notifications?|glocke|bell|support\s+(?:me|us)|playlist)\b")


def _site_domains(info):
    doms = set(SITE_DOMAINS.get(site_key(info), ()))
    for src in (info.get("webpage_url_domain"), info.get("webpage_url")):
        m = re.match(r"(?:[a-z]+://)?([^/?#:\s]+)", str(src or ""), re.I)
        if m:
            d = re.sub(r"^(www|m)\.", "", m.group(1).lower())
            if "." in d:
                doms.add(d)
    return doms


def _site_names(info):
    names = {n for n in (SITE_NAMES.get(site_key(info)), site_label(info)) if n}
    if site_key(info) == "youtube":
        names.add("YouTube")
    return names


def site_topic_keys(info):
    keys = {_topic_key(n) for n in _site_names(info)} | {_topic_key(d) for d in _site_domains(info)}
    keys |= {k + "shorts" for k in keys} | {k + "video" for k in keys}
    keys |= {_topic_key(h) for h in SITE_HASHTAGS.get(site_key(info), ())}
    return {k for k in keys if k}


def _site_patterns(info):
    doms, names = _site_domains(info), _site_names(info)
    url_re = name_re = tag_re = None
    if doms:
        alt = "|".join(re.escape(d) for d in sorted(doms, key=len, reverse=True))
        # also takes a word like "at"/"here" right before the link with it
        url_re = re.compile(r"(?i)(?:\b(?:at|on|via|here|from|unter|auf|bei|hier)\s+)?(?<![\w@.-])"
                            r"(?:https?://)?(?:[\w-]+\.)*(?:" + alt + r")(?::\d+)?(?:/\S*)?")
    if names:
        alt = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
        # only attribution phrases: "on YouTube", "from my TikTok channel", "auf YouTube".
        # "History of YouTube" is left alone: there the site *is* the subject.
        name_re = re.compile(r"(?i)\s*\b(?:on|to|from|via|at|in|auf|bei|von|im)\s+(?:the\s+|my\s+|our\s+|meinem\s+|unserem\s+)?"
                             r"(?:" + alt + r")(?:'s|’s)?(?:\s+(?:channel|kanal|account|page|profile|shorts|videos?))?\b")
    words = [re.escape(n.replace(" ", "")) for n in names]
    words += [re.escape(h) for h in SITE_HASHTAGS.get(site_key(info), ())]
    if words:
        tag_re = re.compile(r"(?i)\s*#(?:" + "|".join(sorted(words, key=len, reverse=True)) + r")(?:shorts)?(?![\w])")
    return url_re, name_re, tag_re, names


def _tidy(text):
    text = re.sub(r"\(\s*\)|\[\s*\]", "", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\s+([.,!?;:])", r"\1", text)
    return text.strip()


def scrub_text(text, info):
    """Remove links to the source site, its hashtag, 'on <site>' phrases and lines that
    only promote the site. Everything else in the text stays as it was."""
    url_re, name_re, tag_re, names = _site_patterns(info)
    if not text or not (url_re or names):
        return text or ""
    name_any = re.compile(r"(?i)\b(?:" + "|".join(re.escape(n) for n in names) + r")\b") if names else None
    out = []
    for line in str(text).split("\n"):
        had_url = bool(url_re and url_re.search(line))
        mentions = had_url or bool(name_any and name_any.search(line)) or bool(tag_re and tag_re.search(line))
        if not mentions:
            out.append(line)
            continue
        cleaned = line
        for rx in (url_re, tag_re, name_re):
            if rx:
                cleaned = rx.sub("", cleaned)
        cleaned = _tidy(cleaned)
        words = re.findall(r"[^\W\d_]{2,}", cleaned)
        if _PROMO_RE.search(line) and len(cleaned) < 160:
            continue                       # "Subscribe to my YouTube channel!"
        if had_url and re.search(r"[:\-–—→➡👉⬇↓>]\s*$", cleaned):
            continue                       # "Original video:" whose link was removed
        if len(words) < 2:
            continue                       # nothing left but a label or emoji
        out.append(cleaned)
    result = "\n".join(out)
    return re.sub(r"\n{3,}", "\n\n", result).strip()


def scrub_title(title, info):
    """'My Video - YouTube', '[TikTok] My Video', 'My Video #shorts #youtube' -> 'My Video'."""
    title = str(title or "")
    url_re, name_re, tag_re, names = _site_patterns(info)
    t = title
    if url_re:
        t = url_re.sub("", t)
    if tag_re:
        t = tag_re.sub("", t)
    for n in sorted(names, key=len, reverse=True):
        alt = re.escape(n)
        t = re.sub(r"(?i)^\s*(?:\[\s*" + alt + r"[^\]]*\]|\(\s*" + alt + r"[^)]*\)|" + alt + r")\s*[-|–—•:·]?\s+", "", t)
        t = re.sub(r"(?i)\s*(?:[-|–—•:·]\s*" + alt + r"(?:\s+(?:shorts|video|clip|live|vod))?|\(\s*" + alt + r"[^)]*\)|\[\s*"
                   + alt + r"[^\]]*\])\s*$", "", t)
        t = re.sub(r"(?i)\s+(?:on|auf)\s+" + alt + r"\s*$", "", t)
    t = _tidy(re.sub(r"\s*[-|–—•:·]\s*$", "", t))
    return t or title


def scrub_description_html(desc, info):
    """The same cleaning for a description already on archive.org (lines joined with <br />)."""
    lines = re.split(r"(?i)<br\s*/?>|\n", str(desc or ""))
    return scrub_text("\n".join(lines), info).replace("\n", "<br />")


def prepare_meta(meta, hide_source=False, author=""):
    """The video's details as they'll be published: author applied, site left out."""
    m = dict(meta)
    if author:
        m["_author"] = author
    if hide_source:
        m["_hide_site"] = True
        m["title"] = scrub_title(meta.get("title"), meta)
        if meta.get("description") is not None:
            m["description"] = scrub_text(plain_text(meta.get("description")), meta)
        site = site_topic_keys(meta)
        m["tags"] = [t for t in _as_list(meta.get("tags")) if _topic_key(t) not in site]
    return m


# ---- topics: what makes an item easy to find on archive.org ----

# Words that say nothing about what a video is about (English + German),
# plus the usual YouTube boilerplate from descriptions.
STOPWORDS = frozenset("""
a about above after again against all almost also always am an and any are aren't around as at
be because been before being below between both but by can can't cannot could couldn't did didn't
do does doesn't doing don't down during each even ever every few for from further get gets getting
got had hadn't has hasn't have haven't having he her here hers herself him himself his how i if in
into is isn't it it's its itself just let's like made make makes making many me more most much must
my myself never new no nor not now of off often on once one only or other our ours ourselves out
over own part really same she should shouldn't so some still such than that that's the their theirs
them themselves then there there's these they they're this those through to too under until up us
very was wasn't way we we're were weren't what what's when where which while who whom why will with
won't would wouldn't yes yet you you're your yours yourself yourselves
thing things lot lots stuff something anything everything nothing someone everyone way ways kind
really actually basically literally maybe well okay ok yeah hey hi hello guys everybody today
tonight tomorrow yesterday week weeks year years day days time times first last next best good
great big little look looking see seen watch watching want know think going gonna wanna use used
using try trying need take come back show shows put give find found keep go goes
video videos vid vids official channel channels subscribe subscribed subscribers subscriber sub subs
like likes liked comment comments follow following instagram insta twitter tiktok facebook patreon
discord twitch merch shop store link links linked below above click description http https www com
net org html shorts short clip clips full episode episodes ep part pt ft feat featuring prod
sponsor sponsored sponsors code discount affiliate thanks thank please support enjoy enjoyed
free check out email business inquiries contact social media playlist upload uploaded uploads
channel's youtube youtu
aber alle allem allen aller alles als also am an ander andere anderen anderem anderer anderes auch auf
aus bei bin bis bist da damit dann das dass dein deine deinem deinen deiner dem den denn der des dich
die dies diese diesem diesen dieser dieses dir doch dort du durch ein eine einem einen einer eines
einfach er es etwas euch euer eure für gegen gibt hab habe haben hat hatte hier hin hinter ich ihm
ihn ihnen ihr ihre ihrem ihren ihrer im in ins ist ja jetzt kann kein keine keinem keinen keiner
können mal man mehr mein meine meinem meinen meiner mich mir mit muss nach nicht nichts noch nun nur
ob oder ohne schon sehr sein seine seinem seinen seiner sich sie sind so soll über um und uns unser
unsere unter viel vom von vor war waren was weil welche wenn wer werden wie wieder will wir wird wo
wurde zu zum zur zwischen heute neue neuen neues teil folge abonnieren kanal danke
""".split())

# YouTube categories too broad to help anyone find a video
GENERIC_CATEGORIES = {"people & blogs", "people and blogs"}

LANG_NAMES = {"en": "English", "de": "German", "fr": "French", "es": "Spanish", "it": "Italian",
              "pt": "Portuguese", "nl": "Dutch", "pl": "Polish", "sv": "Swedish", "da": "Danish",
              "no": "Norwegian", "nb": "Norwegian", "fi": "Finnish", "cs": "Czech", "sk": "Slovak",
              "hu": "Hungarian", "ro": "Romanian", "el": "Greek", "tr": "Turkish", "ru": "Russian",
              "uk": "Ukrainian", "ar": "Arabic", "he": "Hebrew", "fa": "Persian", "hi": "Hindi",
              "bn": "Bengali", "ur": "Urdu", "id": "Indonesian", "ms": "Malay", "th": "Thai",
              "vi": "Vietnamese", "zh": "Chinese", "ja": "Japanese", "ko": "Korean", "tl": "Tagalog",
              "fil": "Filipino", "sw": "Swahili", "ca": "Catalan", "hr": "Croatian", "sr": "Serbian",
              "bg": "Bulgarian", "lt": "Lithuanian", "lv": "Latvian", "et": "Estonian"}

_URL_RE = re.compile(r"https?://\S+|www\.\S+|\S+@\S+\.\w+")
_HASHTAG_RE = re.compile(r"(?<![\w&/])#(\w{2,40})")
_WORD_RE = re.compile(r"[^\W_][\w'’&+.-]*[^\W_]|[^\W\d_]")
_TIMESTAMP_RE = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b")


def _topic_key(text):
    """'Pokémon TCG', 'pokemon-tcg' and '#PokemonTCG' count as the same topic."""
    import unicodedata
    t = unicodedata.normalize("NFKD", str(text).casefold())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"[\W_]+", "", t)


def clean_topic(text):
    t = re.sub(r"\s+", " ", str(text or "")).strip().lstrip("#").strip(" -_.,;:|/\\\"'")
    t = t.replace(";", ",")      # archive.org treats ";" as a topic separator
    if not 2 <= len(t) <= 60 or not re.search(r"[^\W\d_]", t):
        return ""
    return t


def parse_topics(text):
    """Topics typed by the user: comma, semicolon or line separated."""
    return [t for t in (clean_topic(x) for x in re.split(r"[,;\n]+", text or "")) if t]


def split_hashtag(tag):
    """#PokemonTCG -> 'Pokemon TCG'; #booster_opening -> 'booster opening'."""
    t = tag.replace("_", " ")
    t = re.sub(r"(?<=[a-zäöüß])(?=[A-ZÄÖÜ])", " ", t)
    t = re.sub(r"(?<=[A-ZÄÖÜ])(?=[A-ZÄÖÜ][a-zäöüß])", " ", t)
    return t.strip()


def hashtags(text):
    found = []
    for tag in _HASHTAG_RE.findall(_URL_RE.sub(" ", text or "")):
        if re.search(r"[^\W\d_]", tag):
            found.append(split_hashtag(tag))
    return found


def _is_content_word(word):
    lw = word.casefold().strip("'’.-")
    if lw in STOPWORDS or lw.rstrip("s") in STOPWORDS or lw.isdigit():
        return False
    return len(lw) >= 3 or (len(word) >= 2 and word.isupper())   # keep "AI", "TV", "F1"


def extract_keywords(title, description, limit=6, single_words=True):
    """Most telling words and phrases (up to 5 words, e.g. 'James Webb Space Telescope').
    The title counts three times as much as the description; anything only in the
    description has to come up at least twice (three times for a single word)."""
    def tokens(text):
        text = _URL_RE.sub(" | ", text)
        text = _HASHTAG_RE.sub(" | ", text)
        text = _TIMESTAMP_RE.sub(" | ", text)
        out = []
        for piece in re.split(r"[|()\[\]{}<>\"“”„«»!?:;,/\n\r\t–—•·]+|\.(?=\s|$)", text):
            out.extend(_WORD_RE.findall(piece))
            out.append(None)               # phrases never cross punctuation
        return out

    score, forms = {}, {}

    def seen_form(key, form):
        forms.setdefault(key, {}).setdefault(form, 0)
        forms[key][form] += 1

    def feed(toks, weight):
        run = []                           # the current stretch of content words
        for w in toks + [None]:
            if w is not None and _is_content_word(w):
                run.append(w.strip("'’.-"))
                continue
            for i, word in enumerate(run):
                score[word.casefold()] = score.get(word.casefold(), 0) + weight
                seen_form(word.casefold(), word)
                if i:
                    pair = run[i - 1] + " " + word
                    score[pair.casefold()] = score.get(pair.casefold(), 0) + weight * 1.5
                for n in range(2, 6):      # remember longer phrases to join pairs later
                    if i + 1 >= n:
                        phrase = " ".join(run[i + 1 - n:i + 1])
                        seen_form(phrase.casefold(), phrase)
            run = []

    feed(tokens(title or ""), 3)
    feed(tokens((description or "")[:5000]), 1)

    # 1) strong word pairs, joined into longer phrases where they overlap in the text
    phrases = []
    for key in sorted((k for k in score if " " in k and score[k] >= 4.5), key=lambda k: (-score[k], k))[:12]:
        words = key.split(" ")
        for i, p in enumerate(phrases):
            if p[-1] == words[0] and " ".join(p + words[1:]) in forms:
                phrases[i] = p + words[1:]
                break
            if p[0] == words[-1] and " ".join(words[:-1] + p) in forms:
                phrases[i] = words[:-1] + p
                break
        else:
            phrases.append(words)
    joined = True
    while joined:                           # pairs picked in any order can still chain
        joined = False
        for i, p in enumerate(phrases):
            for j, q in enumerate(phrases):
                if i != j and p[-1] == q[0] and " ".join(p + q[1:]) in forms:
                    phrases[i], joined = p + q[1:], True
                    del phrases[j]
                    break
            if joined:
                break
    phrase_score = {" ".join(p): max(score.get(" ".join(p[k:k + 2]), 0) for k in range(len(p) - 1))
                    for p in phrases if len(p) <= 5}
    picked = sorted(phrase_score, key=lambda k: -phrase_score[k])[:max(1, limit // 2 + 1)]

    # 2) single words that aren't already part of a picked phrase. A word has to show
    #    up in the title AND the description (or 4x in the description); with no real
    #    description, the title's words are all there is to go on.
    in_phrases = {w for p in picked for w in p.split(" ")}
    min_word = 3 if len((description or "").strip()) < 100 else 4
    for key in sorted((k for k in score if single_words and " " not in k and score[k] >= min_word),
                      key=lambda k: (-score[k], k)):
        if len(picked) >= limit:
            break
        if key not in in_phrases:
            picked.append(key)

    def best_form(key):
        variants = forms.get(key) or {key: 1}
        return max(variants, key=lambda f: (variants[f], f != f.lower()))
    return [best_form(k) for k in picked[:limit]]


def _as_list(value, split=False):
    """yt-dlp gives lists for most fields, but a plain string on some sites."""
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple)):
        return [v for v in value if v not in (None, "")]
    if split:
        return [v for v in re.split(r"\s*[,;/]\s*", str(value)) if v]
    return [value]


def plain_text(desc):
    """Some sites send descriptions as HTML; archive.org should get readable text."""
    desc = str(desc or "")
    if re.search(r"</?(p|br|a|div|span|b|i|em|strong|ul|li)\b[^>]*>", desc, re.I):
        desc = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>", "\n", desc)
        desc = html.unescape(re.sub(r"<[^>]+>", "", desc))
    return desc.strip()


def build_topics(meta, extra=()):
    """Specific topics first, generic last. Duplicates (any spelling) are dropped."""
    out, seen = [], {}

    def add(topic):
        t = clean_topic(topic)
        k = _topic_key(t)
        if not t or not k:
            return
        if k not in seen:
            seen[k] = len(out)
            out.append(t)
        elif (t.count(" "), t != t.lower()) > (out[seen[k]].count(" "), out[seen[k]] != out[seen[k]].lower()):
            out[seen[k]] = t               # same topic, spelled apart or properly capitalised

    for t in extra:
        add(t)
    for c in _as_list(meta.get("categories")):
        if str(c).casefold() not in GENERIC_CATEGORIES:
            add(c)
    # music, TV and news sites: genre, artist, album, series, cast, place
    for g in _as_list(meta.get("genres")) or _as_list(meta.get("genre"), split=True):
        add(g)
    for a in _as_list(meta.get("artists")) or _as_list(meta.get("artist"), split=True):
        add(a)
    for key in ("album", "series", "location"):
        add(meta.get(key))
    for c in _as_list(meta.get("cast"))[:3]:
        add(c)
    title, desc = str(meta.get("title") or ""), plain_text(meta.get("description"))
    for h in hashtags(title + "\n" + desc)[:6]:
        add(h)
    channel = str(meta.get("uploader") or meta.get("channel") or "")
    author = str(meta.get("_author") or "")
    for t in [t for t in _as_list(meta.get("tags")) if isinstance(t, str)][:12]:
        # creators list the important tags first; later ones are often filler
        if _topic_key(t) not in (_topic_key(channel), _topic_key(author)):
            add(t)
    # Guessed single words are only worth it when the site gave little else
    for k in extract_keywords(title, desc, limit=6 if len(out) < 8 else 3, single_words=len(out) < 3):
        # "Pikachu" adds nothing next to "Pikachu ex"
        if " " not in k and _topic_key(k) in {_topic_key(w) for t in out for w in t.split()}:
            continue
        add(k)
    add(author or channel)                 # your author replaces the uploader's name
    if meta.get("_hide_site"):
        site, own = site_topic_keys(meta), {_topic_key(t) for t in extra}
        out = [t for t in out if _topic_key(t) not in site or _topic_key(t) in own]
    else:
        add(site_label(meta))
    return out[:IA_MAX_TAGS]


def ia_language(meta):
    code = str(meta.get("language") or "").split("-")[0].split("_")[0].lower()
    return LANG_NAMES.get(code, "")


def existing_topics(item):
    subject = (item.get("metadata") or {}).get("subject") or []
    if isinstance(subject, str):
        subject = [subject]
    out = []
    for s in subject:
        out += [x.strip() for x in str(s).split(";") if x.strip()]
    return out


def merge_topics(existing, new, limit=50):
    """Keep every topic already on the item (including ones added by hand), add new ones."""
    merged, seen = list(existing), {_topic_key(t) for t in existing}
    for t in new:
        k = _topic_key(t)
        if k and k not in seen and len(merged) < limit:
            seen.add(k)
            merged.append(t)
    return merged


def ia_update_metadata(keys, identifier, fields, remove=()):
    """Change (and remove) fields of an existing item through archive.org's metadata API."""
    patch = [{"op": "add", "path": "/" + k, "value": v} for k, v in fields.items()]
    patch += [{"op": "remove", "path": "/" + k} for k in remove]
    body = urllib.parse.urlencode({"-target": "metadata", "-patch": json.dumps(patch),
                                   "access": keys[0], "secret": keys[1]}).encode("utf-8")
    req = urllib.request.Request("https://archive.org/metadata/" + urllib.parse.quote(identifier),
                                 data=body, headers={"User-Agent": IA_UA})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            result = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        try:
            result = json.loads(exc.read().decode("utf-8", "replace"))
        except (OSError, ValueError):
            raise IAError(f"archive.org answered with error {exc.code}.")
    except (OSError, ValueError) as exc:
        raise IAError(f"Couldn't reach archive.org ({getattr(exc, 'reason', exc)}).")
    if not isinstance(result, dict):
        raise IAError("archive.org gave an answer this app doesn't understand.")
    if result.get("success") or "no changes" in str(result.get("error", "")).lower():
        return
    raise IAError("archive.org didn't take the new topics: " + str(result.get("error") or "no reason given"))


def ia_meta_headers(meta, mediatype, topics=None):
    """Turn yt-dlp's info into x-archive-meta-* headers (non-ASCII safe)."""
    h = {"x-archive-meta-mediatype": mediatype,
         "x-archive-meta-collection": (TRIAL_COLLECTION if meta.get("_trial") else
                                       "opensource_movies" if mediatype == "movies" else "opensource_audio")}

    def put(key, value):
        value = str(value or "").strip()
        if not value:
            return
        enc = _uri(value)
        while len(enc) > IA_MAX_HEADER and value:
            value = value[:int(len(value) * 0.8)].rstrip() + "…"
            enc = _uri(value)
        h[key] = enc

    put("x-archive-meta-title", meta.get("title"))
    artists = _as_list(meta.get("artists")) or _as_list(meta.get("artist"))
    creator = meta.get("uploader") or meta.get("channel")
    if mediatype == "audio" and artists:
        creator = ", ".join(str(a) for a in artists[:3])
    creator = meta.get("_author") or creator or (", ".join(str(a) for a in artists[:3]) if artists else "")
    put("x-archive-meta-creator", creator)
    d = str(meta.get("upload_date") or "")
    if re.fullmatch(r"\d{8}", d):
        put("x-archive-meta-date", f"{d[:4]}-{d[4:6]}-{d[6:]}")
    desc = plain_text(meta.get("description"))
    if desc:
        put("x-archive-meta-description", html.escape(desc).replace("\n", "<br />"))
    if not meta.get("_hide_site"):
        put("x-archive-meta-source", meta.get("webpage_url"))
        if meta.get("id") and site_key(meta):
            put("x-archive-meta-external-identifier", f"{site_key(meta)}:{meta['id']}")
    put("x-archive-meta-language", ia_language(meta))
    put("x-archive-meta-licenseurl", meta.get("_license"))
    if topics is None:
        topics = build_topics(meta)
    for i, tag in enumerate(topics[:IA_MAX_TAGS], 1):
        put(f"x-archive-meta{i:02d}-subject", tag)
    return h


def _ia_connection(host):
    # Generous timeout: after a big file, archive.org can take minutes to answer.
    return http.client.HTTPSConnection(host, timeout=600, blocksize=1 << 20)


class _ProgressReader:
    """File wrapper that reports upload progress and lets Stop abort the upload."""

    def __init__(self, f, total, on_progress, cancel):
        self.f, self.total, self.on_progress, self.cancel = f, total, on_progress, cancel
        self.done, self.t0, self.last = 0, time.monotonic(), 0.0

    def read(self, n=-1):
        if self.cancel.is_set():
            raise Cancelled()
        chunk = self.f.read(n if n and n > 0 else 1 << 20)
        self.done += len(chunk)
        now = time.monotonic()
        if now - self.last > 0.3 or not chunk:
            self.last = now
            speed = self.done / max(now - self.t0, 0.001)
            eta = (self.total - self.done) / speed if speed else None
            self.on_progress(self.done, self.total, speed, eta)
        return chunk


ACCOUNT_REFUSED = "\u200b"   # invisible marker: the refusal is about the account, not this item


def explain_denied(keys, identifier, remote, username=None, check_auth=None, get_item=None):
    """archive.org said "Access Denied": find out why and say what to do."""
    check_auth, get_item = check_auth or ia_check_auth, get_item or ia_item
    if username is None:
        username = load_settings().get("ia_username") or ""
    page = "https://archive.org/details/" + identifier
    what = f"archive.org refused {remote} for the item {page}"
    try:
        _, user = check_auth(keys)
        username = user or username
    except IAError as exc:
        if exc.rejected:
            return (f"{what}: it no longer accepts your keys. Get them again at {IA_KEYS_PAGE} and enter "
                    "them under Options > archive.org account.")
    try:
        item = get_item(identifier)
    except IAError:
        item = None
    if item:
        md = item.get("metadata") or {}
        if item.get("is_dark"):
            return (f"{what}: archive.org has hidden (\"darked\") this item, so nothing can be added to it. "
                    "That usually follows a rights complaint or a review; check your email from archive.org.")
        owner = str(md.get("uploader") or "")
        if owner and username and owner.lower() != str(username).lower():
            return (f"{what}: the item belongs to another account. Change your identifier prefix under "
                    "Options > archive.org account.")
        if not md and not item.get("files"):
            return (f"{what}: the item name exists but archive.org isn't showing it (removed or blocked). "
                    "Uploads to it aren't possible; if it was yours, ask info@archive.org about it.")
    if remote.startswith("__ia_"):
        return f"{what}: archive.org doesn't allow uploading a file with this name."
    return ACCOUNT_REFUSED + (f"{what}. Your keys work and the item is yours or free, so archive.org refused the account "
            "itself. That's usually a temporary limit (for example after many uploads in a short time): "
            "wait a few hours and upload fewer videos at once. If it keeps happening, email "
            f"info@archive.org with \"s3help\" in the subject and the item name {identifier}.")


def _refused(status, text, keys, identifier, remote):
    if status in (401, 403):
        msg = explain_denied(keys, identifier, remote)
        exc = IAError(msg.lstrip(ACCOUNT_REFUSED), rejected=True)
        exc.account = msg.startswith(ACCOUNT_REFUSED) or "no longer accepts your keys" in msg
        return exc
    return IAError(_s3_message(status, text))


def _s3_message(status, body):
    m = re.search(r"<Message>(.*?)</Message>", body, re.S)
    text = html.unescape(m.group(1).strip()) if m else body.strip()[:200]
    if status in (401, 403):
        return ("archive.org refused the upload" + (f": {text}" if text else "") +
                ". If the identifier belongs to someone else, change your identifier "
                "prefix under Options > archive.org account.")
    return f"archive.org error {status}" + (f": {text}" if text else "")


def _arrived(identifier, name, md5_hex):
    try:
        files = ia_item(identifier).get("files") or []
    except IAError:
        return False
    return any(f.get("name") == name and f.get("md5") == md5_hex for f in files)


def ia_upload(keys, identifier, path, meta, mediatype, on_progress, cancel, on_status=None,
              topics=None, remote_name=None):
    """Upload one file into the item. archive.org checks the MD5 before saying OK.
    Big files go in parts, so a lost connection only costs the part being sent."""
    path = Path(path)
    remote = remote_name or path.name
    size = path.stat().st_size
    wait_for_queue(keys, identifier, cancel, on_status)
    if size > IA_MULTIPART_OVER:
        try:
            return ia_upload_parts(keys, identifier, path, remote, ia_meta_headers(meta, mediatype, topics),
                                   on_progress, cancel, on_status)
        except _NoMultipart:
            if on_status:
                on_status("archive.org didn't take the upload in parts; sending it in one go...")
    if on_status:
        on_status("Checking the file before upload...")
    md5 = hashlib.md5()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            if cancel.is_set():
                raise Cancelled()
            md5.update(chunk)
    headers = {"Authorization": _ia_auth(keys), "User-Agent": IA_UA,
               "Content-Length": str(size), "Content-MD5": base64.b64encode(md5.digest()).decode(),
               "Content-Type": "application/octet-stream",
               "x-amz-auto-make-bucket": "1", "x-archive-size-hint": str(size),
               **ia_meta_headers(meta, mediatype, topics)}
    target = "/" + urllib.parse.quote(identifier) + "/" + urllib.parse.quote(remote)
    problem = "unknown problem"
    for attempt in range(IA_RETRIES):
        if attempt:
            wait = 30 * attempt
            if on_status:
                on_status(f"archive.org is busy ({problem}). Trying again in {wait} s...")
            if cancel.wait(wait):
                raise Cancelled()
            if _arrived(identifier, remote, md5.hexdigest()):
                return  # the earlier try got through; only the answer was lost
        conn = _ia_connection(IA_S3_HOST)
        try:
            with open(path, "rb") as f:
                if on_status:
                    on_status("Uploading to archive.org...")
                conn.request("PUT", target, body=_ProgressReader(f, size, on_progress, cancel),
                             headers=headers)
                if on_status:
                    on_status("Waiting for archive.org to confirm...")
                resp = conn.getresponse()
                body = resp.read(65536).decode("utf-8", "replace")
        except Cancelled:
            raise
        except (OSError, http.client.HTTPException) as exc:
            problem = f"connection: {getattr(exc, 'strerror', None) or exc or type(exc).__name__}"
            continue
        finally:
            conn.close()
        if resp.status == 200:
            return
        if resp.status in (429, 500, 502, 503, 504):
            problem = f"error {resp.status}"
            continue
        raise _refused(resp.status, body, keys, identifier, remote)
    raise IAError(f"Upload didn't go through after {IA_RETRIES} tries ({problem}).")


# --------------------------------------------------------------------------
# archive.org extras: trial mode, licenses, item names, queue check,
# resumable uploads in parts
# --------------------------------------------------------------------------

TRIAL_COLLECTION = "test_collection"      # archive.org removes these items after ~30 days
LICENSES = {
    "No license (all rights reserved)": "",
    "CC0 (no rights reserved)": "http://creativecommons.org/publicdomain/zero/1.0/",
    "Public Domain Mark": "http://creativecommons.org/publicdomain/mark/1.0/",
    "CC BY 4.0": "http://creativecommons.org/licenses/by/4.0/",
    "CC BY-SA 4.0": "http://creativecommons.org/licenses/by-sa/4.0/",
    "CC BY-NC 4.0": "http://creativecommons.org/licenses/by-nc/4.0/",
    "CC BY-NC-SA 4.0": "http://creativecommons.org/licenses/by-nc-sa/4.0/",
    "CC BY-ND 4.0": "http://creativecommons.org/licenses/by-nd/4.0/",
    "CC BY-NC-ND 4.0": "http://creativecommons.org/licenses/by-nc-nd/4.0/",
}
IA_PART_SIZE = 64 << 20                   # resumable uploads go in 64 MB parts
IA_MULTIPART_OVER = 200 << 20             # files bigger than this use parts
IA_PART_WAITS = (15, 30, 60, 120, 240, 300, 300)   # long enough for Wi-Fi to come back
UPLOADS_DIR = DATA_DIR / "uploads"        # progress of unfinished uploads


def site_code(site):
    """A short code instead of the site's name, for item names that shouldn't show it."""
    return "s" + hashlib.md5(str(site).encode("utf-8")).hexdigest()[:5]


def item_names(prefix, vid, site, hide_site=False, trial=False):
    """Possible archive.org item names for a video, the preferred one first. With the
    site left out, the old name (with the site in it) is still checked, so videos
    uploaded before are found again instead of being uploaded twice."""
    prefix = (prefix or "yt") + ("-test" if trial else "")
    named = ia_identifier(prefix, vid, site)
    if hide_site and site and site != "youtube":
        coded = ia_identifier(prefix, vid, site_code(site))
        return [coded] if trial else [coded, named]
    return [named]


def item_is_blocked(item):
    """The item exists but archive.org hides it (darked, removed): nothing can be added."""
    if not item:
        return False
    return bool(item.get("is_dark")) or (not item.get("metadata") and not item.get("files"))


def renamed_items():
    data = load_settings().get("ia_renamed")
    return data if isinstance(data, dict) else {}


def remember_rename(old, new):
    data = renamed_items()
    data[old] = new
    save_settings(ia_renamed=data)


def blocked_skips():
    data = load_settings().get("ia_blocked_skip")
    return set(data) if isinstance(data, list) else set()


def add_blocked_skips(idents):
    save_settings(ia_blocked_skip=sorted(blocked_skips() | set(idents)))


def free_item_name(identifier, get_item=None):
    """The first free name of identifier-2 ... identifier-9 (None if all are taken)."""
    get_item = get_item or ia_item
    for n in range(2, 10):
        name = f"{identifier[:100 - len(str(n)) - 1].rstrip('._-')}-{n}"
        if not get_item(name):
            return name
    return None


def pick_item(names, username=""):
    """(name, item, note): the first name that already has an item of yours, else the
    preferred name. item is {} if it doesn't exist yet. Names the user moved to a new
    item (because the old one was blocked) are replaced by the new name."""
    moved = renamed_items()
    names = [moved.get(n, n) for n in names]
    first, note = {}, ""
    for i, name in enumerate(names):
        try:
            found = ia_item(name)
        except IAError as exc:
            if i == 0:
                note = str(exc)
            continue
        if i == 0:
            first = found
            if found:
                return name, found, ""
        elif found:
            owner = str((found.get("metadata") or {}).get("uploader") or "").lower()
            if not owner or not username or owner == str(username).lower():
                return name, found, ""
    return names[0], first, note


def ia_queue_busy(keys, identifier):
    """True if archive.org says uploads to this item would be slowed down right now,
    False if not, None if it couldn't be asked."""
    url = (f"https://{IA_S3_HOST}/?check_limit=1&accesskey={urllib.parse.quote(keys[0])}"
           f"&bucket={urllib.parse.quote(identifier)}")
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": IA_UA}),
                                    timeout=30) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        return bool(int(data.get("over_limit") or 0))
    except Exception:
        return None


def wait_for_queue(keys, identifier, cancel, on_status=None, max_wait=1800):
    """Wait while archive.org's upload queue is over its limit (max 30 minutes)."""
    waited, step = 0, 30
    while ia_queue_busy(keys, identifier) and waited < max_wait:
        if on_status:
            on_status(f"archive.org's upload queue is full. Waiting {step} s before uploading "
                      f"({waited // 60} min so far)...")
        if cancel.wait(step):
            raise Cancelled()
        waited += step
        step = min(step * 2, 300)


class _NoMultipart(Exception):
    """archive.org didn't accept an upload in parts: send the file in one go."""


def _journal_path(identifier, name):
    key = hashlib.md5(f"{identifier}/{name}".encode("utf-8")).hexdigest()[:16]
    return UPLOADS_DIR / f"{key}.json"


def _load_journal(jp):
    try:
        return json.loads(jp.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _save_journal(jp, data):
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = jp.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, jp)


def _forget_journal(jp):
    try:
        jp.unlink()
    except OSError:
        pass


def _s3(method, target, headers, body=b""):
    """One request to archive.org's S3 API: (status, text). Connection problems raise."""
    conn = _ia_connection(IA_S3_HOST)
    try:
        conn.request(method, target, body=body, headers=headers)
        resp = conn.getresponse()
        return resp.status, resp.read(65536).decode("utf-8", "replace")
    finally:
        conn.close()


def _retrying(do, what, cancel, on_status, waits=None):
    """Run do() -> (status, text) until it gets an answer that isn't a hiccup."""
    waits = IA_PART_WAITS if waits is None else waits
    problem = ""
    for attempt in range(len(waits) + 1):
        if attempt:
            wait = waits[attempt - 1]
            if on_status:
                on_status(f"{what}: {problem}. Trying again in {wait} s...")
            if cancel.wait(wait):
                raise Cancelled()
        try:
            status, text = do()
        except Cancelled:
            raise
        except (OSError, http.client.HTTPException) as exc:
            problem = f"connection lost ({getattr(exc, 'strerror', None) or exc or type(exc).__name__})"
            continue
        if status in (429, 500, 502, 503, 504):
            problem = f"archive.org is busy (error {status})"
            continue
        return status, text
    raise IAError(f"{what} didn't go through after {len(waits) + 1} tries ({problem}). Your progress is "
                  "saved: start the upload again (e.g. with Upload files...) to continue where it stopped.")


def ia_upload_parts(keys, identifier, path, remote, meta_headers, on_progress, cancel, on_status=None):
    """Upload a big file in parts. Finished parts are noted on disk, so after a lost
    connection, Stop or a closed app, only the missing parts are sent again."""
    st = path.stat()
    size, mtime = st.st_size, int(st.st_mtime)
    n_parts = max(1, -(-size // IA_PART_SIZE))
    target = "/" + urllib.parse.quote(identifier) + "/" + urllib.parse.quote(remote)
    auth = {"Authorization": _ia_auth(keys), "User-Agent": IA_UA}
    jp = _journal_path(identifier, remote)
    j = _load_journal(jp)
    if not (j and j.get("size") == size and j.get("mtime") == mtime and j.get("part") == IA_PART_SIZE
            and j.get("upload_id")):
        status, text = _retrying(lambda: _s3("POST", target + "?uploads", {
            **auth, "Content-Length": "0", "x-archive-auto-make-bucket": "1",
            "x-archive-size-hint": str(size), **meta_headers}), "Starting the upload", cancel, on_status)
        m = re.search(r"<UploadId>([^<]+)</UploadId>", text)
        if status in (400, 405, 501) or (status == 200 and not m):
            raise _NoMultipart()
        if status != 200:
            raise _refused(status, text, keys, identifier, remote)
        j = {"size": size, "mtime": mtime, "part": IA_PART_SIZE, "upload_id": m.group(1), "parts": {}}
        _save_journal(jp, j)
    elif on_status:
        on_status(f"Continuing an unfinished upload ({len(j['parts'])} of {n_parts} parts already sent)...")

    sent_before = sum(min(IA_PART_SIZE, size - (int(n) - 1) * IA_PART_SIZE) for n in j["parts"])
    t0, base = time.monotonic(), [sent_before]
    uid = urllib.parse.quote(j["upload_id"])
    with open(path, "rb") as f:
        for n in range(1, n_parts + 1):
            if cancel.is_set():
                raise Cancelled()
            if str(n) in j["parts"]:
                continue
            f.seek((n - 1) * IA_PART_SIZE)
            data = f.read(IA_PART_SIZE)
            md5 = hashlib.md5(data)

            def progress(done, total, speed, eta, _n=n):
                now = base[0] + done
                rate = (now - sent_before) / max(time.monotonic() - t0, 0.001)
                on_progress(now, size, rate, (size - now) / rate if rate else None)

            def put(_n=n, _data=data, _md5=md5, _progress=progress):
                return _s3("PUT", f"{target}?partNumber={_n}&uploadId={uid}", {
                    **auth, "Content-Length": str(len(_data)),
                    "Content-MD5": base64.b64encode(_md5.digest()).decode()},
                    body=_ProgressReader(io.BytesIO(_data), len(_data), _progress, cancel))
            status, text = _retrying(put, f"Part {n} of {n_parts}", cancel, on_status)
            if status == 404 or "NoSuchUpload" in text:
                _forget_journal(jp)
                raise IAError("archive.org forgot the unfinished upload (it was too old). Start the "
                              "upload again; it will begin from the start.")
            if status != 200:
                raise _refused(status, text, keys, identifier, remote)
            j["parts"][str(n)] = md5.hexdigest()
            _save_journal(jp, j)
            base[0] += len(data)

    if on_status:
        on_status("Putting the parts together on archive.org (can take a few minutes)...")
    xml = "<CompleteMultipartUpload>" + "".join(
        f'<Part><PartNumber>{n}</PartNumber><ETag>"{j["parts"][str(n)]}"</ETag></Part>'
        for n in range(1, n_parts + 1)) + "</CompleteMultipartUpload>"
    body = xml.encode("utf-8")
    status, text = _retrying(lambda: _s3("POST", f"{target}?uploadId={uid}", {
        **auth, "Content-Length": str(len(body)), "Content-Type": "application/xml"}, body),
        "Finishing the upload", cancel, on_status)
    if status == 200 and "<Error>" not in text:
        _forget_journal(jp)
        return
    if "InvalidPart" in text or "NoSuchUpload" in text:
        _forget_journal(jp)
        raise IAError("archive.org couldn't put the parts together. Start the upload again; it will "
                      "begin from the start.")
    raise _refused(status, text, keys, identifier, remote)


def restore_bundled_ytdlp():
    """Put back only the app's own copy of yt-dlp (other tools, such as an updated
    Deno, stay as they are). Returns None or an error message."""
    if not (BUNDLE / "tools.tar.xz").exists():
        return "The app's own copy of yt-dlp isn't available. Use Tools > Repair built-in tools."
    tmp = YTDLP.with_name(YTDLP.name + ".restore")
    try:
        with tarfile.open(BUNDLE / "tools.tar.xz", "r:xz") as tar:
            member = tar.getmember(YTDLP.name)
            with tar.extractfile(member) as src, open(tmp, "wb") as out:
                shutil.copyfileobj(src, out)
        os.replace(tmp, YTDLP)
    except (OSError, KeyError, tarfile.TarError) as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        return f"Couldn't put back the app's own yt-dlp ({exc}). Use Tools > Repair built-in tools."
    return None


# --------------------------------------------------------------------------
# Cookies: none, from Firefox, or from a cookies.txt file (any browser)
# --------------------------------------------------------------------------

COOKIES_COPY = DATA_DIR / "cookies.txt"


def check_cookies_file(path):
    """Number of cookies in a Netscape-format cookies.txt, or raise ValueError."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ValueError(f"Couldn't read the file ({exc.strerror or exc}).")
    count = 0
    for line in text.splitlines():
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        elif not line.strip() or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) == 7 and fields[4].strip().lstrip("-").isdigit():
            count += 1
    if not count:
        raise ValueError("This isn't a cookies.txt file in the usual (Netscape) format. Export it again "
                         "and choose \"Netscape\" or \"cookies.txt\" as the format.")
    return count


def use_cookies_file(path):
    """Copy a cookies.txt into the app's folder (yt-dlp also writes to it). Returns the count."""
    count = check_cookies_file(path)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(path, COOKIES_COPY)
    return count


def cookie_mode():
    st = load_settings()
    mode = st.get("cookies_mode") or ("firefox" if st.get("firefox_cookies") else "off")
    if mode == "file" and not COOKIES_COPY.is_file():
        return "off"
    return mode if mode in ("off", "firefox", "file") else "off"


def cookie_args(mode=None):
    mode = mode or cookie_mode()
    if mode == "firefox":
        return ["--cookies-from-browser", "firefox"]
    if mode == "file" and COOKIES_COPY.is_file():
        return ["--cookies", str(COOKIES_COPY)]
    return []


# --------------------------------------------------------------------------
# YouTube "403 Forbidden" fix: the bgutil PO-token provider, run with our Deno
# --------------------------------------------------------------------------

POT_VERSION = "2.0.2"
POT_REPO = "https://github.com/Brainicism/bgutil-ytdlp-pot-provider"
POT_PLUGIN_URL = f"{POT_REPO}/releases/download/{POT_VERSION}/bgutil-ytdlp-pot-provider.zip"
POT_PLUGIN_SHA256 = "a26b980bf6d8e8400d8f21445daa4d8381bf6183d20f0be309391f86b31e27ef"
POT_SOURCE_URL = f"{POT_REPO}/archive/refs/tags/{POT_VERSION}.zip"
# Files of the provider's server folder, checked one by one (GitHub may repack its
# source zips, but the files inside never change for a release).
POT_FILES = {
    "deno.json": "02eb902dee78d03e82ff838bd39115b4caef4e6bd82b31615f539910436fc6cb",
    "deno.lock": "5b251ee1b127499e72b8faf9f6a9cee36b6d8dc3970bc8249e58f6f840550c59",
    "package.json": "325cf8e4c7bce37870ecb3275df3c67f9d2058ecb0d7e728c99278207873c169",
    "tsconfig.json": "3709d6cc9b127b674b0ae4997c144e55812ec976ccf63a7e482581473dbaf869",
    "src/generate_once.ts": "c8a2a22a07d9576c73377ce79edced330fce69c86d3bb901bd81032fa43ab2cd",
    "src/main.ts": "238a1ba5fc2b4591b5207bfcbf95e8a84b9653359fe35781f6a45460423924f7",
    "src/session_manager.ts": "39a8789229da1003dde24c7e3630a429a4fd7567fd3b8f22d2a2a389b6182c6f",
    "src/utils.ts": "64f049023d482dcc8a93ff34c95de8c132d244694fa3d8f2a11eecd2870c696f",
    "types/commander.d.ts": "290e470503eaabe716fa373995e076bb2465411eb92c18de62c65a7ed98b9d8e",
}
POT_DIR = DATA_DIR / "pot"
POT_SERVER = POT_DIR / "server"
POT_MARKER = POT_DIR / "installed.json"
POT_PLUGIN_NAME = "bgutil-ytdlp-pot-provider.zip"


def pot_installed():
    try:
        return (json.loads(POT_MARKER.read_text(encoding="utf-8")).get("version") == POT_VERSION
                and (plugins_dir() / POT_PLUGIN_NAME).is_file() and (POT_SERVER / "node_modules").is_dir())
    except (OSError, ValueError):
        return False


def pot_args():
    if pot_installed():
        return ["--extractor-args", f"youtubepot-bgutilscript:server_home={POT_SERVER}"]
    return []


def _download(url, limit=50 << 20):
    req = urllib.request.Request(url, headers={"User-Agent": IA_UA})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise OSError("download too large")
    return data


def install_pot(say, fetch=_download, run=None):
    """Download, check and set up the provider. say(text) reports progress.
    Call with TOOL_LOCK held. Returns True if it worked."""
    import zipfile
    say(f"Downloading the PO-token plugin {POT_VERSION}...")
    try:
        plugin = fetch(POT_PLUGIN_URL)
        source = fetch(POT_SOURCE_URL)
    except Exception as exc:
        say(f"Couldn't download it ({getattr(exc, 'reason', exc)}). Check your internet connection.")
        return False
    if hashlib.sha256(plugin).hexdigest() != POT_PLUGIN_SHA256:
        say("The plugin download doesn't match its known checksum. Stopping to be safe.")
        return False
    new = POT_DIR / "server.new"
    try:
        shutil.rmtree(new, ignore_errors=True)
        with zipfile.ZipFile(io.BytesIO(source)) as z:
            names = {"/".join(n.split("/")[1:]): n for n in z.namelist()}   # drop "<repo>-<tag>/"
            for rel, want in POT_FILES.items():
                full = names.get("server/" + rel)
                data = z.read(full) if full else None
                if data is None or hashlib.sha256(data).hexdigest() != want:
                    say(f"The provider's file {rel} doesn't match its known checksum. Stopping to be safe.")
                    shutil.rmtree(new, ignore_errors=True)
                    return False
                (new / rel).parent.mkdir(parents=True, exist_ok=True)
                (new / rel).write_bytes(data)
    except (OSError, zipfile.BadZipFile) as exc:
        say(f"Couldn't unpack the provider ({exc}).")
        shutil.rmtree(new, ignore_errors=True)
        return False
    say("Installing the provider's parts with Deno (a few minutes, downloads about 100 MB)...")
    if run is None:
        def run(args, cwd):
            r = subprocess.run([str(DENO), *args], cwd=str(cwd), capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=900, stdin=subprocess.DEVNULL,
                               **proc_kwargs())
            return r.returncode, (r.stdout or "") + (r.stderr or "")
    try:
        code, out = run(["install", "--allow-scripts=npm:canvas", "--frozen"], new)
    except Exception as exc:
        code, out = 1, str(exc)
    if code != 0 or not (new / "node_modules").is_dir():
        last = [l for l in out.strip().splitlines() if l.strip()][-3:]
        say("Deno couldn't install the provider's parts:\n  " + "\n  ".join(last or ["no details"]))
        shutil.rmtree(new, ignore_errors=True)
        return False
    try:
        shutil.rmtree(POT_SERVER, ignore_errors=True)
        new.rename(POT_SERVER)
        d = plugins_dir()
        d.mkdir(parents=True, exist_ok=True)
        (d / POT_PLUGIN_NAME).write_bytes(plugin)
        POT_MARKER.write_text(json.dumps({"version": POT_VERSION}), encoding="utf-8")
    except OSError as exc:
        say(f"Couldn't save the provider ({exc.strerror or exc}).")
        return False
    return True


def pot_status_line(verbose_output):
    """The provider line from 'yt-dlp -v' output, e.g. 'bgutil:script-deno-2.0.2'."""
    for line in verbose_output.splitlines():
        if "PO Token Providers" in line:
            return line.split("PO Token Providers:", 1)[-1].strip()
    return ""


def remove_pot():
    problems = []
    for p in (plugins_dir() / POT_PLUGIN_NAME, POT_MARKER):
        try:
            p.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            problems.append(str(p))
    shutil.rmtree(POT_SERVER, ignore_errors=True)
    return problems


# --------------------------------------------------------------------------
# Remembering what each downloaded file is, so it can be uploaded later
# ("Upload downloaded files": after a failed upload, Wi-Fi drop, etc.)
# --------------------------------------------------------------------------

LIBRARY_FILE = DATA_DIR / "library.json"
_LIBRARY_LOCK = threading.Lock()
MEDIA_EXTS = ("mp4", "mkv", "webm", "mov", "mp3", "m4a", "opus", "ogg", "oga", "flac", "wav", "aac")
YOUTUBE_ID_RE = re.compile(r"[A-Za-z0-9_-]{11}")
_ENTRY_KEYS = ("webpage_url", "url", "extractor_key", "ie_key", "webpage_url_domain",
               "_yta_id", "_yta_title", "_yta_page", "_yta", "_yta_generic")


def _load_library():
    try:
        data = json.loads(LIBRARY_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def remember_file(path, meta, entry):
    """Note the details of a saved file. Never stops a download if it fails."""
    if _settings_frozen:
        return
    keep = {k: v for k, v in (meta or {}).items()
            if (k in META_FIELDS or k == "_yta_title") and v not in (None, "", [])}
    if isinstance(keep.get("description"), str):
        keep["description"] = keep["description"][:5000]
    rec = {"meta": keep, "entry": {k: entry[k] for k in _ENTRY_KEYS if entry.get(k) not in (None, "")},
           "saved": time.strftime("%Y-%m-%d %H:%M")}
    with _LIBRARY_LOCK:
        try:
            lib = _load_library()
            lib[Path(path).name] = rec
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            tmp = LIBRARY_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(lib, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, LIBRARY_FILE)
        except OSError:
            pass


def file_video_id(path):
    m = re.search(r"\[([^\[\]]+)\]$", Path(path).stem)
    return m.group(1) if m else None


def scan_media(folder, subfolders=True):
    """Downloaded files in a folder: media files whose name ends in [video id]."""
    folder = Path(folder)
    found = []
    for p in (folder.rglob("*") if subfolders else folder.glob("*")):
        try:
            rel = p.relative_to(folder).parts
        except ValueError:
            continue
        if any(part.startswith(".") for part in rel[:-1]) or STAGING_NAME in rel:
            continue        # hidden folders, and downloads still in progress
        if (p.suffix.lower().lstrip(".") in MEDIA_EXTS and file_video_id(p)
                and not p.name.startswith(".") and p.is_file()):
            found.append(p)
    return sorted(found, key=lambda p: str(p).lower())


def read_file_tags(path):
    """The tags inside a media file (yt-dlp writes title, link, description there)."""
    try:
        r = run_tool(FFMPEG_DIR / ("ffprobe" + EXE),
                     ["-v", "error", "-show_entries", "format_tags", "-of", "json", str(path)], 60)
        tags = (json.loads(r.stdout or "{}").get("format") or {}).get("tags") or {}
    except Exception:
        return {}
    return {str(k).lower(): str(v) for k, v in tags.items()}


def meta_from_tags(tags, vid, path):
    stem = re.sub(r"\s*\[[^\[\]]+\]$", "", Path(path).stem)
    meta = {"id": vid, "title": (tags.get("title") or "").strip() or stem}
    for key in ("purl", "comment"):
        value = (tags.get(key) or "").strip()
        if re.fullmatch(r"https?://\S+", value):
            meta["webpage_url"] = value
            break
    if tags.get("artist"):
        meta["uploader"] = tags["artist"]
    desc = tags.get("description") or tags.get("synopsis")
    if desc:
        meta["description"] = desc
    date = re.sub(r"\D", "", tags.get("date") or "")
    if len(date) == 8:
        meta["upload_date"] = date
    return meta


def describe_file(path, library, read_tags=read_file_tags):
    """What's known about a downloaded file, for uploading it like a normal download.
    Returns a dict with path, vid, meta, entry, lookup (ask yt-dlp for more details),
    how (where the details come from) and site; how is None if it can't be uploaded."""
    path = Path(path)
    vid = file_video_id(path)
    item = {"path": str(path), "vid": vid, "meta": {}, "entry": {}, "lookup": False, "how": None,
            "site": "", "reason": ""}
    rec = library.get(path.name)
    if isinstance(rec, dict) and rec.get("meta"):
        item["meta"], item["entry"] = dict(rec["meta"]), dict(rec.get("entry") or {})
        item["vid"] = item["entry"].get("_yta_id") or item["meta"].get("id") or vid
        item["how"] = "saved at download"
    else:
        meta = meta_from_tags(read_tags(path), vid, path)
        item["meta"] = meta
        if meta.get("webpage_url"):
            item["entry"] = {"webpage_url": meta["webpage_url"]}
            item.update(lookup=True, how="link inside the file")
        elif vid and YOUTUBE_ID_RE.fullmatch(vid) and not vid.startswith("s-"):
            item["entry"] = {"ie_key": "Youtube", "webpage_url": f"https://www.youtube.com/watch?v={vid}"}
            item.update(lookup=True, how="YouTube id")
        else:
            item["reason"] = ("Downloaded before this version, and the file doesn't say which site it "
                              "came from. Paste its link again with uploading on; it won't be downloaded twice.")
            return item
    item["site"] = site_key(item["meta"]) or site_key(item["entry"]) or "youtube"
    return item


def check_on_archive(item, prefix, username, hide_site=False, trial=False):
    """Add the archive.org identifier and status ('upload', 'there', 'theirs') to an item."""
    names = item_names(prefix, item["vid"], item["site"], hide_site, trial)
    item["ident"], found, note = pick_item(names, username)
    item["status"], item["note"] = "upload", ""
    if note:
        item["note"] = f"couldn't check archive.org ({note})"
        return item
    if item_is_blocked(found):
        item["note"] = "its item is blocked on archive.org; you'll be asked at the end of the run"
        return item
    if found:
        owner = str((found.get("metadata") or {}).get("uploader") or "").lower()
        if owner and username and owner != str(username).lower():
            item["status"] = "theirs"
        elif Path(item["path"]).name in {f.get("name") for f in found.get("files") or []}:
            item["status"] = "there"
    return item


def scan_for_upload(folder, subfolders, prefix, username, progress=lambda text: None,
                    read_tags=read_file_tags, cancel=None, hide_site=False, trial=False):
    """Everything the 'Upload downloaded files' window shows. Call with TOOL_LOCK held."""
    import concurrent.futures
    files = scan_media(folder, subfolders)
    progress(f"Found {len(files)} downloaded file(s). Reading their details...")
    library = _load_library()
    items = []
    for n, p in enumerate(files, 1):
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        items.append(describe_file(p, library, read_tags))
        if n % 10 == 0:
            progress(f"Reading details: {n} of {len(files)}...")
    known = [i for i in items if i["how"]]
    done = [0]
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(check_on_archive, i, prefix, username, hide_site, trial) for i in known]
        for _ in concurrent.futures.as_completed(futures):
            done[0] += 1
            progress(f"Checking archive.org: {done[0]} of {len(known)}...")
            if cancel is not None and cancel.is_set():
                for f in futures:
                    f.cancel()
                raise Cancelled()
    return items


def scan_report(items, folder, refresh):
    folder = Path(folder)
    up = [i for i in items if i.get("status") == "upload"]
    there = [i for i in items if i.get("status") == "there"]
    theirs = [i for i in items if i.get("status") == "theirs"]
    unknown = [i for i in items if not i["how"]]

    def name(i):
        try:
            return str(Path(i["path"]).relative_to(folder))
        except ValueError:
            return Path(i["path"]).name
    lines = [f"{len(items)} downloaded file(s) in {folder}", ""]
    lines.append(f"  {len(up):4}  not on archive.org yet: will be uploaded")
    lines.append(f"  {len(there):4}  already on archive.org" + (": will be updated (option in Step 4 is on)"
                                                               if refresh and there else ""))
    if theirs:
        lines.append(f"  {len(theirs):4}  belong to an item of someone else: skipped")
    if unknown:
        lines.append(f"  {len(unknown):4}  can't tell which site they're from: skipped")
    for title, group, extra in (("WILL BE UPLOADED", up, lambda i: f"   ({i['how']}"
                                                         + (f"; {i['note']}" if i["note"] else "") + ")"),
                                ("SKIPPED: SOMEONE ELSE'S ITEM", theirs,
                                 lambda i: f"   ({i['ident']}: change your identifier prefix)"),
                                ("SKIPPED: UNKNOWN SITE", unknown, lambda i: ""),
                                ("ALREADY ON ARCHIVE.ORG", there, lambda i: "")):
        if group:
            lines += ["", title]
            lines += ["  " + name(i) + extra(i) for i in group[:200]]
            if len(group) > 200:
                lines.append(f"  ... and {len(group) - 200} more")
            if group is unknown:
                lines.append("  " + unknown[0]["reason"])
    return "\n".join(lines), up + (there if refresh else [])


# --------------------------------------------------------------------------
# A download job: resolves links, then downloads video by video
# --------------------------------------------------------------------------


class Job:
    """ui needs: log(text, tag), now(text), progress(pct, detail), confirm(question)."""

    def __init__(self, urls, mode, quality, out_dir, playlist, ui, cookies=False, archive=None,
                 author="", hide_source=False, files=None):
        self.urls, self.mode, self.quality = urls, mode, quality
        # files: items from scan_for_upload() -> upload these instead of downloading
        self.files = files
        self.out_dir, self.playlist, self.ui = Path(out_dir), playlist, ui
        self.cookies = cookies
        # archive: None, or {"keys": (access, secret), "prefix": ..., "username": ...}
        self.archive = archive
        self.author = (author or "").strip()
        self.system_certs = False
        self._generic = False
        self.hide_source = hide_source
        self.uploaded = self.on_ia = self.retopiced = 0
        self.upload_blocked = None   # set when archive.org refuses the whole account
        self.blocked = []            # videos whose item is blocked: asked about at the end
        self._meta = {}
        self.ext = "mp3" if mode == "mp3" else "mp4"
        # yt-dlp works in here and only moves the finished file into your folder,
        # so Stop can simply empty this folder without touching anything of yours.
        self.staging = self.out_dir / STAGING_NAME / uuid.uuid4().hex[:8]
        self.cancel = threading.Event()
        self.cancelled = False
        self.saved = self.existing = self.skipped = 0
        self.failed = []
        self._seen_warnings = set()
        self._final = None

    # --- running yt-dlp ---
    def _run(self, args, on_line):
        code, errors = self._run_once(args, on_line)
        if code != 0 and not self.system_certs and any(is_cert_error(e) for e in errors):
            # yt-dlp checks certificates against its own built-in list. Antivirus "HTTPS
            # scanning", network filters and servers with an incomplete chain fail that,
            # but usually pass with Windows' own list, which is still a real check.
            self.system_certs = True
            self.ui.log("Note: a secure-connection check failed. Trying again with Windows' "
                        "own certificate list (used for the rest of this run).", "warn")
            code, errors = self._run_once(args, on_line)
        return code, errors

    def _run_once(self, args, on_line):
        if self.cancel.is_set():
            raise Cancelled()
        if isinstance(self.cookies, (list, tuple)):
            cookie_list = list(self.cookies)
        else:   # True/False from older callers
            cookie_list = ["--cookies-from-browser", "firefox"] if self.cookies else []
        cert_args = ["--compat-options", "no-certifi"] if self.system_certs else []
        proc = subprocess.Popen([str(YTDLP), *COMMON_ARGS, *cookie_list, *cert_args, *pot_args(), *args],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
                                errors="replace", bufsize=1, **proc_kwargs())

        def watch():
            while proc.poll() is None:
                if self.cancel.is_set():
                    kill_tree(proc)
                    return
                time.sleep(0.2)
        threading.Thread(target=watch, daemon=True).start()

        errors = []
        for raw in proc.stdout:
            line = raw.rstrip("\r\n")
            if line.startswith("ERROR:"):
                errors.append(line)
            elif line.startswith("WARNING:"):
                self.warning(line)
            elif line:
                on_line(line)
        proc.wait()
        if self.cancel.is_set():
            raise Cancelled()
        return proc.returncode, errors

    @staticmethod
    def _failure(code, errors):
        return RuntimeError(errors[-1] if errors else f"yt-dlp stopped unexpectedly (code {code}).")

    def warning(self, msg):
        text = re.sub(r"^\[[^\]]+\]\s*([\w-]{11}:\s*)?", "", msg.replace("WARNING: ", "")).strip()
        if "javascript runtime" in text.lower():
            text = "yt-dlp can't use Deno. Some formats may be missing. Use Tools > Repair built-in tools."
        key = text[:90]
        if key not in self._seen_warnings:
            self._seen_warnings.add(key)
            self.ui.log("Note: " + text.splitlines()[0][:300], "warn")

    def _on_download_line(self, line):
        if line.startswith("@@P "):
            f = line[4:].split("|")
            if len(f) < 7:
                return
            pct, text = describe_progress({"downloaded_bytes": _num(f[0]), "total_bytes": _num(f[1]),
                                           "total_bytes_estimate": _num(f[2]),
                                           "speed": _num(f[3]), "eta": _num(f[4])})
            vcodec, acodec = f[5], f[6]
            if self.mode == "mp4" and vcodec == "none":
                label, overall = "Audio stream", None if pct is None else 50 + pct / 2
            elif self.mode == "mp4" and acodec == "none":
                label, overall = "Video stream", None if pct is None else pct / 2
            else:
                label, overall = "Downloading", pct
            shown = f"{pct:.0f}%, " if pct is not None else ""
            self.ui.progress(overall, f"{label}: {shown}{text}")
        elif line.startswith("@@PP "):
            name, _, status = line[5:].partition("|")
            text = {"ExtractAudio": "Converting to MP3...", "Merger": "Merging video and audio...",
                    "EmbedThumbnail": "Adding cover art..."}.get(name)
            if text and status == "started":
                self.ui.progress(100, text)
        elif line.startswith("@@F "):
            self._final = line[4:].strip()
        elif line.startswith("@@M "):
            parse_meta_line(line, self._meta)

    def _download_args(self, folder, in_playlist, entry=None):
        args = ["--newline", "--progress", "--no-simulate", "--no-playlist", "--no-overwrites",
                "--windows-filenames", "--retries", "5", "--fragment-retries", "5",
                "--ffmpeg-location", str(FFMPEG_DIR),
                # Folders go in -P, so only the title is ever shortened and the
                # [video id] always survives in the file name.
                "-P", f"home:{folder}", "-P", f"temp:{self.staging}",
                "-o", self._out_template(entry),
                "--match-filters", LIVE_FILTER,
                "--progress-template", PROGRESS_TPL,
                "--progress-template", POSTPROC_TPL,
                "--print", "after_move:@@F %(filepath)s"]
        if self.archive or self.author or self.hide_source:
            args += meta_print_args("after_move:")
        if entry:
            args += self._link_args(entry)
        if self.archive and self.archive.get("extras"):
            # kept in the work folder (not next to your files) for archive.org
            args += ["--write-thumbnail", "--convert-thumbnails", "jpg",
                     "-P", f"thumbnail:{self.staging / 'extra'}"]
        if in_playlist:
            args += PLAYLIST_SLEEP
        if self.mode == "mp3":
            args += ["-f", "bestaudio/best", "-x", "--audio-format", "mp3",
                     "--audio-quality", str(self.quality), "--embed-metadata",
                     "--embed-thumbnail", "--convert-thumbnails", "jpg"]
        else:
            # Resolution first, then codec: at the same resolution H.264 beats a
            # higher-fps VP9/AV1 stream, so "plays everywhere" holds.
            args += ["-f", "bv*+ba/b", "-S", f"res:{self.quality},vcodec:h264,fps,acodec:aac",
                     "--merge-output-format", "mp4", "--embed-metadata"]
        return args

    @staticmethod
    def _out_template(entry):
        if entry and entry.get("_yta_id"):
            name = safe_title(entry.get("_yta_title") or "stream").replace("%", "%%")
            return f"{name} [{entry['_yta_id']}].%(ext)s"
        return "%(title).120B [%(id)s].%(ext)s"

    def _overrides(self, meta, entry):
        """A stream link's own title and page replace what yt-dlp guessed ('master')."""
        meta = dict(meta or {})
        if entry.get("_yta_title"):
            meta["title"] = entry["_yta_title"]
            meta["_yta_title"] = entry["_yta_title"]
        if entry.get("_yta_id"):
            meta["id"] = entry["_yta_id"]
        if entry.get("_yta_page"):
            m = re.match(r"https?://(?:www\.)?([^/?#:]+)", entry["_yta_page"])
            meta.update(webpage_url=entry["_yta_page"], extractor_key="Generic",
                        webpage_url_domain=m.group(1) if m else "")
        return meta

    @staticmethod
    def _is_container(entry):
        """A channel/profile inside a playlist. The URL patterns only apply to YouTube:
        on TikTok, Mastodon, PeerTube... normal video links contain /@ too."""
        if entry.get("_type") == "playlist" or entry.get("ie_key") == "YoutubeTab":
            return True
        url = entry.get("url") or ""
        return (re.match(r"https?://([\w-]+\.)?(youtube\.com|youtu\.be)/", url) is not None
                and any(s in url for s in ("/playlist?", "/@", "/channel/", "/c/", "/user/")))

    @staticmethod
    def _link_args(entry_or_opts):
        opts = entry_or_opts.get("_yta", entry_or_opts) if isinstance(entry_or_opts, dict) else {}
        args = ["--referer", opts["referer"]] if opts.get("referer") else []
        if isinstance(entry_or_opts, dict) and entry_or_opts.get("_yta_generic"):
            args.append("--force-generic-extractor")
        return args

    def _resolve(self, url, opts=None):
        """Return (playlist_title or None, [entries]) for one link."""
        opts = opts or {}
        title, items = self._resolve_raw(url, opts)
        for e in items:
            if opts:
                e["_yta"] = opts
            if self._generic:
                e["_yta_generic"] = True
        if is_stream_url(url) or opts.get("title"):
            # A stream address (.m3u8/.mpd) says nothing about the video: give it a
            # title and an id of its own, and remember the page it came from.
            for e in items:
                e["_yta_id"] = stream_id(url) if len(items) == 1 else f"{stream_id(url)}-{e.get('id')}"
                e["_yta_title"] = opts.get("title") or stream_title(url, opts.get("referer"))
                if opts.get("referer"):
                    e["_yta_page"] = opts["referer"]
        return title, items

    def _resolve_raw(self, url, opts):
        self.ui.now("Looking up " + url)
        self.ui.progress(None, "Checking the link...")
        self._generic = False
        base = ["-J", "--flat-playlist", "--yes-playlist" if self.playlist else "--no-playlist",
                "-I", f"1:{PLAYLIST_MAX + 1}", *self._link_args(opts)]
        out = []
        code, errors = self._run([*base, "--", url],
                                 lambda line: out.append(line) if line.startswith("{") else None)
        if not out and needs_generic(errors):
            # The site's own support in yt-dlp broke (sites change all the time):
            # the generic scanner often still finds a <video> tag or embedded player.
            self.ui.log("Note: yt-dlp's support for this site failed; trying its generic page "
                        "scanner...", "warn")
            out2 = []
            code2, errors2 = self._run([*base, "--force-generic-extractor", "--", url],
                                       lambda line: out2.append(line) if line.startswith("{") else None)
            if out2:
                out, self._generic = out2, True
                self.ui.log("   The generic scanner found it.", "ok")
        if not out:
            raise self._failure(code, errors)
        info = json.loads(out[-1])
        if info.get("_type") not in ("playlist", "multi_video"):
            return None, [info]
        if info.get("_type") == "multi_video":
            # one post with several videos (Twitter, Instagram...): it's one link, get them all
            items = [e for e in (info.get("entries") or []) if e]
            for e in items:
                e.setdefault("webpage_url", info.get("webpage_url"))
                e.setdefault("extractor_key", info.get("extractor_key"))
            return None, items

        if not self.playlist:
            self.skipped += 1
            self.ui.log(f"Skipped {url}: this is a playlist or channel link. Tick "
                        "\"download all of it\" to get the videos.", "warn")
            return None, []
        items = list(info.get("entries") or [])
        if any(self._is_container(e) for e in items):
            self.skipped += 1
            self.ui.log(f"Skipped {url}: this is a whole channel. Open the channel's "
                        "Videos tab or one of its playlists and paste that link.", "warn")
            return None, []

        total = info.get("playlist_count") or len(items)
        more = len(items) > PLAYLIST_MAX or total > PLAYLIST_MAX
        items = items[:PLAYLIST_MAX]
        title = info.get("title") or info.get("id") or "Playlist"
        if not items:
            self.ui.log(f"\"{title}\" has no videos to download.", "warn")
            return None, []
        if len(items) > PLAYLIST_CONFIRM_OVER:
            q = (f"\"{title}\" has {total} videos. Download the first {len(items)}?" if more
                 else f"\"{title}\" has {len(items)} videos. Download all of them?")
            if not self.ui.confirm(q):
                self.skipped += 1
                self.ui.log(f"Skipped \"{title}\".", "warn")
                return None, []
        return title, items

    AUDIO_EXTS = ("mp3", "m4a", "opus", "ogg", "oga", "flac", "wav", "aac")

    def _existing(self, folder, vid):
        if not folder.is_dir():
            return None
        exts = [self.ext] + ([e for e in self.AUDIO_EXTS if e != self.ext] if self.mode == "mp4" else [])
        for ext in exts:   # audio-only sites give audio files even in MP4 mode
            for p in folder.glob("*" + glob.escape(f"[{vid}].{ext}")):
                return p
        # ids with characters Windows doesn't allow in file names get changed by
        # yt-dlp, so also compare ids with only their letters and digits
        want = re.sub(r"[\W_]+", "", str(vid))
        if want and want != str(vid):
            for ext in exts:
                for p in folder.glob(f"*].{ext}"):
                    m = re.search(r"\[([^\[\]]+)\]$", p.stem)
                    if m and re.sub(r"[\W_]+", "", m.group(1)) == want:
                        return p
        return None

    def _clear_staging(self):
        """Throw away everything unfinished. Only our own work folder is touched."""
        _rmtree_retry(self.staging)
        self.staging.mkdir(parents=True, exist_ok=True)

    def _do_video(self, entry, playlist_title, n, total):
        if self.cancel.is_set():
            raise Cancelled()
        folder = self.out_dir / safe_folder(playlist_title) if playlist_title else self.out_dir
        name = entry.get("_yta_title") or entry.get("title") or entry.get("id") or "video"
        vid = entry.get("_yta_id") or entry.get("id")
        self.ui.now(f"{n} of {total}: {name}" if total > 1 else name)
        self.ui.progress(0, "Starting...")

        if vid and (found := self._existing(folder, vid)):
            self.existing += 1
            self.ui.log("Already in folder: " + found.name)
            if self.archive:
                self._archive(found, None, vid, entry)
            return
        if entry.get("is_live") or entry.get("live_status") in LIVE_STATES:
            self.skipped += 1
            self.ui.log(f"Skipped \"{name}\": it's a live or upcoming stream. "
                        "Download it once the stream has ended.", "warn")
            return

        folder.mkdir(parents=True, exist_ok=True)
        args, info_file = self._download_args(folder, playlist_title is not None, entry), None
        if "formats" in entry:  # a single video that was already looked up in full
            fd, info_file = tempfile.mkstemp(suffix=".info.json", dir=self.staging)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(entry, fh)
            args += ["--load-info-json", info_file]
        else:
            url = self._entry_url(entry, vid)
            if not url:
                raise RuntimeError("This entry has no link to download from.")
            args += ["--", url]

        self._final, self._meta = None, {}
        try:
            code, errors = self._run(args, self._on_download_line)
        except Cancelled:
            self._clear_staging()
            raise
        finally:
            if info_file:
                try:
                    os.unlink(info_file)
                except OSError:
                    pass
        if self._final:
            self.saved += 1
            self.ui.log("Saved: " + Path(self._final).name, "ok")
            self._meta = self._overrides(self._meta, entry)
            remember_file(Path(self._final), {**self._site_info(entry), **self._meta}, entry)
            if self.author or self.hide_source or entry.get("_yta_title"):
                self._retag(Path(self._final), {**self._site_info(entry), **self._meta})
            if self.archive:
                self._archive(Path(self._final), self._meta, vid or self._meta.get("id"), entry)
        elif code == 0 and not errors:
            self.skipped += 1
            self.ui.log(f"Skipped \"{name}\": yt-dlp didn't save a file. This usually means "
                        "it's a live or upcoming stream.", "warn")
        else:
            self._clear_staging()
            raise self._failure(code, errors)

    # --- archive.org ---
    def _fetch_meta(self, entry, vid, path):
        """Title, description etc. for a file that was downloaded in an earlier run."""
        url = self._entry_url(entry, vid)
        if not url:
            stem = re.sub(r"\s*\[[^\[\]]+\]$", "", path.stem)
            return {"id": vid, "title": entry.get("title") or stem, **self._site_info(entry), "_fallback": True}
        self.ui.progress(None, "Getting the video's details for archive.org...")
        meta = {}
        try:
            self._run(["--skip-download", "--no-playlist", *self._link_args(entry), *meta_print_args(), "--", url],
                      lambda line: parse_meta_line(line, meta) if line.startswith("@@M ") else None)
        except Cancelled:
            raise
        except Exception:
            pass
        if meta.get("title"):
            return self._overrides(meta, entry)
        stem = re.sub(r"\s*\[[^\[\]]+\]$", "", path.stem)
        return {"id": vid, "title": entry.get("title") or stem, "webpage_url": url, **self._site_info(entry),
                "_fallback": True}

    def _run_tool(self, args):
        """Run ffmpeg (or another tool) so that Stop can end it."""
        if self.cancel.is_set():
            raise Cancelled()
        proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
                                errors="replace", **proc_kwargs())

        def watch():
            while proc.poll() is None:
                if self.cancel.is_set():
                    kill_tree(proc)
                    return
                time.sleep(0.2)
        threading.Thread(target=watch, daemon=True).start()
        _, err = proc.communicate()
        if self.cancel.is_set():
            raise Cancelled()
        return proc.returncode, err or ""

    def _retag(self, path, meta):
        """Put your author into the file and take the source link out of it. Streams are
        copied, not re-encoded, so this takes seconds and quality is untouched."""
        pm = prepare_meta(meta, self.hide_source, self.author)
        tags = []
        if self.author:
            tags += ["-metadata", f"artist={self.author}", "-metadata", f"album_artist={self.author}"]
        if self.hide_source:
            tags += ["-metadata", "comment=", "-metadata", "purl="]   # yt-dlp puts the link here
            if pm.get("title") and pm["title"] != meta.get("title"):
                tags += ["-metadata", f"title={pm['title']}"]
            if "description" in meta:
                desc = (pm.get("description") or "")[:8000]
                tags += ["-metadata", f"description={desc}", "-metadata", f"synopsis={desc}"]
        if meta.get("_yta_title") and not any(t.startswith("title=") for t in tags):
            tags += ["-metadata", f"title={meta['_yta_title']}"]
        if not tags:
            return
        tmp = self.staging / ("retag" + path.suffix)
        args = [str(FFMPEG_DIR / ("ffmpeg" + EXE)), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                "-i", str(path), "-map", "0", "-dn", "-c", "copy", *tags]
        if path.suffix.lower() == ".mp3":
            args += ["-id3v2_version", "3"]
        elif path.suffix.lower() in (".mp4", ".m4a", ".mov"):
            args += ["-movflags", "+faststart"]
        args.append(str(tmp))
        self.ui.progress(None, "Updating the file's details...")
        try:
            code, err = self._run_tool(args)
            if code != 0 or not tmp.is_file() or tmp.stat().st_size < path.stat().st_size * 0.9:
                raise OSError((err.strip().splitlines() or ["ffmpeg failed"])[-1][:200])
            st = path.stat()
            os.utime(tmp, (st.st_atime, st.st_mtime))
            os.replace(tmp, path)
        except Cancelled:
            raise
        except OSError as exc:
            self.ui.log(f"Note: couldn't update the details inside {path.name} ({exc}). "
                        "The file itself is fine.", "warn")
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass

    @staticmethod
    def _entry_url(entry, vid):
        url = entry.get("webpage_url") or entry.get("url")
        if not url and vid and is_youtube(entry):
            url = f"https://www.youtube.com/watch?v={vid}"
        return url

    @staticmethod
    def _site_info(entry):
        return {k: entry[k] for k in ("extractor_key", "ie_key", "webpage_url_domain") if entry.get(k)}

    # --- thumbnail and subtitles for archive.org ---
    def _extra_dir(self):
        d = self.staging / "extra"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _ffmpeg(self, *args):
        return self._run_tool([str(FFMPEG_DIR / ("ffmpeg" + EXE)), "-hide_banner", "-loglevel", "error",
                               "-nostdin", "-y", *map(str, args)])

    def _thumbnail_for(self, path, meta):
        """A JPG for the item's thumbnail: the one saved with the download, the cover
        inside the file, the site's thumbnail, or else a frame from the video."""
        d = self._extra_dir()
        saved = d / (path.stem + ".jpg")
        if saved.is_file() and saved.stat().st_size:
            return saved
        out = d / (path.stem + ".thumb.jpg")
        audio = path.suffix.lower().lstrip(".") in self.AUDIO_EXTS
        if audio:   # cover art inside an MP3/M4A
            code, _ = self._ffmpeg("-i", path, "-an", "-map", "0:v:0", "-frames:v", "1", out)
            if code == 0 and out.is_file() and out.stat().st_size:
                return out
        url = str(meta.get("thumbnail") or "")
        if re.match(r"https?://", url):
            raw = d / (path.stem + ".thumb.src")
            try:
                raw.write_bytes(_download(url, limit=20 << 20))
                code, _ = self._ffmpeg("-i", raw, "-frames:v", "1", "-q:v", "2", out)
                if code == 0 and out.is_file() and out.stat().st_size:
                    return out
            except Cancelled:
                raise
            except Exception:
                pass
        if not audio:   # a frame from about 10% into the video
            start = 5.0
            try:
                r = run_tool(FFMPEG_DIR / ("ffprobe" + EXE), ["-v", "error", "-show_entries", "format=duration",
                                                             "-of", "csv=p=0", str(path)], 60)
                start = max(0.0, float(r.stdout.strip()) * 0.1)
            except (ValueError, OSError, subprocess.SubprocessError):
                pass
            code, _ = self._ffmpeg("-ss", f"{start:.1f}", "-i", path, "-frames:v", "1", "-q:v", "2", out)
            if code == 0 and out.is_file() and out.stat().st_size:
                return out
        return None

    def _subtitles_for(self, path, meta, entry, vid):
        """Subtitles the creator made (not automatic ones), as .srt: [(file, language)].
        Fetched in a run of their own, so a subtitle problem never stops a download."""
        url = meta.get("webpage_url") or self._entry_url(entry, vid)
        if not url or entry.get("_yta_id"):
            return []
        d = self._extra_dir() / "subs"
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True)
        self.ui.progress(None, "Getting subtitles...")
        try:
            self._run(["--skip-download", "--no-playlist", "--ignore-errors", "--write-subs",
                       "--sub-langs", "all,-live_chat", "--convert-subs", "srt",
                       "--ffmpeg-location", str(FFMPEG_DIR), "-P", f"home:{d}", "-P", f"temp:{d}",
                       "-o", "subs.%(ext)s", *self._link_args(entry), "--", url], lambda line: None)
        except Cancelled:
            raise
        except Exception:
            return []
        found = []
        for f in sorted(d.glob("subs.*.srt")):
            lang = f.name[len("subs."):-len(".srt")]
            if lang and f.stat().st_size:
                found.append((f, lang))
        return found

    def _upload_extras(self, ident, path, meta, mediatype, entry, vid, topics):
        extras = []
        try:
            thumb = self._thumbnail_for(path, meta)
            if thumb:
                extras.append((thumb, "__ia_thumb.jpg", "thumbnail"))
            for f, lang in self._subtitles_for(path, meta, entry, vid):
                extras.append((f, f"{path.stem}.{lang}.srt", f"{lang} subtitles"))
        except Cancelled:
            raise
        except Exception as exc:
            self.ui.log(f"Note: couldn't prepare the thumbnail/subtitles ({exc}).", "warn")
        done = []
        for f, remote, label in extras:
            names = [remote] + ([f"{path.stem}.thumb.jpg"] if remote == "__ia_thumb.jpg" else [])
            for i, name in enumerate(names):
                try:
                    self.ui.progress(None, f"Uploading the {label}...")
                    ia_upload(self.archive["keys"], ident, f, meta, mediatype, lambda *a: None, self.cancel,
                              on_status=lambda t: self.ui.progress(None, t), topics=topics, remote_name=name)
                    done.append(label if i == 0 else f"{label} (as {name})")
                    break
                except Cancelled:
                    raise
                except Exception as exc:
                    if getattr(exc, "account", False):
                        self.ui.log(f"Note: couldn't upload the {label} ({exc}).", "warn")
                        break
                    if i + 1 == len(names):
                        self.ui.log(f"Note: couldn't upload the {label} ({exc}).", "warn")
        if done:
            self.ui.log("   Also uploaded: " + ", ".join(done))

    def _refresh_item(self, ident, item, meta, entry, vid, path):
        """Bring an item uploaded earlier up to date: add topics, set your author and,
        if you chose so, take out the source site. Hand-added topics are kept."""
        if not meta or not meta.get("title"):
            meta = self._fetch_meta(entry, vid, path)
        meta = {**self._site_info(entry), **meta}
        pm = prepare_meta(meta, self.hide_source, self.author)
        md = item.get("metadata") or {}
        fields, remove, notes = {}, [], []

        old = existing_topics(item)
        site = site_topic_keys(meta) if self.hide_source else set()
        kept = [t for t in old if _topic_key(t) not in site]
        merged = merge_topics(kept, build_topics(pm, self.archive.get("topics") or ()))
        if merged != old:
            fields["subject"] = merged
            if merged[len(kept):]:
                notes.append("new topics: " + ", ".join(merged[len(kept):]))
        lang = ia_language(meta)
        if lang and not md.get("language"):
            fields["language"] = lang
            notes.append("language")
        if self.author and str(md.get("creator") or "") != self.author:
            fields["creator"] = self.author
            notes.append("author: " + self.author)
        lic = self.archive.get("license") or ""
        if lic and str(md.get("licenseurl") or "") != lic:
            fields["licenseurl"] = lic
            notes.append("license")
        if self.hide_source:
            remove = [k for k in ("source", "external-identifier") if md.get(k)]
            title = md.get("title")
            if isinstance(title, str) and scrub_title(title, meta) != title:
                fields["title"] = scrub_title(title, meta)
            desc = md.get("description")
            if isinstance(desc, str) and scrub_description_html(desc, meta) != desc:
                fields["description"] = scrub_description_html(desc, meta)
            if remove or "title" in fields or "description" in fields or len(kept) < len(old):
                notes.append("site left out")
        if not fields and not remove:
            self.ui.log("   Already up to date.")
            return
        self.ui.progress(None, "Updating the item on archive.org...")
        ia_update_metadata(self.archive["keys"], ident, fields, remove)
        self.retopiced += 1
        self.ui.log("   Updated: " + "; ".join(notes or ["details"]), "ok")

    def _ask(self, question, title):
        try:
            return self.ui.confirm(question, title)
        except TypeError:          # a ui whose confirm() takes only the question
            return self.ui.confirm(question)

    def _defer_blocked(self, ident, item, path, meta, vid, entry):
        """The video's item is blocked on archive.org. Don't stop the run to ask: note it
        and ask once at the end (or skip it silently if you said not to ask again)."""
        if ident in blocked_skips():
            self.skipped += 1
            self.ui.log(f"Not uploaded: {path.name} (its archive.org item is blocked; you chose to skip it).")
            return
        why = ("archive.org has hidden (\"darked\") it" if item.get("is_dark")
               else "archive.org removed or blocks it")
        self.blocked.append({"path": path, "meta": meta, "vid": vid, "entry": entry, "ident": ident, "why": why})
        self.ui.log(f"Waiting: {path.name}: its item https://archive.org/details/{ident} is blocked ({why}). "
                    "You'll be asked about it at the end; the other videos carry on.", "warn")

    def _review_blocked(self):
        """At the end of the run: one question about all blocked videos."""
        if not self.blocked or self.cancel.is_set():
            return
        try:
            choice = self.ui.review_blocked([dict(b, name=b["path"].name) for b in self.blocked])
        except AttributeError:      # a ui without the review window: skip them
            choice = {"upload": [], "never": False}
        chosen = set(choice.get("upload") or [])
        skipped = [b for b in self.blocked if b["ident"] not in chosen]
        if choice.get("never"):
            add_blocked_skips([b["ident"] for b in skipped])
        for b in skipped:
            self.failed.append(("archive.org upload of " + b["path"].name,
                                f"Its item https://archive.org/details/{b['ident']} is blocked on archive.org "
                                f"({b['why']}); not uploaded."))
        for b in self.blocked:
            if b["ident"] not in chosen:
                continue
            if self.cancel.is_set():
                raise Cancelled()
            new = free_item_name(b["ident"])
            if not new:
                self.failed.append(("archive.org upload of " + b["path"].name,
                                    f"No free new item name found next to {b['ident']}."))
                continue
            remember_rename(b["ident"], new)
            self.ui.log(f"Uploading {b['path'].name} under the new item name {new}.", "warn")
            self.ui.now(b["path"].name)
            self._archive(b["path"], b["meta"], b["vid"], b["entry"])
        self.blocked = []

    def _archive(self, path, meta, vid, entry):
        """Upload one saved file. Upload problems are reported, the job goes on."""
        if self.upload_blocked:
            self.skipped += 1
            self.ui.log(f"Not uploaded: {path.name} (archive.org refused the account earlier in this run; "
                        "use Upload files... later).", "warn")
            return
        if not vid:
            self.failed.append(("archive.org: " + path.name, "The site gave no video id, so no item name."))
            return
        if meta:
            meta = self._overrides(meta, entry)
        site = site_key(meta or {}) or site_key(self._overrides({}, entry)) or site_key(entry)
        if not site:   # an older file and a bare link: ask yt-dlp which site it is
            fetched = self._fetch_meta(entry, vid, path)
            meta = {**fetched, **(meta or {})} if meta and meta.get("title") else fetched
            site = site_key(meta)
        a = self.archive
        names = item_names(a.get("prefix"), vid, site or "youtube", self.hide_source, a.get("trial"))
        try:
            self.ui.progress(None, "Checking archive.org for " + names[0] + "...")
            ident, item, note = pick_item(names, a.get("username"))
            if note:
                self.ui.log(f"Note: {note} Uploading anyway.", "warn")
            if item_is_blocked(item):
                self._defer_blocked(ident, item, path, meta, vid, entry)
                return
            page = "https://archive.org/details/" + ident
            if item:
                owner = str((item.get("metadata") or {}).get("uploader") or "").lower()
                me = str(self.archive.get("username") or "").lower()
                if owner and me and owner != me:
                    raise IAError(f"\"{ident}\" is someone else's item. Change your identifier "
                                  "prefix under Options > archive.org account.")
                if path.name in {f.get("name") for f in item.get("files") or []}:
                    self.on_ia += 1
                    self.ui.log("Already on archive.org: " + page)
                    if self.archive.get("refresh_topics"):
                        self._refresh_item(ident, item, meta, entry, vid, path)
                    return
            if not meta or not meta.get("title"):
                meta = self._fetch_meta(entry, vid, path)
            meta = prepare_meta({**self._site_info(entry), **meta}, self.hide_source, self.author)
            meta["_trial"], meta["_license"] = bool(a.get("trial")), a.get("license") or ""
            topics = build_topics(meta, self.archive.get("topics") or ())

            def progress(done, total, speed, eta):
                pct, text = describe_progress({"downloaded_bytes": done, "total_bytes": total,
                                               "speed": speed, "eta": eta})
                self.ui.progress(pct, f"Uploading to archive.org: {pct or 0:.0f}%, {text}")

            audio = self.mode == "mp3" or path.suffix.lower().lstrip(".") in self.AUDIO_EXTS
            ia_upload(self.archive["keys"], ident, path, meta,
                      "audio" if audio else "movies", progress, self.cancel,
                      on_status=lambda t: self.ui.progress(None, t), topics=topics)
            self.uploaded += 1
            self.ui.log("Uploaded: " + page + ("  (TRIAL: deleted after about 30 days)" if a.get("trial")
                                               else "  (can take a few minutes to show up)"), "ok")
            self.ui.log("   Topics: " + ", ".join(topics))
            if a.get("extras"):
                self._upload_extras(ident, path, meta, "audio" if audio else "movies", entry, vid, topics)
        except Cancelled:
            raise
        except IAError as exc:
            self.failed.append(("archive.org upload of " + path.name, str(exc)))
            if getattr(exc, "account", False):
                self.upload_blocked = str(exc)
                self.ui.log("archive.org refused the account, so the other videos in this run are only "
                            "downloaded. Upload them later with Upload files...", "err")
        except Exception as exc:
            self.failed.append(("archive.org upload of " + path.name, str(exc)[:300] or "Unknown error"))

    def _upload_files(self):
        """Upload files that were downloaded earlier, exactly like a fresh download's."""
        total = len(self.files)
        for n, item in enumerate(self.files, 1):
            if self.cancel.is_set():
                raise Cancelled()
            path = Path(item["path"])
            self.ui.now(f"{n} of {total}: {path.name}" if total > 1 else path.name)
            self.ui.progress(0, "Starting...")
            if not path.is_file():
                self.failed.append((path.name, "The file is gone (moved, renamed or deleted since the scan)."))
                continue
            meta, entry = dict(item.get("meta") or {}), dict(item.get("entry") or {})
            try:
                if item.get("lookup"):
                    # older file: get tags, categories etc. from the site again
                    fetched = self._fetch_meta(entry, item["vid"], path)
                    if not fetched.get("_fallback"):
                        meta = {**meta, **fetched}
                self._archive(path, meta, item["vid"], entry)
            except Cancelled:
                raise
            except Exception as exc:
                self.failed.append((path.name, clean_error(exc)))

    def _acquire_tools(self):
        if TOOL_LOCK.acquire(blocking=False):
            return
        self.ui.now("Waiting for the yt-dlp update to finish...")
        while not TOOL_LOCK.acquire(timeout=0.3):
            if self.cancel.is_set():
                raise Cancelled()

    def run(self):
        try:
            self._acquire_tools()
        except Cancelled:
            self.cancelled = True
            return
        try:
            self.out_dir.mkdir(parents=True, exist_ok=True)
            parent = self.staging.parent
            _rmtree_retry(parent)  # leftovers of a crash; only one app instance can run
            self.staging.mkdir(parents=True)
            _hide(parent)
            if self.files is not None:
                self._upload_files()
            for line in ([] if self.files is not None else self.urls):
                if self.cancel.is_set():
                    raise Cancelled()
                url, opts = parse_link_line(line)
                try:
                    title, items = self._resolve(url, opts)
                except Cancelled:
                    raise
                except Exception as exc:
                    self.failed.append((url, clean_error(exc)))
                    continue
                for n, entry in enumerate(items, 1):
                    try:
                        self._do_video(entry, title, n, len(items))
                    except Cancelled:
                        raise
                    except Exception as exc:
                        label = entry.get("title") or entry.get("url") or url
                        self.failed.append((label, clean_error(exc)))
            self._review_blocked()
        except Cancelled:
            self.cancelled = True
        finally:
            _rmtree_retry(self.staging.parent)
            TOOL_LOCK.release()

    def summary(self):
        parts = [] if self.files is not None else [f"{self.saved} saved"]
        if self.existing:
            parts.append(f"{self.existing} already in folder")
        if self.uploaded:
            parts.append(f"{self.uploaded} uploaded")
        if self.on_ia:
            parts.append(f"{self.on_ia} already on archive.org")
        if self.retopiced:
            parts.append(f"{self.retopiced} updated on archive.org")
        if self.skipped:
            parts.append(f"{self.skipped} skipped")
        if self.failed:
            parts.append(f"{len(self.failed)} failed")
        return ("Stopped: " if self.cancelled else "Finished: ") + (", ".join(parts) or "nothing to do")


# --------------------------------------------------------------------------
# Window
# --------------------------------------------------------------------------

class GuiUI:
    """Passes messages from the download thread to the window."""

    def __init__(self, events):
        self.events = events

    def log(self, text, tag=None):
        self.events.put(("log", text, tag))

    def now(self, text):
        self.events.put(("now", text))

    def progress(self, pct, detail):
        self.events.put(("progress", pct, detail))

    def review_blocked(self, items):
        answered, box = threading.Event(), {}
        self.events.put(("blocked", items, box, answered))
        answered.wait()
        return box.get("answer") or {"upload": [], "never": False}

    def confirm(self, question, title="Large playlist"):
        answered, box = threading.Event(), {}
        self.events.put(("confirm", question, box, answered, title))
        answered.wait()
        return box.get("answer", False)


def run_gui():
    import tkinter as tk
    import tkinter.font as tkfont
    from tkinter import filedialog, messagebox, ttk

    if sys.stdout is None:  # started without a console
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")
    if sys.platform == "win32":
        try:  # crisp pixels instead of Windows blurring the whole window on high-DPI screens
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

    migration_note = migrate_old_data()
    root = tk.Tk()
    root.title(APP_NAME)
    try:
        root._yta_icons = [tk.PhotoImage(data=base64.b64encode(icon_png(s)).decode("ascii"))
                           for s in (3, 2, 1)]
        root.iconphoto(True, *root._yta_icons)
    except tk.TclError:
        pass  # the exe's own icon is still used
    if not acquire_single_instance():
        root.withdraw()
        messagebox.showinfo(APP_NAME, f"{APP_NAME} is already open.")
        root.destroy()
        return
    scale = max(root.winfo_fpixels("1i") / 96, 1.0)

    def px(n):
        return max(1, round(n * scale))

    # Classic Windows palette, plus the old CGA colours for the log
    FACE, SHADOW, BLACK, WHITE = "#C0C0C0", "#808080", "#000000", "#FFFFFF"
    NAVY, TITLE_END, MAROON = "#000080", "#1084D0", "#800000"
    LOG_BG, LOG_FG, LOG_OK, LOG_WARN, LOG_ERR = "#000000", "#C0C0C0", "#55FF55", "#FFFF55", "#FF5555"

    families = set(tkfont.families(root))

    def pick(*names):
        return next((n for n in names if n in families), names[-1])

    UI_FONT = pick("Tahoma", "Microsoft Sans Serif", "Arial", "Helvetica")
    MONO = pick("Lucida Console", "Courier New", "Courier")
    F, FB, FMONO = (UI_FONT, 9), (UI_FONT, 9, "bold"), (MONO, 9)

    root.configure(bg=FACE)
    root.option_add("*TCombobox*Listbox.font", F)
    root.option_add("*TCombobox*Listbox.selectBackground", NAVY)
    style = ttk.Style(root)
    for theme in ("winnative", "classic"):
        if theme in style.theme_names():
            style.theme_use(theme)
            break
    style.configure("TCombobox", font=F)

    events = queue.Queue()
    settings = load_settings()
    state = {"job": None, "closing": False, "ready": False}
    sound = tk.BooleanVar(value=settings.get("sound", True))
    cookies_mode = tk.StringVar(value=cookie_mode())

    # ---- building blocks ----
    def button(parent, text, command, width=10, default=False):
        """A raised 3D button. The default one gets the thick black outline."""
        holder = tk.Frame(parent, bg=BLACK if default else FACE, padx=1, pady=1)
        b = tk.Button(holder, text=text, command=command, width=width, font=FB if default else F,
                      bg=FACE, activebackground=FACE, fg=BLACK, disabledforeground=SHADOW,
                      relief="raised", bd=2, highlightthickness=0, padx=px(4), pady=px(2))
        b.pack()
        return holder, b

    def group(parent, title):
        return tk.LabelFrame(parent, text=f" {title} ", bg=FACE, fg=BLACK, font=F,
                             relief="groove", bd=2, padx=px(8), pady=px(6))

    def label(parent, text="", **kw):
        return tk.Label(parent, text=text, bg=FACE, fg=kw.pop("fg", BLACK),
                        font=kw.pop("font", F), anchor="w", justify="left", **kw)

    field = dict(bg=WHITE, fg=BLACK, relief="sunken", bd=2, highlightthickness=0, font=F,
                 selectbackground=NAVY, selectforeground=WHITE, insertbackground=BLACK)

    class BlockBar(tk.Canvas):
        """The chunky navy-block progress bar from the file-copy dialogs."""

        def __init__(self, parent):
            super().__init__(parent, height=px(20), bg=FACE, relief="sunken", bd=2,
                             highlightthickness=0)
            self.value = 0
            self.bind("<Configure>", lambda e: self.draw())

        def set(self, value):
            self.value = max(0, min(100, value))
            self.draw()

        def draw(self):
            self.delete("all")
            w, h = self.winfo_width() - 4, self.winfo_height() - 4
            block, gap, pad = px(8), px(2), px(2)
            fill_to = pad + (w - 2 * pad) * self.value / 100
            x = pad + 2
            while x + block <= fill_to + 2:
                self.create_rectangle(x, pad + 2, x + block, h - pad + 2, fill=NAVY, width=0)
                x += block + gap

    # ---- header: teal gradient banner with the app's pixel icon ----
    banner = tk.Canvas(root, height=px(56), highlightthickness=0, bd=0, bg="#005050")
    banner.pack(fill="x")

    def draw_banner(event=None):
        banner.delete("all")
        w, h = banner.winfo_width(), banner.winfo_height()
        steps = 48
        for i in range(steps):
            t = i / (steps - 1)
            c = "#%02x%02x%02x" % tuple(round(a + (b - a) * t) for a, b in
                                        zip((0x00, 0x50, 0x50), (0x10, 0xA0, 0x90)))
            banner.create_rectangle(w * i / steps, 0, w * (i + 1) / steps + 1, h, fill=c, width=0)
        cell = px(3)
        top, left = (h - cell * len(ICON_ROWS)) // 2, px(12)
        for y, row in enumerate(ICON_ROWS):
            for x, ch in enumerate(row):
                if ch in ICON_COLORS:
                    banner.create_rectangle(left + x * cell, top + y * cell, left + (x + 1) * cell,
                                            top + (y + 1) * cell, fill=ICON_COLORS[ch], width=0)
        tx = left + cell * len(ICON_ROWS[0]) + px(12)
        banner.create_text(tx + 2, h // 2 - px(7) + 2, text="Video Archiver", anchor="w",
                           fill=BLACK, font=(UI_FONT, 15, "bold"))
        banner.create_text(tx, h // 2 - px(7), text="Video Archiver", anchor="w",
                           fill=WHITE, font=(UI_FONT, 15, "bold"))
        banner.create_text(tx, h // 2 + px(13), text=APP_BLURB,
                           anchor="w", fill="#C8F0E8", font=F)
    banner.bind("<Configure>", draw_banner)
    tk.Frame(root, height=2, bg=WHITE).pack(fill="x")

    body = tk.Frame(root, bg=FACE, padx=px(10), pady=px(8))
    body.pack(fill="both", expand=True)
    body.columnconfigure(0, weight=1)

    # ---- step 1: links ----
    g1 = group(body, "Step 1: Paste your links: YouTube, TikTok, Vimeo, SoundCloud... (one per line)")
    g1.grid(row=0, column=0, sticky="nsew")
    g1.columnconfigure(0, weight=1)
    links = tk.Text(g1, height=4, wrap="none", padx=px(4), pady=px(3), **field)
    links.grid(row=0, column=0, rowspan=2, sticky="nsew")

    def paste():
        try:
            text = root.clipboard_get().strip()
        except tk.TclError:
            return
        current = links.get("1.0", "end").strip()
        links.insert("end", ("\n" if current else "") + text)

    h, _ = button(g1, "Paste", paste, width=8)
    h.grid(row=0, column=1, sticky="n", padx=(px(8), 0))
    h, _ = button(g1, "Clear", lambda: links.delete("1.0", "end"), width=8)
    h.grid(row=1, column=1, sticky="n", padx=(px(8), 0), pady=(px(4), 0))

    # ---- step 2: format ----
    g2 = group(body, "Step 2: Choose a format")
    g2.grid(row=1, column=0, sticky="ew", pady=(px(8), 0))
    mode = tk.StringVar(value=settings.get("mode", "mp3"))
    quality = tk.StringVar()
    playlist = tk.BooleanVar(value=False)
    radio = dict(bg=FACE, activebackground=FACE, selectcolor=WHITE, highlightthickness=0, font=F)

    top = tk.Frame(g2, bg=FACE)
    top.pack(fill="x")
    quality_box = ttk.Combobox(top, textvariable=quality, state="readonly", width=28, font=F)
    hint = label(g2, wraplength=px(440))

    def update_hint(*_):
        if mode.get() == "mp3":
            hint.configure(text=MP3_HINT, fg=BLACK)
        else:
            uhd = MP4_QUALITIES.get(quality.get(), 1080) > 1080
            hint.configure(text=MP4_HINT_UHD if uhd else MP4_HINT_HD, fg=MAROON if uhd else BLACK)

    def on_mode_change(*_):
        table = MP3_QUALITIES if mode.get() == "mp3" else MP4_QUALITIES
        quality_box["values"] = list(table)
        saved = settings.get("quality_" + mode.get())
        quality.set(saved if saved in table else next(iter(table)))
        update_hint()

    def on_quality_change(*_):
        settings["quality_" + mode.get()] = quality.get()
        save_settings(mode=mode.get(), **{"quality_" + mode.get(): quality.get()})
        update_hint()

    tk.Radiobutton(top, text="MP3 (music)", value="mp3", variable=mode,
                   command=lambda: (on_mode_change(), save_settings(mode="mp3")), **radio).pack(side="left")
    tk.Radiobutton(top, text="MP4 (video)", value="mp4", variable=mode,
                   command=lambda: (on_mode_change(), save_settings(mode="mp4")), **radio).pack(side="left", padx=(px(10), px(18)))
    label(top, "Quality:").pack(side="left", padx=(0, px(6)))
    quality_box.pack(side="left")
    quality_box.bind("<<ComboboxSelected>>", on_quality_change)
    hint.pack(fill="x", pady=(px(6), 0))
    tk.Checkbutton(g2, text="If a link is a playlist, download all of it "
                   f"(asks first above {PLAYLIST_CONFIRM_OVER} videos)",
                   variable=playlist, **radio).pack(anchor="w", pady=(px(4), 0))
    on_mode_change()

    # ---- step 3: folder ----
    g3 = group(body, "Step 3: Save to")
    g3.grid(row=2, column=0, sticky="ew", pady=(px(8), 0))
    g3.columnconfigure(0, weight=1)
    out_dir = tk.StringVar(value=settings.get("out_dir") or str(DEFAULT_OUT))
    out_entry = tk.Entry(g3, textvariable=out_dir, **field)
    out_entry.grid(row=0, column=0, sticky="ew", ipady=px(2))

    def remember_dir(*_):
        path = out_dir.get().strip()
        if path and Path(path).is_absolute():
            save_settings(out_dir=path)

    def choose_dir():
        start_at = out_dir.get() if Path(out_dir.get()).is_dir() else str(Path.home())
        chosen = filedialog.askdirectory(initialdir=start_at, title="Where should downloads go?")
        if chosen:
            out_dir.set(str(Path(chosen)))
            remember_dir()
            write_log("Downloads will now go to: " + out_dir.get())

    def reset_dir():
        out_dir.set(str(DEFAULT_OUT))
        remember_dir()
        write_log("Download folder reset to: " + out_dir.get())

    def ensure_folder():
        """True if the folder is usable. Asks before creating one that doesn't exist."""
        path = out_dir.get().strip()
        if (problem := folder_problem(path)):
            messagebox.showerror("Folder problem", problem + "\n\nPick another folder with Browse...")
            return False
        if not Path(path).exists():
            if Path(path) != DEFAULT_OUT and not messagebox.askyesno(
                    "Create folder?", f"This folder doesn't exist yet:\n\n{path}\n\nCreate it?"):
                return False
            if (problem := create_folder(path)):
                messagebox.showerror("Folder problem", problem)
                return False
        return True

    def open_dir():
        if not ensure_folder():
            return
        path = Path(out_dir.get().strip())
        if sys.platform == "win32":
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])

    out_entry.bind("<FocusOut>", remember_dir)
    out_entry.bind("<Return>", remember_dir)
    for col, (text, cmd) in enumerate((("Browse...", choose_dir), ("Default", reset_dir),
                                       ("Open", open_dir)), start=1):
        h, _ = button(g3, text, cmd, width=8)
        h.grid(row=0, column=col, padx=(px(6), 0))

    # ---- step 4: archive.org ----
    ga = group(body, "Step 4: Author, topics and archive.org (optional)")
    ga.grid(row=3, column=0, sticky="ew", pady=(px(8), 0))
    ga.columnconfigure(0, weight=1)
    upload = tk.BooleanVar(value=bool(settings.get("ia_upload")) and load_ia_keys() is not None)
    trial = tk.BooleanVar(value=bool(settings.get("ia_trial")))
    author_row = tk.Frame(ga, bg=FACE)
    author_row.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, px(6)))
    author_row.columnconfigure(1, weight=1)
    label(author_row, "Author:").grid(row=0, column=0, sticky="w")
    author_var = tk.StringVar(value=settings.get("author", ""))
    tk.Entry(author_row, textvariable=author_var, **field).grid(
        row=0, column=1, sticky="ew", padx=(px(6), 0), ipady=px(1))
    label(author_row, "Written into each file and used as the creator on archive.org. "
                      "Leave empty to use the uploader's name.", fg=SHADOW,
          wraplength=px(440)).grid(row=1, column=0, columnspan=2, sticky="w")
    ia_status = label(ga, " ")
    ia_status.grid(row=2, column=0, sticky="w", pady=(px(4), 0))

    def refresh_ia_status():
        st = load_settings()
        if load_ia_keys() is None:
            ia_status.configure(text="Account: not set up yet. Press Account... to add your keys.",
                                fg=SHADOW)
        else:
            who = st.get("ia_screenname") or "keys saved"
            prefix = (st.get("ia_prefix") or "yt") + ("-test" if trial.get() else "")
            text = f"Account: {who}   Items: archive.org/details/{prefix}-<video id>"
            if trial.get():
                ia_status.configure(text="TRIAL MODE (deleted after ~30 days).  " + text, fg=MAROON)
            else:
                ia_status.configure(text=text, fg=BLACK)

    def toggle_upload():
        if upload.get() and load_ia_keys() is None:
            upload.set(False)
            ia_dialog(enable_after=True)
            return
        if upload.get() and not load_settings().get("ia_warned"):
            if not messagebox.askokcancel(
                    "Uploading to archive.org",
                    "Items on archive.org are public: anyone can see and download them.\n\n"
                    "Only upload videos you have the right to share there (your own videos, "
                    "public domain, Creative Commons...). archive.org removes items that break "
                    "its terms.\n\nTurn uploading on?"):
                upload.set(False)
                return
            save_settings(ia_warned=True)
        save_settings(ia_upload=upload.get())
        write_log("archive.org uploads " + ("on." if upload.get() else "off."))

    tk.Checkbutton(ga, text="Also upload every saved file to my archive.org account",
                   variable=upload, command=toggle_upload, **radio).grid(row=1, column=0, sticky="w")
    h, _ = button(ga, "Account...", lambda: ia_dialog(), width=13)
    h.grid(row=1, column=1, padx=(px(6), 0))
    h, _ = button(ga, "Upload files...", lambda: folder_upload_dialog(), width=13)
    h.grid(row=2, column=1, padx=(px(6), 0), pady=(px(4), 0))

    topics_row = tk.Frame(ga, bg=FACE)
    topics_row.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(px(6), 0))
    topics_row.columnconfigure(1, weight=1)
    label(topics_row, "Your topics:").grid(row=0, column=0, sticky="w")
    topics_var = tk.StringVar(value=settings.get("ia_topics", ""))
    tk.Entry(topics_row, textvariable=topics_var, **field).grid(
        row=0, column=1, sticky="ew", padx=(px(6), 0), ipady=px(1))
    label(topics_row, "Added to every video in this batch, separated by commas, e.g. "
                      "Pokémon TCG, booster opening. Tags, hashtags and keywords from each "
                      "video are added automatically.", fg=SHADOW, wraplength=px(440)).grid(
        row=1, column=0, columnspan=2, sticky="w")
    refresh_topics = tk.BooleanVar(value=bool(settings.get("ia_refresh_topics")))
    tk.Checkbutton(ga, text="Also update videos already on archive.org (topics, author, site)",
                   variable=refresh_topics, **radio,
                   command=lambda: save_settings(ia_refresh_topics=refresh_topics.get())).grid(
        row=4, column=0, columnspan=2, sticky="w", pady=(px(2), 0))
    hide_source = tk.BooleanVar(value=settings.get("hide_source", False))
    tk.Checkbutton(ga, text="Leave out which site videos come from (title, description, topics, "
                            "links, and inside the files)",
                   variable=hide_source, **radio,
                   command=lambda: save_settings(hide_source=hide_source.get())).grid(
        row=5, column=0, columnspan=2, sticky="w")
    extras = tk.BooleanVar(value=settings.get("ia_extras", True))
    tk.Checkbutton(ga, text="Also upload the thumbnail and subtitles", variable=extras, **radio,
                   command=lambda: save_settings(ia_extras=extras.get())).grid(
        row=6, column=0, columnspan=2, sticky="w")
    lic_row = tk.Frame(ga, bg=FACE)
    lic_row.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(px(4), 0))
    label(lic_row, "License:").pack(side="left")
    license_var = tk.StringVar(value=settings.get("ia_license") if settings.get("ia_license") in LICENSES
                               else next(iter(LICENSES)))
    lic_box = ttk.Combobox(lic_row, textvariable=license_var, state="readonly", values=list(LICENSES),
                           width=30, font=F)
    lic_box.pack(side="left", padx=(px(6), px(8)))
    lic_box.bind("<<ComboboxSelected>>", lambda e: save_settings(ia_license=license_var.get()))
    label(lic_row, "Only pick a license for videos you hold the rights to.", fg=SHADOW).pack(side="left")

    def ia_dialog(enable_after=False):
        dlg = tk.Toplevel(root)
        dlg.title("archive.org account")
        dlg.configure(bg=FACE)
        dlg.transient(root)
        dlg.resizable(False, False)
        frm = tk.Frame(dlg, bg=FACE, padx=px(12), pady=px(10))
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)
        label(frm, "Paste the two keys from your archive.org account's API keys page:").grid(
            row=0, column=0, columnspan=2, sticky="w")
        link = tk.Label(frm, text=IA_KEYS_PAGE, bg=FACE, fg="#0000EE", cursor="hand2",
                        font=(UI_FONT, 9, "underline"))
        link.grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, px(8)))
        link.bind("<Button-1>", lambda e: webbrowser.open(IA_KEYS_PAGE))

        cur = load_ia_keys() or ("", "")
        access, secret = tk.StringVar(value=cur[0]), tk.StringVar(value=cur[1])
        prefix = tk.StringVar(value=load_settings().get("ia_prefix", ""))
        rows = (("Access key:", access, None), ("Secret key:", secret, "\u2022"),
                ("Identifier prefix:", prefix, None))
        for r, (text, var, show) in enumerate(rows, start=2):
            label(frm, text).grid(row=r, column=0, sticky="w", pady=px(2))
            e = tk.Entry(frm, textvariable=var, width=40, show=show or "", **field)
            e.grid(row=r, column=1, sticky="ew", padx=(px(6), 0), pady=px(2), ipady=px(1))
        label(frm, "Each video becomes its own item named <prefix>-<video id>. Item names are "
                   "shared by everyone on archive.org, so pick something yours. Leave it empty "
                   "to use your screen name.", fg=SHADOW, wraplength=px(380)).grid(
            row=5, column=0, columnspan=2, sticky="w", pady=(px(4), 0))
        label(frm, "The keys are stored encrypted for your Windows account only.",
              fg=SHADOW).grid(row=6, column=0, columnspan=2, sticky="w", pady=(px(4), 0))
        msg = label(frm, " ", fg=MAROON, wraplength=px(380))
        msg.grid(row=7, column=0, columnspan=2, sticky="w", pady=(px(6), 0))
        btns = tk.Frame(frm, bg=FACE)
        btns.grid(row=8, column=0, columnspan=2, sticky="e", pady=(px(8), 0))

        def finish(a, s, screen, user):
            p = clean_prefix(prefix.get())
            if not p:
                p = (clean_prefix(screen) + "-yt") if clean_prefix(screen) else "yta-" + uuid.uuid4().hex[:6]
            try:
                save_ia_keys(a, s)
            except OSError as exc:
                msg.configure(text=f"Couldn't save the keys: {exc.strerror or exc}")
                return
            save_settings(ia_prefix=p, ia_screenname=screen, ia_username=user)
            refresh_ia_status()
            write_log(f"archive.org account saved ({screen or 'not verified'}). Item prefix: {p}", "ok")
            dlg.destroy()
            if enable_after:
                upload.set(True)
                toggle_upload()

        def save():
            a, s = access.get().strip(), secret.get().strip()
            if not a or not s:
                msg.configure(text="Paste both keys first.")
                return
            save_b.configure(state="disabled")
            msg.configure(text="Checking the keys with archive.org...", fg=BLACK)
            result, done = {}, threading.Event()

            def work():
                try:
                    result["ok"] = ia_check_auth((a, s))
                except IAError as exc:
                    result["err"], result["rejected"] = str(exc), exc.rejected
                done.set()
            threading.Thread(target=work, daemon=True).start()

            def wait():
                if not dlg.winfo_exists():
                    return
                if not done.is_set():
                    dlg.after(100, wait)
                    return
                save_b.configure(state="normal")
                msg.configure(fg=MAROON)
                if "ok" in result:
                    finish(a, s, *result["ok"])
                elif result.get("rejected"):
                    msg.configure(text=result["err"])
                elif messagebox.askyesno("Couldn't check the keys",
                                         result["err"] + "\n\nSave them anyway?", parent=dlg):
                    finish(a, s, "", "")
                else:
                    msg.configure(text=result["err"])
            wait()

        def remove():
            if messagebox.askyesno("Remove keys", "Remove the saved archive.org keys from this "
                                   "computer? Your uploads on archive.org are not affected.",
                                   parent=dlg):
                forget_ia_keys()
                save_settings(ia_screenname="", ia_username="", ia_upload=False)
                upload.set(False)
                refresh_ia_status()
                write_log("archive.org keys removed from this computer.")
                dlg.destroy()

        h, save_b = button(btns, "Check & Save", save, width=12, default=True)
        h.pack(side="left")
        if load_ia_keys() is not None:
            h, _ = button(btns, "Remove keys", remove, width=11)
            h.pack(side="left", padx=(px(6), 0))
        h, _ = button(btns, "Cancel", dlg.destroy, width=8)
        h.pack(side="left", padx=(px(6), 0))
        dlg.bind("<Return>", lambda e: save())
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        try:
            dlg.wait_visibility()
            dlg.grab_set()
        except tk.TclError:
            pass

    refresh_ia_status()

    # ---- step 5: go ----
    actions = tk.Frame(body, bg=FACE)
    actions.grid(row=4, column=0, sticky="ew", pady=(px(10), 0))
    go_holder, go = button(actions, "Download", None, width=14, default=True)
    go_holder.pack(side="left")
    stop_holder, stop = button(actions, "Stop", None, width=8)
    stop_holder.pack(side="left", padx=(px(8), 0))
    label(actions, "Tip: Ctrl+Enter also starts the download.", fg=SHADOW).pack(side="right")
    go.configure(state="disabled")
    stop.configure(state="disabled")

    # ---- progress ----
    g4 = group(body, "Progress")
    g4.grid(row=5, column=0, sticky="ew", pady=(px(8), 0))
    now = label(g4, "Getting ready...", font=FB)
    now.pack(fill="x")
    bar = BlockBar(g4)
    bar.pack(fill="x", pady=(px(4), px(3)))
    detail = label(g4, " ")
    detail.pack(fill="x")

    # ---- activity log: a little DOS box ----
    g5 = group(body, "Activity log")
    g5.grid(row=6, column=0, sticky="nsew", pady=(px(8), 0))
    body.rowconfigure(6, weight=1)
    log = tk.Text(g5, height=7, bg=LOG_BG, fg=LOG_FG, font=FMONO, relief="sunken", bd=2,
                  highlightthickness=0, padx=px(6), pady=px(4), state="disabled", wrap="word",
                  selectbackground=FACE, selectforeground=BLACK, insertbackground=LOG_FG)
    log.pack(fill="both", expand=True)
    log.tag_configure("ok", foreground=LOG_OK)
    log.tag_configure("warn", foreground=LOG_WARN)
    log.tag_configure("err", foreground=LOG_ERR)

    # ---- status bar ----
    status = tk.Frame(root, bg=FACE, padx=px(2), pady=px(2))
    status.pack(fill="x", side="bottom")
    status_left = tk.Label(status, text="Ready.", bg=FACE, font=F, anchor="w",
                           relief="sunken", bd=1, padx=px(4))
    status_left.pack(side="left", fill="x", expand=True)
    status_right = tk.Label(status, text="yt-dlp: starting", bg=FACE, font=F, anchor="w",
                            relief="sunken", bd=1, padx=px(4), width=22)
    status_right.pack(side="right", padx=(px(2), 0))

    g4.bind("<Configure>", lambda e: detail.configure(wraplength=max(e.width - px(20), 200)))
    g2.bind("<Configure>", lambda e: hint.configure(wraplength=max(e.width - px(20), 200)))

    def write_log(text, tag=None):
        log.configure(state="normal")
        log.insert("end", "> " + text + "\n", tag)
        log.see("end")
        log.configure(state="disabled")

    def set_busy(busy):
        go.configure(state="disabled" if busy or not state["ready"] else "normal")
        stop.configure(state="normal" if busy else "disabled")

    def beep():
        if sound.get() and sys.platform == "win32":
            try:
                import winsound
                winsound.MessageBeep(winsound.MB_ICONASTERISK)
            except Exception:
                pass

    def worker(job):
        try:
            job.run()
        except Exception as exc:
            job.failed.append(("setup", clean_error(exc)))
        events.put(("done", job))

    def archive_settings():
        """archive.org settings for a job, or None (after asking for keys) if they're missing."""
        keys = load_ia_keys()
        if keys is None:
            upload.set(False)
            messagebox.showinfo("archive.org", "Your archive.org keys are missing. "
                                "Add them with Account..., then start again.")
            ia_dialog()
            return None
        st = load_settings()
        save_settings(ia_topics=topics_var.get().strip())
        return {"keys": keys, "prefix": st.get("ia_prefix") or "",
                "topics": parse_topics(topics_var.get()),
                "refresh_topics": refresh_topics.get(),
                "username": st.get("ia_username") or "",
                "trial": trial.get(), "license": LICENSES.get(license_var.get(), ""),
                "extras": extras.get()}

    def start_folder_upload(folder, items):
        if state["job"] is not None or not state["ready"]:
            return
        archive = archive_settings()
        if archive is None:
            return
        save_settings(author=author_var.get().strip(), hide_source=hide_source.get())
        job = Job([], "mp4", 0, folder, False, GuiUI(events), cookies=cookie_args(), archive=archive,
                  author=author_var.get().strip(), hide_source=hide_source.get(), files=items)
        state["job"] = job
        set_busy(True)
        bar.set(0)
        status_left.configure(text="Working...")
        write_log(f"Uploading {len(items)} already-downloaded file(s) from {folder} to archive.org")
        threading.Thread(target=worker, args=(job,), daemon=True).start()

    def folder_upload_dialog():
        if state["job"] is not None:
            messagebox.showinfo(APP_NAME, "Finish or stop the current download first.")
            return
        if load_ia_keys() is None:
            messagebox.showinfo("archive.org", "Add your archive.org keys first.")
            ia_dialog()
            return
        dlg = tk.Toplevel(root)
        dlg.title("Upload downloaded files to archive.org")
        dlg.configure(bg=FACE)
        dlg.transient(root)
        dlg.minsize(px(560), px(420))
        frm = tk.Frame(dlg, bg=FACE, padx=px(12), pady=px(10))
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)
        label(frm, "Finds videos you've already downloaded and uploads the ones that aren't on archive.org "
                   "yet, with the same title, topics and author as a normal upload. Handy when an upload "
                   "failed, for example because the Wi-Fi dropped. Nothing is downloaded again.",
              wraplength=px(540)).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, px(8)))
        folder_var = tk.StringVar(value=out_dir.get().strip())
        sub_var = tk.BooleanVar(value=True)
        label(frm, "Folder:").grid(row=1, column=0, sticky="w")
        tk.Entry(frm, textvariable=folder_var, **field).grid(row=1, column=1, sticky="ew", padx=px(6), ipady=px(1))

        def browse():
            d = filedialog.askdirectory(parent=dlg, initialdir=folder_var.get() or str(Path.home()))
            if d:
                folder_var.set(str(Path(d)))
        h, _ = button(frm, "Browse...", browse, width=10)
        h.grid(row=1, column=2)
        tk.Checkbutton(frm, text="Include subfolders (playlists are saved in their own folder)",
                       variable=sub_var, **radio).grid(row=2, column=0, columnspan=2, sticky="w",
                                                       pady=(px(4), 0))
        h, scan_b = button(frm, "Scan", lambda: do_scan(), width=10, default=True)
        h.grid(row=2, column=2, pady=(px(4), 0))
        out = tk.Text(frm, height=16, width=90, bg=LOG_BG, fg=LOG_FG, font=FMONO, relief="sunken",
                      bd=2, wrap="none", state="disabled")
        out.grid(row=3, column=0, columnspan=3, sticky="nsew", pady=(px(8), 0))
        frm.rowconfigure(3, weight=1)
        btns = tk.Frame(frm, bg=FACE)
        btns.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(px(8), 0))
        found = {"items": [], "folder": None, "cancel": threading.Event()}

        def show(text):
            out.configure(state="normal")
            out.delete("1.0", "end")
            out.insert("end", text)
            out.configure(state="disabled")

        def do_scan():
            folder = folder_var.get().strip()
            if not folder or not Path(folder).is_dir():
                show("That folder doesn't exist. Pick the folder your downloads are in.")
                return
            if not TOOL_LOCK.acquire(blocking=False):
                show("Busy: wait until the current download or update has finished, then scan again.")
                return
            found["items"] = []
            upload_b.configure(state="disabled", text="Upload")
            scan_b.configure(state="disabled")
            st = load_settings()
            refresh = refresh_topics.get()
            status, result = {"text": "Looking for downloaded files..."}, {}

            def work():
                try:
                    result["items"] = scan_for_upload(
                        folder, sub_var.get(), st.get("ia_prefix") or "", st.get("ia_username") or "",
                        progress=lambda t: status.update(text=t), cancel=found["cancel"],
                        hide_site=hide_source.get(), trial=trial.get())
                except Cancelled:
                    result["error"] = "Stopped."
                except Exception as exc:
                    result["error"] = str(exc) or type(exc).__name__
                finally:
                    TOOL_LOCK.release()
            threading.Thread(target=work, daemon=True).start()

            def wait():
                if not dlg.winfo_exists():
                    return
                if not result:
                    show(status["text"])
                    dlg.after(200, wait)
                    return
                scan_b.configure(state="normal")
                if "error" in result:
                    show("Couldn't scan the folder: " + result["error"])
                    return
                report, todo = scan_report(result["items"], folder, refresh)
                show(report)
                found.update(items=todo, folder=folder)
                if todo:
                    upload_b.configure(state="normal", text=f"Upload {len(todo)} file(s)")
            wait()

        def do_upload():
            items, folder = found["items"], found["folder"]
            dlg.destroy()
            start_folder_upload(folder, items)

        def close():
            found["cancel"].set()
            dlg.destroy()
        h, upload_b = button(btns, "Upload", do_upload, width=18)
        h.pack(side="left")
        upload_b.configure(state="disabled")
        h, _ = button(btns, "Close", close, width=8)
        h.pack(side="right")
        dlg.protocol("WM_DELETE_WINDOW", close)
        dlg.bind("<Escape>", lambda e: close())
        show("Press Scan to look for downloaded files that aren't on archive.org yet.")

    def start():
        if state["job"] is not None or not state["ready"]:
            return
        urls = [u.strip() for u in links.get("1.0", "end").splitlines() if u.strip()]
        if not urls:
            now.configure(text="Paste at least one link first (Step 1).")
            links.focus_set()
            return
        if not ensure_folder():
            return
        archive = None
        if upload.get():
            archive = archive_settings()
            if archive is None:
                return
        save_settings(author=author_var.get().strip(), hide_source=hide_source.get())
        remember_dir()
        m = mode.get()
        q = (MP3_QUALITIES if m == "mp3" else MP4_QUALITIES)[quality.get()]
        job = Job(urls, m, q, out_dir.get().strip(), playlist.get(), GuiUI(events), cookies=cookie_args(),
                  archive=archive, author=author_var.get().strip(), hide_source=hide_source.get())
        state["job"] = job
        set_busy(True)
        bar.set(0)
        status_left.configure(text="Working...")
        write_log(f"Starting {len(urls)} link(s) as {m.upper()}, {quality.get()}"
                  + (", uploading to archive.org" if archive else ""))
        threading.Thread(target=worker, args=(job,), daemon=True).start()

    def cancel():
        job = state["job"]
        if job:
            job.cancel.set()
            stop.configure(state="disabled")
            now.configure(text="Stopping...")

    go.configure(command=start)
    stop.configure(command=cancel)

    def on_close():
        if state["job"] is None:
            root.destroy()
            return
        if messagebox.askyesno("Download in progress",
                               "Stop the download and close?\nUnfinished files will be removed."):
            state["closing"] = True
            cancel()
            now.configure(text="Stopping, then closing...")

    root.protocol("WM_DELETE_WINDOW", on_close)

    # ---- menu bar ----
    menubar = tk.Menu(root)
    m_file = tk.Menu(menubar, tearoff=0)
    m_file.add_command(label="Open download folder", underline=0, command=open_dir)
    m_file.add_command(label="Change download folder...", underline=0, command=choose_dir)
    m_file.add_separator()
    m_file.add_command(label="Exit", underline=1, command=on_close)
    m_opts = tk.Menu(menubar, tearoff=0)
    m_opts.add_checkbutton(label="Play a sound when finished", variable=sound,
                           command=lambda: save_settings(sound=sound.get()))
    m_cookies = tk.Menu(menubar, tearoff=0)
    for value, text in (("off", "Don't use cookies"), ("firefox", "From Firefox"),
                        ("file", "From a cookies.txt file (any browser)...")):
        m_cookies.add_radiobutton(label=text, value=value, variable=cookies_mode,
                                  command=lambda v=value: set_cookies(v))
    m_opts.add_cascade(label="Cookies (bot checks, sign-in-only videos)", menu=m_cookies)
    m_opts.add_checkbutton(label="archive.org trial mode (test uploads, deleted after ~30 days)",
                           variable=trial, command=lambda: toggle_trial())
    m_opts.add_separator()
    m_opts.add_command(label="archive.org account...", command=lambda: ia_dialog())
    def open_path(path):
        if sys.platform == "win32":
            os.startfile(path)
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])

    def simple_dialog(title, rows, intro=None, ok_text="OK", width=56):
        """Small form; returns {key: text} or None. rows: (key, label, initial, hint)."""
        dlg = tk.Toplevel(root)
        dlg.title(title)
        dlg.configure(bg=FACE)
        dlg.transient(root)
        dlg.resizable(False, False)
        frm = tk.Frame(dlg, bg=FACE, padx=px(12), pady=px(10))
        frm.pack(fill="both", expand=True)
        r = 0
        if intro:
            label(frm, intro, wraplength=px(460)).grid(row=r, column=0, columnspan=2, sticky="w",
                                                                       pady=(0, px(8)))
            r += 1
        vars_ = {}
        for key, text, initial, hint in rows:
            label(frm, text).grid(row=r, column=0, sticky="w", pady=px(2))
            vars_[key] = tk.StringVar(value=initial)
            e = tk.Entry(frm, textvariable=vars_[key], width=width, **field)
            e.grid(row=r, column=1, sticky="ew", padx=(px(6), 0), pady=px(2), ipady=px(1))
            if r == (1 if intro else 0):
                e.focus_set()
            r += 1
            if hint:
                label(frm, hint, fg=SHADOW, wraplength=px(400)).grid(row=r, column=1, sticky="w",
                                                                    padx=(px(6), 0))
                r += 1
        result = {}

        def ok():
            result.update({k: v.get().strip() for k, v in vars_.items()})
            dlg.destroy()
        btns = tk.Frame(frm, bg=FACE)
        btns.grid(row=r, column=0, columnspan=2, sticky="e", pady=(px(10), 0))
        h, _ = button(btns, ok_text, ok, width=10, default=True)
        h.pack(side="left")
        h, _ = button(btns, "Cancel", dlg.destroy, width=8)
        h.pack(side="left", padx=(px(6), 0))
        dlg.bind("<Return>", lambda e: ok())
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        try:
            dlg.wait_visibility()
            dlg.grab_set()
        except tk.TclError:
            pass
        root.wait_window(dlg)
        return result or None

    def add_stream_link():
        got = simple_dialog(
            "Add a stream link", ok_text="Add",
            intro=("For sites yt-dlp doesn't support: open the video page in your browser, press F12, "
                   "go to Network, type m3u8 (or mpd) in the filter box and play the video. "
                   "Right-click the request that appears > Copy > Copy URL, and paste it below."),
            rows=[("url", "Stream address:", "", "Ends in .m3u8 or .mpd (anything after a ? is fine)."),
                  ("page", "Page it's on:", "", "Sent along as the Referer; many sites need it."),
                  ("title", "Title:", "", "Optional. Without it, the title comes from the page address.")])
        if not got:
            return
        if not re.match(r"https?://\S+$", got["url"]):
            messagebox.showinfo("Add a stream link", "The stream address has to start with http:// or https://.")
            return
        line = got["url"]
        if re.match(r"https?://\S+$", got["page"]):
            line += " | referer=" + got["page"]
        if got["title"]:
            line += " | title=" + got["title"].replace("|", "/")
        current = links.get("1.0", "end").strip()
        links.insert("end", ("\n" if current else "") + line)
        write_log("Stream link added to Step 1.", "ok")

    def teach_site_dialog():
        """Wizard: give it a video link, it works out the rest and builds a tested plugin."""
        dlg = tk.Toplevel(root)
        dlg.title("Teach Video Archiver a new site")
        dlg.configure(bg=FACE)
        dlg.transient(root)
        dlg.minsize(px(560), px(420))
        frm = tk.Frame(dlg, bg=FACE, padx=px(12), pady=px(10))
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)
        frm.rowconfigure(8, weight=1)
        label(frm, "For a site yt-dlp can't download from. Give me a link to one video on it: I'll read the "
                   "page, work out where the video comes from, make a plugin for the site and test it. "
                   "No programming needed.", wraplength=px(540)).grid(row=0, column=0, columnspan=3,
                                                                       sticky="w", pady=(0, px(8)))
        first = links.get("1.0", "end").strip().splitlines()
        link_var = tk.StringVar(value=parse_link_line(first[0])[0] if first else "")
        link2_var, stream_var, pattern_var = tk.StringVar(), tk.StringVar(), tk.StringVar()
        rows = [(link_var, "Video link:", None),
                (link2_var, "Another video:", "Optional, but recommended: a second video from the same site, "
                                              "to check it works for more than one."),
                (stream_var, "Stream address:", "Leave empty. Only needed if I ask for it below.")]
        r = 1
        for var, text, hint in rows:
            label(frm, text).grid(row=r, column=0, sticky="w", pady=px(2))
            tk.Entry(frm, textvariable=var, **field).grid(row=r, column=1, columnspan=2, sticky="ew",
                                                          padx=(px(6), 0), pady=px(2), ipady=px(1))
            r += 1
            if hint:
                label(frm, hint, fg=SHADOW, wraplength=px(460)).grid(row=r, column=1, columnspan=2, sticky="w",
                                                                    padx=(px(6), 0))
                r += 1
        h, analyze_b = button(frm, "Analyze", lambda: do_analyze(), width=12, default=True)
        h.grid(row=r, column=2, sticky="e", pady=(px(6), px(6)))
        r += 1
        out = tk.Text(frm, height=12, width=90, bg=LOG_BG, fg=LOG_FG, font=FMONO, relief="sunken",
                      bd=2, wrap="word", state="disabled")
        out.grid(row=r, column=0, columnspan=3, sticky="nsew")
        frm.rowconfigure(r, weight=1)
        r += 1
        label(frm, "Links it handles:").grid(row=r, column=0, sticky="w", pady=(px(8), 0))
        pat_entry = tk.Entry(frm, textvariable=pattern_var, state="disabled", **field)
        pat_entry.grid(row=r, column=1, columnspan=2, sticky="ew", padx=(px(6), 0), pady=(px(8), 0), ipady=px(1))
        r += 1
        label(frm, "* stands for the part that's different for every video. Change it if other videos' links "
                   "look different, e.g. site.de/*/video/*.", fg=SHADOW, wraplength=px(460)).grid(
            row=r, column=1, columnspan=2, sticky="w", padx=(px(6), 0))
        r += 1
        btns = tk.Frame(frm, bg=FACE)
        btns.grid(row=r, column=0, columnspan=3, sticky="ew", pady=(px(10), 0))
        state_ = {"analysis": None, "log": ""}

        def show(text, append=False):
            out.configure(state="normal")
            if not append:
                out.delete("1.0", "end")
            out.insert("end", text)
            out.see("end")
            out.configure(state="disabled")

        def set_buttons(busy=False):
            a = state_["analysis"]
            analyze_b.configure(state="disabled" if busy else "normal")
            create_b.configure(state="normal" if a and a["ok"] and not busy else "disabled")
            use_b.configure(state="normal" if a and a.get("stream_line") and not busy else "disabled")
            pat_entry.configure(state="normal" if a and a["ok"] else "disabled")

        def in_background(task, done, needs_tools=False):
            if needs_tools and not TOOL_LOCK.acquire(blocking=False):
                show("\nBusy: wait until the current download or update has finished, then try again.", True)
                return
            set_buttons(busy=True)
            result = {}

            def work():
                try:
                    result["value"] = task()
                except Exception as exc:
                    result["error"] = str(exc) or type(exc).__name__
                finally:
                    if needs_tools:
                        TOOL_LOCK.release()
            threading.Thread(target=work, daemon=True).start()

            def wait():
                if not dlg.winfo_exists():
                    return
                if not result:
                    dlg.after(150, wait)
                    return
                set_buttons()
                if "error" in result:
                    show("\nSomething went wrong: " + result["error"], True)
                else:
                    done(result["value"])
            wait()

        def do_analyze():
            link = link_var.get().strip()
            if not link:
                show("Paste a link to one video on the site first.")
                return
            state_["analysis"] = None
            set_buttons()
            show("Reading the page...\n")

            def done(a):
                state_["analysis"] = a
                show("\n".join(a["lines"]) + "\n")
                if a["pattern"]:
                    pattern_var.set(a["pattern"])
                if a["ok"]:
                    show("\nLooks good. Check the pattern below, then press \"Create and test\".", True)
                set_buttons()
            in_background(lambda: analyze_site(link, link2_var.get(), stream_var.get()), done)

        def do_create():
            a, link = state_["analysis"], link_var.get().strip()
            try:
                wildcard_to_regex(pattern_var.get(), link)
                if link2_var.get().strip() and not re.match(wildcard_to_regex(pattern_var.get(), link),
                                                            link2_var.get().strip(), re.I):
                    raise ValueError("The second video's link doesn't fit the pattern. Change the pattern "
                                     "so both fit (use * for every part that differs).")
            except ValueError as exc:
                show(f"\n{exc}", True)
                return
            try:
                path = write_site_plugin(pattern_var.get(), link, a["site"])
            except (OSError, SyntaxError, ValueError) as exc:
                show(f"\nCouldn't save the plugin: {exc}", True)
                return
            write_log(f"Site plugin saved: {path}", "ok")
            show(f"\nPlugin saved:\n{path}\n\nTesting it with yt-dlp (up to a minute per video)...\n", True)
            tests = [x for x in (link, link2_var.get().strip()) if x]

            def task():
                return [check_link(x, cookie_args()) for x in tests]

            def done(results):
                good = 0
                for x, (report, raw) in zip(tests, results):
                    works = "Result:     WORKS" in report
                    good += works
                    state_["log"] += raw + "\n\n"
                    show(f"\n{x}\n" + "\n".join("  " + l for l in report.splitlines()[:6]) + "\n", True)
                if good == len(tests):
                    show("\nDone! Links from this site now work in Video Archiver like any other link.", True)
                    write_log("New site works: " + pattern_var.get(), "ok")
                else:
                    show("\nThe plugin didn't work in yt-dlp's real test. If the Plugins line above doesn't "
                         "mention it, yt-dlp didn't load it: try Tools > Repair, then test again with "
                         "Tools > Check a site or link. Otherwise try again with the stream address "
                         "(see the steps you get when you leave it empty and the page has none), or "
                         "use \"Use as stream link\" for single videos.", True)
            in_background(task, done, needs_tools=True)

        def use_stream_line():
            line = (state_["analysis"] or {}).get("stream_line")
            if line:
                current = links.get("1.0", "end").strip()
                links.insert("end", ("\n" if current else "") + line)
                write_log("Stream link added to Step 1.", "ok")
                show("\nAdded to Step 1. It downloads this one video.", True)

        def copy_log():
            root.clipboard_clear()
            root.clipboard_append(state_["log"] or out.get("1.0", "end"))

        h, create_b = button(btns, "Create and test", do_create, width=15)
        h.pack(side="left")
        h, use_b = button(btns, "Use as stream link", use_stream_line, width=17)
        h.pack(side="left", padx=(px(6), 0))
        h, _ = button(btns, "Copy log", copy_log, width=10)
        h.pack(side="left", padx=(px(6), 0))
        h, _ = button(btns, "Close", dlg.destroy, width=8)
        h.pack(side="right")
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        set_buttons()
        show("1. Paste a link to one video on the site (and, if you like, a second one).\n"
             "2. Press Analyze.\n"
             "3. If it looks good, press Create and test.")

    def open_plugins():
        d = plugins_dir()
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showinfo("Plugins", f"Couldn't create {d} ({exc.strerror or exc}).")
            return
        write_log(f"yt-dlp plugin folder: {d}  (each plugin: <name>\\yt_dlp_plugins\\extractor\\<site>.py)")
        open_path(d)

    def site_check_dialog():
        dlg = tk.Toplevel(root)
        dlg.title("Check a site or link")
        dlg.configure(bg=FACE)
        dlg.transient(root)
        dlg.minsize(px(520), px(360))
        frm = tk.Frame(dlg, bg=FACE, padx=px(10), pady=px(8))
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)
        frm.rowconfigure(3, weight=1)
        first = links.get("1.0", "end").strip().splitlines()
        link_var = tk.StringVar(value=first[0].strip() if first else "")
        site_var = tk.StringVar()
        label(frm, "Link:").grid(row=0, column=0, sticky="w")
        tk.Entry(frm, textvariable=link_var, **field).grid(row=0, column=1, sticky="ew", padx=px(6), ipady=px(1))
        label(frm, "Site name:").grid(row=1, column=0, sticky="w", pady=(px(4), 0))
        tk.Entry(frm, textvariable=site_var, **field).grid(row=1, column=1, sticky="ew", padx=px(6),
                                                           pady=(px(4), 0), ipady=px(1))
        label(frm, "Check link tests the actual page (is the site supported, and does it work right "
                   "now?). Search looks the name up in yt-dlp's list of supported sites; a listed "
                   "site can still be broken.", fg=SHADOW, wraplength=px(560)).grid(
            row=2, column=0, columnspan=3, sticky="w", pady=(px(4), px(4)))
        out = tk.Text(frm, height=16, width=90, bg=LOG_BG, fg=LOG_FG, font=FMONO, relief="sunken",
                      bd=2, wrap="word", state="disabled")
        out.grid(row=3, column=0, columnspan=3, sticky="nsew")
        full_log = {"text": ""}

        def show(text):
            out.configure(state="normal")
            out.delete("1.0", "end")
            out.insert("end", text)
            out.configure(state="disabled")

        def busy(on):
            for b in (check_b, search_b):
                b.configure(state="disabled" if on else "normal")

        def background(task, done):
            if not TOOL_LOCK.acquire(blocking=False):
                show("Busy: wait until the current download or update has finished.")
                return
            busy(True)
            result = {}

            def work():
                try:
                    result["value"] = task()
                except Exception as exc:
                    result["error"] = str(exc)
                finally:
                    TOOL_LOCK.release()
            threading.Thread(target=work, daemon=True).start()

            def wait():
                if not dlg.winfo_exists():
                    return
                if "value" not in result and "error" not in result:
                    dlg.after(150, wait)
                    return
                busy(False)
                if "error" in result:
                    show("Something went wrong: " + result["error"])
                else:
                    done(result["value"])
            wait()

        def do_check():
            line = link_var.get().strip()
            if not line:
                show("Type or paste a link first.")
                return
            show("Testing the link with yt-dlp (this can take up to a minute)...")

            def done(value):
                report, raw = value
                full_log["text"] = raw
                show(report + "\n\n(Copy full log gives the complete yt-dlp -v output, e.g. for a bug report.)")
            background(lambda: check_link(line, cookie_args()), done)

        def do_search():
            q = site_var.get().strip() or link_var.get().strip()
            if not q:
                show("Type a site name first, e.g. vimeo or meineseite.de")
                return
            show("Reading yt-dlp's list of supported sites...")

            def done(value):
                hits, total = value
                if not hits:
                    show(f"\"{q}\" is not among the {total} sites yt-dlp knows.\n\n"
                         "It may still work through the generic page scanner: use Check link with "
                         "a video page. If it doesn't, Tools > yt-dlp channel > Nightly might know it, "
                         "or use Tools > Add a stream link.")
                else:
                    more = f"\n... and {len(hits) - 60} more" if len(hits) > 60 else ""
                    show(f"{len(hits)} match(es) among {total} supported sites:\n\n"
                         + "\n".join(hits[:60]) + more
                         + "\n\n\"(CURRENTLY BROKEN)\" means yt-dlp knows the site can't work right now.")
            background(lambda: search_extractors(q), done)

        h, check_b = button(frm, "Check link", do_check, width=11, default=True)
        h.grid(row=0, column=2, sticky="e")
        h, search_b = button(frm, "Search", do_search, width=11)
        h.grid(row=1, column=2, sticky="e", pady=(px(4), 0))
        btns = tk.Frame(frm, bg=FACE)
        btns.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(px(8), 0))

        def copy_log():
            root.clipboard_clear()
            root.clipboard_append(full_log["text"] or out.get("1.0", "end"))
        for text, cmd, w in (("Copy full log", copy_log, 12), ("Plugin folder", open_plugins, 12),
                             ("Close", dlg.destroy, 8)):
            h, _ = button(btns, text, cmd, width=w)
            h.pack(side="right" if text == "Close" else "left", padx=(0, px(6)))
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        show("Check link: is this page supported, and does it work right now?\n"
             "Search: is a site on yt-dlp's list?")

    def pot_dialog():
        """Install, test or remove the YouTube PO-token helper."""
        dlg = tk.Toplevel(root)
        dlg.title("Fix YouTube \"403 Forbidden\"")
        dlg.configure(bg=FACE)
        dlg.transient(root)
        dlg.minsize(px(540), px(360))
        frm = tk.Frame(dlg, bg=FACE, padx=px(12), pady=px(10))
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(0, weight=1)
        frm.rowconfigure(2, weight=1)
        label(frm, "YouTube is starting to ask for \"PO tokens\". Without them some downloads fail with "
                   "\"HTTP Error 403: Forbidden\". This installs the token helper yt-dlp recommends "
                   f"(bgutil, version {POT_VERSION}), run with the Deno the app already has. Every file is "
                   "checked against a known checksum. It downloads about 100 MB and takes a few minutes. "
                   "Only needed if you get 403 errors on YouTube.", wraplength=px(520)).grid(
            row=0, column=0, sticky="w")
        status_lbl = label(frm, " ", font=FB)
        status_lbl.grid(row=1, column=0, sticky="w", pady=(px(8), px(4)))
        out = tk.Text(frm, height=12, width=80, bg=LOG_BG, fg=LOG_FG, font=FMONO, relief="sunken",
                      bd=2, wrap="word", state="disabled")
        out.grid(row=2, column=0, sticky="nsew")
        btns = tk.Frame(frm, bg=FACE)
        btns.grid(row=3, column=0, sticky="ew", pady=(px(8), 0))
        lines, busy = [], {"on": False}

        def say(text):
            lines.append(text)

        def flush():
            if not dlg.winfo_exists():
                return
            if lines:
                out.configure(state="normal")
                while lines:
                    out.insert("end", lines.pop(0) + "\n")
                out.see("end")
                out.configure(state="disabled")
            dlg.after(150, flush)

        def refresh():
            on = pot_installed()
            status_lbl.configure(text=f"Installed (version {POT_VERSION})" if on else "Not installed",
                                 fg=BLACK if on else SHADOW)
            for b, ok in ((install_b, True), (test_b, on), (remove_b, on)):
                b.configure(state="normal" if ok and not busy["on"] else "disabled")
            install_b.configure(text="Reinstall" if on else "Install")

        def background(task):
            if not TOOL_LOCK.acquire(blocking=False):
                say("Busy: wait until the current download or update has finished.")
                return
            busy["on"] = True
            refresh()

            def work():
                try:
                    task()
                except Exception as exc:
                    say(f"Something went wrong: {exc}")
                finally:
                    TOOL_LOCK.release()
                    busy["on"] = False
                    try:
                        dlg.after(0, refresh)
                    except (tk.TclError, RuntimeError):
                        pass
            threading.Thread(target=work, daemon=True).start()

        def test():
            say("Testing with a YouTube video (up to a minute)...")
            r = run_tool(YTDLP, [*COMMON_ARGS, "-v", "--simulate", "--no-playlist", *cookie_args(), *pot_args(),
                                 "--", "https://www.youtube.com/watch?v=jNQXAC9IVRw"], 180)
            text = (r.stdout or "") + (r.stderr or "")
            line = pot_status_line(text)
            deno = [p.strip() for p in line.split(",") if "bgutil:script-deno" in p]
            if deno and "unavailable" not in deno[0].lower():
                say(f"Working: yt-dlp uses the token helper ({deno[0]}).")
                write_log("YouTube PO-token helper is working.", "ok")
            elif line:
                say(f"yt-dlp sees the helper, but it isn't working: {line}")
                say("Try Reinstall. If it still fails, Copy the log from Tools > Check a site or link.")
            else:
                say("yt-dlp didn't load the helper. Try Reinstall.")
            errs = [l for l in text.splitlines() if l.startswith("ERROR:")]
            if errs:
                say("yt-dlp also said: " + clean_error(errs[-1]))

        def do_install():
            def task():
                if install_pot(say):
                    say("Installed.")
                    write_log(f"YouTube PO-token helper {POT_VERSION} installed.", "ok")
                    test()
            background(task)

        def do_remove():
            problems = remove_pot()
            say("Removed." if not problems else "Removed, except: " + ", ".join(problems))
            write_log("YouTube PO-token helper removed.")
            refresh()

        h, install_b = button(btns, "Install", do_install, width=10, default=True)
        h.pack(side="left")
        h, test_b = button(btns, "Test", lambda: background(test), width=8)
        h.pack(side="left", padx=(px(6), 0))
        h, remove_b = button(btns, "Remove", do_remove, width=8)
        h.pack(side="left", padx=(px(6), 0))
        h, _ = button(btns, "Close", dlg.destroy, width=8)
        h.pack(side="right")
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        refresh()
        flush()

    def set_channel():
        save_settings(ytdlp_channel=channel_var.get())
        write_log(f"yt-dlp channel: {channel_var.get()}. Switching now...")
        tool_task("yt-dlp")

    channel_var = tk.StringVar(value=ytdlp_channel())
    m_channel = tk.Menu(menubar, tearoff=0)
    m_channel.add_radiobutton(label="Stable (recommended)", value="stable", variable=channel_var,
                              command=set_channel)
    m_channel.add_radiobutton(label="Nightly (newest fixes and sites)", value="nightly",
                              variable=channel_var, command=set_channel)

    m_tools = tk.Menu(menubar, tearoff=0)
    m_tools.add_command(label="Update yt-dlp now", command=lambda: tool_task("yt-dlp"))
    m_tools.add_cascade(label="yt-dlp channel", menu=m_channel)
    m_tools.add_command(label="Update Deno now", command=lambda: tool_task("deno"))
    m_tools.add_separator()
    m_tools.add_command(label="Upload downloaded files to archive.org...", command=folder_upload_dialog)
    m_tools.add_separator()
    m_tools.add_command(label="Check a site or link...", command=site_check_dialog)
    m_tools.add_command(label="Fix YouTube \"403 Forbidden\" (PO tokens)...", command=pot_dialog)
    m_tools.add_command(label="Add a stream link (.m3u8 / .mpd)...", command=add_stream_link)
    m_tools.add_separator()
    m_tools.add_command(label="Open yt-dlp plugin folder", command=open_plugins)
    m_tools.add_command(label="Teach Video Archiver a new site...", command=teach_site_dialog)
    m_tools.add_separator()
    m_tools.add_command(label="Repair built-in tools", command=lambda: tool_task("repair"))
    m_help = tk.Menu(menubar, tearoff=0)
    m_help.add_command(label="About Video Archiver", underline=0, command=lambda: messagebox.showinfo(
        "About Video Archiver",
        "Video Archiver\n\nSaves videos from YouTube and the 1000+ other sites yt-dlp supports "
        "as MP3 or MP4, and can upload them to your archive.org account.\n"
        "Powered by yt-dlp, FFmpeg and Deno.\n\n"
        "Licenses: FFmpeg is GPL, the yt-dlp program includes GPLv3+ parts, Deno is MIT. "
        "If you give this app to someone else, those licenses apply to them too. "
        "License files are in:\n" + str(TOOLS) + "\n\nFor personal use."))
    def uninstall_from_menu():
        if state["job"] is not None:
            messagebox.showinfo(APP_NAME, "Finish or stop the current download first.")
            return
        if not TOOL_LOCK.acquire(blocking=False):
            messagebox.showinfo(APP_NAME, "Wait for the yt-dlp update to finish first.")
            return
        try:
            if not messagebox.askyesno(f"Uninstall {APP_NAME}", UNINSTALL_QUESTION, icon="warning"):
                return
            report = uninstall_report(uninstall_files())
        finally:
            TOOL_LOCK.release()
        messagebox.showinfo(APP_NAME, report)
        root.destroy()

    m_help.add_separator()
    m_help.add_command(label=f"Uninstall {APP_NAME}...", command=uninstall_from_menu)
    menubar.add_cascade(label="File", underline=0, menu=m_file)
    menubar.add_cascade(label="Options", underline=0, menu=m_opts)
    menubar.add_cascade(label="Tools", underline=0, menu=m_tools)
    menubar.add_cascade(label="Help", underline=0, menu=m_help)
    root.config(menu=menubar)

    def blocked_review_dialog(items, box, answered):
        """End of a run: decide about all videos whose archive.org item is blocked."""
        dlg = tk.Toplevel(root)
        dlg.title("Videos with a blocked archive.org item")
        dlg.configure(bg=FACE)
        dlg.transient(root)
        frm = tk.Frame(dlg, bg=FACE, padx=px(12), pady=px(10))
        frm.pack(fill="both", expand=True)
        label(frm, f"The run is done. These {len(items)} video(s) weren't uploaded because archive.org has "
                   "hidden or removed their items. The videos themselves are saved on your PC.\n\n"
                   "Only tick a video if YOU removed its item or it was a broken upload of yours. If archive.org "
                   "removed it (for example after a rights complaint), uploading it again under a new name can "
                   "get your account closed. If you're not sure, leave it unticked.",
              wraplength=px(560)).pack(anchor="w")
        lst = tk.Frame(frm, bg=WHITE, relief="sunken", bd=2, padx=px(6), pady=px(4))
        lst.pack(fill="both", expand=True, pady=(px(8), px(6)))
        picks = []
        for it in items[:40]:
            var = tk.BooleanVar(value=False)
            picks.append((it["ident"], var))
            tk.Checkbutton(lst, text=f"{it['name']}   (archive.org/details/{it['ident']})", variable=var,
                           bg=WHITE, activebackground=WHITE, anchor="w", font=F, highlightthickness=0).pack(
                fill="x", anchor="w")
        if len(items) > 40:
            tk.Label(lst, text=f"... and {len(items) - 40} more (skipped).", bg=WHITE, fg=BLACK, font=F,
                     anchor="w").pack(fill="x", anchor="w")
        never = tk.BooleanVar(value=True)
        tk.Checkbutton(frm, text="Don't ask again about the videos I leave unticked", variable=never,
                       **radio).pack(anchor="w")
        btns = tk.Frame(frm, bg=FACE)
        btns.pack(fill="x", pady=(px(8), 0))

        def finish(upload):
            box["answer"] = {"upload": [i for i, v in picks if v.get()] if upload else [],
                             "never": never.get()}
            answered.set()
            dlg.destroy()
        h, _ = button(btns, "Skip all", lambda: finish(False), width=10, default=True)
        h.pack(side="right")
        h, _ = button(btns, "Upload ticked under new names", lambda: finish(True), width=26)
        h.pack(side="right", padx=(0, px(6)))
        dlg.protocol("WM_DELETE_WINDOW", lambda: finish(False))
        dlg.bind("<Escape>", lambda e: finish(False))
        try:
            dlg.wait_visibility()
            dlg.grab_set()
        except tk.TclError:
            pass

    def poll():
        try:
            while True:
                ev = events.get_nowait()
                kind = ev[0]
                if kind == "progress":
                    if ev[1] is not None:
                        bar.set(ev[1])
                    detail.configure(text=ev[2] or " ")
                elif kind == "now":
                    now.configure(text=ev[1][:90])
                elif kind == "log":
                    write_log(ev[1], ev[2])
                elif kind == "version":
                    status_right.configure(text="yt-dlp " + version_label(ev[1]))
                elif kind == "blocked":
                    _, items, box, answered = ev
                    blocked_review_dialog(items, box, answered)
                elif kind == "confirm":
                    _, question, box, answered, title = ev
                    box["answer"] = messagebox.askyesno(title, question)
                    answered.set()
                elif kind == "ready":
                    state["ready"] = ev[1]
                    now.configure(text="Ready. Paste a link and press Download." if ev[1]
                                  else "Not ready. See the activity log.")
                    status_left.configure(text="Ready." if ev[1] else "Not ready.")
                    set_busy(False)
                elif kind == "done":
                    job = ev[1]
                    for lbl, msg in job.failed:
                        write_log(f"Failed: {lbl}: {msg}", "err")
                    summary = job.summary()
                    write_log(summary, "err" if job.failed else "ok")
                    now.configure(text=summary)
                    status_left.configure(text=summary)
                    detail.configure(text=" ")
                    bar.set(100 if (job.saved or job.uploaded) and not job.failed and not job.cancelled else 0)
                    state["job"] = None
                    set_busy(False)
                    if state["closing"]:
                        root.destroy()
                        return
                    beep()
        except queue.Empty:
            pass
        root.after(100, poll)

    def set_cookies(choice):
        if choice == "file":
            path = filedialog.askopenfilename(
                title="Choose your exported cookies.txt",
                filetypes=[("cookies.txt", "*.txt"), ("All files", "*.*")])
            if not path:
                cookies_mode.set(cookie_mode())
                return
            try:
                count = use_cookies_file(path)
            except (ValueError, OSError) as exc:
                messagebox.showerror("Cookies", str(exc))
                cookies_mode.set(cookie_mode())
                return
            write_log(f"Using {count} cookies from {Path(path).name}. Sites see you as the account they came "
                      "from. The app keeps its own copy; choose a fresh export if you get signed out. "
                      "Treat cookies.txt like a password and only export it with a tool you trust.", "warn")
        elif choice == "firefox":
            write_log("Firefox cookies on: sites see you as your signed-in Firefox self. "
                      "Only use this on your own computer.", "warn")
        else:
            write_log("Cookies off.")
        save_settings(cookies_mode=choice, firefox_cookies=(choice == "firefox"))

    def toggle_trial():
        save_settings(ia_trial=trial.get())
        refresh_ia_status()
        write_log("archive.org trial mode " + ("ON: uploads go to the test collection and are deleted after "
                                               "about 30 days." if trial.get() else "off."),
                  "warn" if trial.get() else None)

    def tool_task(kind, automatic=False):
        """Run an update or repair in the background, never while downloading."""
        def work():
            if not TOOL_LOCK.acquire(blocking=False):
                if not automatic:
                    events.put(("log", "Finish or stop the current download first.", "warn"))
                return
            try:
                if kind == "yt-dlp":
                    events.put(("version", version_label(ytdlp_version() or "?") + " (checking...)"))
                    msg, _ = update_ytdlp()
                    if msg or not automatic:
                        events.put(("log", msg or "yt-dlp is already the newest version.", None))
                elif kind == "deno":
                    events.put(("log", update_deno(), None))
                elif kind == "repair":
                    problem = unpack_tools(force=True)
                    events.put(("log", problem or "Built-in tools restored from the app.",
                                "err" if problem else "ok"))
                    events.put(("ready", problem is None and ytdlp_version() is not None))
            finally:
                events.put(("version", ytdlp_version() or "not working"))
                TOOL_LOCK.release()
        threading.Thread(target=work, daemon=True).start()

    def startup():
        """Get the tools ready; the app is usable before any update check runs."""
        if clean_stale_bundles():
            events.put(("log", "Cleaned up leftovers from an earlier crash.", None))
        with TOOL_LOCK:
            if tools_need_unpack():
                events.put(("log", "Unpacking the built-in tools...", None))
            if (problem := unpack_tools()):
                events.put(("log", problem, "err"))
                events.put(("ready", False))
                return
            version = ytdlp_version()
            if not version:  # e.g. a broken self-update: restore the copy inside the exe
                events.put(("log", "yt-dlp didn't start. Restoring the built-in copy...", "warn"))
                problem = unpack_tools(force=True)
                version = None if problem else ytdlp_version()
                if not version:
                    events.put(("log", problem or "yt-dlp still doesn't start. Your antivirus may "
                                "be blocking it; allow it, then use Tools > Repair.", "err"))
                    events.put(("ready", False))
                    return
            dv = deno_version()
        events.put(("version", version))
        events.put(("log", f"yt-dlp {version} ready.", None))
        if dv is None:
            events.put(("log", "Deno doesn't start, so YouTube may hide some formats. "
                        "Use Tools > Repair built-in tools.", "warn"))
        elif dv < MIN_DENO:
            events.put(("log", f"Deno {'.'.join(map(str, dv))} is older than yt-dlp needs "
                        "(2.3 or newer). Use Tools > Update Deno now.", "warn"))
        events.put(("ready", True))
        if update_due():
            tool_task("yt-dlp", automatic=True)

    if migration_note:
        write_log(migration_note, "ok" if migration_note.startswith(("Moved", "Copied")) else "warn")
    write_log("Downloads go to: " + out_dir.get())
    threading.Thread(target=startup, daemon=True).start()
    root.bind("<Control-Return>", lambda e: start())
    root.update_idletasks()
    room = root.winfo_screenheight() - px(80)      # leave space for the taskbar and title bar
    if root.winfo_reqheight() > room:                # small screens: shorter activity box
        log.configure(height=3)
        root.update_idletasks()
    root.minsize(max(root.winfo_reqwidth(), px(560)), min(root.winfo_reqheight(), room))
    links.focus_set()
    poll()
    root.mainloop()


if __name__ == "__main__":
    if "--uninstall" in sys.argv[1:]:
        run_uninstall()
    else:
        run_gui()
