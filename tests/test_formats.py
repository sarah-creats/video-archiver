"""MP3 / MP4 / Original quality pick the right streams and don't convert what they shouldn't.
Runs the real yt-dlp and FFmpeg on files served from this PC (no internet).
Needs: ffmpeg + ffprobe, and yt-dlp (VA_TEST_YTDLP=path, or "pip install yt-dlp")."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
import os, tempfile, json, shutil, subprocess, threading, http.server, functools
from pathlib import Path
os.environ["YTD_DATA_DIR"] = tempfile.mkdtemp()
import video_archiver as A

ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
if ffmpeg and not (Path(ffmpeg).parent / "ffprobe").exists() and ffprobe:
    ffmpeg = str(Path(ffprobe).parent / "ffmpeg")          # use an ffmpeg that has ffprobe next to it
ytdlp = os.environ.get("VA_TEST_YTDLP") or shutil.which("yt-dlp")
if not ytdlp:
    try:
        import yt_dlp  # noqa: F401
        w = Path(tempfile.mkdtemp()) / "yt-dlp"
        w.write_text(f"#!/bin/sh\nexec {sys.executable} -m yt_dlp \"$@\"\n")
        w.chmod(0o755)
        ytdlp = str(w)
    except ImportError:
        pass
if not (ffmpeg and ffprobe and ytdlp) or sys.platform == "win32":
    print("SKIPPED (needs ffmpeg, ffprobe and yt-dlp)")
    raise SystemExit(0)
A.YTDLP, A.FFMPEG_DIR = Path(ytdlp), Path(ffmpeg).parent
A.Job._acquire_tools = lambda self: A.TOOL_LOCK.acquire()

# four streams like YouTube offers them: video-only H.264 and VP9, audio-only AAC and Opus
srv = Path(tempfile.mkdtemp())
def ff(*args):
    subprocess.run([ffmpeg, "-loglevel", "error", "-y", *args], check=True)
src_v = ["-f", "lavfi", "-i", "testsrc=duration=2:size=640x360:rate=25"]
src_a = ["-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-ac", "2"]
ff(*src_v, "-c:v", "libx264", "-an", str(srv / "v-h264.mp4"))
ff(*src_v, "-c:v", "libvpx-vp9", "-b:v", "200k", "-an", str(srv / "v-vp9.webm"))
ff(*src_a, "-c:a", "aac", "-b:a", "128k", "-vn", str(srv / "a-aac.m4a"))
ff(*src_a, "-c:a", "libopus", "-b:a", "160k", "-vn", str(srv / "a-opus.webm"))

class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass
handler = functools.partial(Quiet, directory=str(srv))
httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
B = f"http://127.0.0.1:{httpd.server_port}"


def fmt(fid, name, **kw):
    return {"format_id": fid, "url": f"{B}/{name}", "protocol": "http", **kw}


ENTRY = {"id": "asmr0000001", "title": "Soft Whispers", "extractor": "generic", "extractor_key": "Generic",
         "webpage_url": f"{B}/watch", "formats": [
             fmt("v-h264", "v-h264.mp4", ext="mp4", vcodec="avc1.64001e", acodec="none", height=360, width=640, tbr=300),
             fmt("v-vp9", "v-vp9.webm", ext="webm", vcodec="vp9", acodec="none", height=360, width=640, tbr=200),
             fmt("a-aac", "a-aac.m4a", ext="m4a", vcodec="none", acodec="mp4a.40.2", abr=128),
             fmt("a-opus", "a-opus.webm", ext="webm", vcodec="none", acodec="opus", abr=160)]}


class UI:
    def __init__(s): s.logs = []
    def log(s, t, tag=None): s.logs.append(t)
    def now(s, t): pass
    def progress(s, p, d): pass
    def confirm(s, q, title=None): return True


def probe(path):
    out = subprocess.run([ffprobe, "-v", "error", "-show_entries", "stream=codec_type,codec_name",
                          "-of", "json", str(path)], capture_output=True, text=True).stdout
    return sorted(f"{s['codec_type']}:{s['codec_name']}" for s in json.loads(out)["streams"]
                  if s["codec_type"] in ("video", "audio"))


def run(mode, quality, original):
    d = Path(tempfile.mkdtemp())
    ui = UI()
    job = A.Job(["x"], mode, quality, d, False, ui, original=original)
    job._resolve = lambda url, opts=None: (None, [dict(ENTRY)])
    job.run()
    files = [p for p in d.iterdir() if p.is_file()]
    assert job.saved == 1 and not job.failed and len(files) == 1, (job.summary(), job.failed, ui.logs[-5:])
    assert job.ok_ids() == {"asmr0000001"}
    return files[0], probe(files[0]), job, d


f, codecs, job, d = run("mp4", 1080, False)
assert f.suffix == ".mp4" and codecs == ["audio:aac", "video:h264"], (f.name, codecs)
print("MP4: H.264 + AAC in .mp4 (plays everywhere)")

f, codecs, job, d = run("mp4", 1080, True)
assert f.name == "Soft Whispers [asmr0000001].mkv" and codecs == ["audio:opus", "video:vp9"], (f.name, codecs)
print("Original quality: VP9 + Opus kept as they are, in .mkv")

# the same video already there as MP4 counts (no second download in another format)
again = A.Job(["x"], "mp4", 1080, d, False, UI(), original=False)
again._resolve = lambda url, opts=None: (None, [dict(ENTRY)])
again.run()
assert again.existing == 1 and again.saved == 0, again.summary()
print("An earlier .mkv counts as already in the folder for MP4 too")

f, codecs, job, d = run("mp3", 0, True)
assert f.suffix == ".opus" and codecs == ["audio:opus"], (f.name, codecs)
print("Original audio: the Opus track, not converted (.opus)")

f, codecs, job, d = run("mp3", 0, False)
assert f.suffix == ".mp3" and codecs == ["audio:mp3"], (f.name, codecs)
print("MP3: converted to MP3 as before")
httpd.shutdown()
print("ALL FORMAT TESTS PASSED")
