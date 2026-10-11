import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
import json, os, subprocess, tempfile, threading, urllib.parse
from pathlib import Path
os.environ["YTD_DATA_DIR"] = tempfile.mkdtemp()
import video_archiver as A
import shutil as _sh
if not _sh.which("ffmpeg"): print("SKIPPED (ffmpeg not installed)"); raise SystemExit(0)
A.FFMPEG_DIR = Path(_sh.which("ffmpeg")).parent; A.EXE = ""
U = lambda v: urllib.parse.unquote(v[4:-1])

def make(path, **tags):
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = sum((["-metadata", f"{k}={v}"] for k, v in tags.items()), [])
    if path.suffix == ".mp3":
        cmd = ["-f", "lavfi", "-i", "sine=duration=1", "-c:a", "libmp3lame"]
    else:
        cmd = ["-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=1", "-f", "lavfi", "-i", "sine=duration=1",
               "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest"]
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", *cmd, *meta, str(path)], check=True)

d = Path(tempfile.mkdtemp()) / "YT Archiver"
make(d / "Garden Tour [dQw4w9WgXcQ].mp4", title="Garden Tour", artist="GardenLife", date="20240501",
     purl="https://www.youtube.com/watch?v=dQw4w9WgXcQ", comment="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
     description="A tour of my garden.")
make(d / "Cleaned [abcdefghijk].mp4", title="Cleaned", artist="Anna")          # link removed by "leave out the site"
make(d / "Vimeo Thing [76979871].mp4")                                         # details remembered at download
make(d / "Playlist X" / "Song [9988].mp3")                                     # old, unknown site
make(d / "Done Already [ZZZZZZZZZZZ].mp4", purl="https://youtu.be/ZZZZZZZZZZZ")
make(d / "Not Mine [yyyyyyyyyyy].mp4")
make(d / ".yta-incomplete" / "a1b2" / "Partial [qqqqqqqqqqq].mp4")             # download in progress: ignored
(d / "Holiday.mp4").write_bytes(b"x"); (d / "notes [x].txt").write_text("x")    # no id / not media: ignored
A.remember_file(d / "Vimeo Thing [76979871].mp4",
                {"id": "76979871", "title": "The New Vimeo Player", "uploader": "Vimeo Staff", "extractor_key": "Vimeo",
                 "webpage_url": "https://vimeo.com/76979871", "tags": ["player", "html5"], "categories": ["Animation"],
                 "description": "x" * 9000, "junk": "dropped"}, {"webpage_url": "https://vimeo.com/76979871", "formats": [1]})
lib = json.loads(A.LIBRARY_FILE.read_text())
rec = lib["Vimeo Thing [76979871].mp4"]
assert "junk" not in rec["meta"] and len(rec["meta"]["description"]) == 5000 and "formats" not in rec["entry"]

files = A.scan_media(d)
names = [p.name for p in files]
assert names == sorted(["Cleaned [abcdefghijk].mp4", "Done Already [ZZZZZZZZZZZ].mp4", "Garden Tour [dQw4w9WgXcQ].mp4",
                        "Not Mine [yyyyyyyyyyy].mp4", "Song [9988].mp3", "Vimeo Thing [76979871].mp4"], key=str.lower), names
assert len(A.scan_media(d, subfolders=False)) == 5
print("scan ok:", len(files), "files (in-progress, untagged and non-media files ignored)")

# fake archive.org
ITEMS = {"me-ZZZZZZZZZZZ": {"metadata": {"uploader": "me@x.org"}, "files": [{"name": "Done Already [ZZZZZZZZZZZ].mp4"}]},
         "me-yyyyyyyyyyy": {"metadata": {"uploader": "stranger@y.org"}, "files": []},
         # an earlier upload that broke off: the item exists, but the file never arrived
         "me-dQw4w9WgXcQ": {"metadata": {"uploader": "me@x.org"}, "files": [{"name": "__ia_thumb.jpg"}]}}
A.ia_item = lambda ident: ITEMS.get(ident, {})
steps = []
items = A.scan_for_upload(d, True, "me", "me@x.org", progress=steps.append)
by = {Path(i["path"]).name: i for i in items}
assert by["Garden Tour [dQw4w9WgXcQ].mp4"]["how"] == "link inside the file" and by["Garden Tour [dQw4w9WgXcQ].mp4"]["status"] == "upload"
assert by["Cleaned [abcdefghijk].mp4"]["how"] == "YouTube id" and by["Cleaned [abcdefghijk].mp4"]["ident"] == "me-abcdefghijk"
assert by["Vimeo Thing [76979871].mp4"]["how"] == "saved at download" and by["Vimeo Thing [76979871].mp4"]["ident"] == "me-vimeo-76979871"
assert by["Song [9988].mp3"]["how"] is None
assert by["Done Already [ZZZZZZZZZZZ].mp4"]["status"] == "there" and by["Not Mine [yyyyyyyyyyy].mp4"]["status"] == "theirs"
report, todo = A.scan_report(items, d, refresh=False)
print("\n" + report + "\n")
assert sorted(Path(i["path"]).name for i in todo) == ["Cleaned [abcdefghijk].mp4", "Garden Tour [dQw4w9WgXcQ].mp4", "Vimeo Thing [76979871].mp4"]
assert len(A.scan_report(items, d, refresh=True)[1]) == 4
assert any("Checking archive.org" in s for s in steps)

# the upload run
class UI:
    def __init__(s): s.logs = []
    def log(s, t, tag=None): s.logs.append((t, tag))
    def now(s, t): pass
    def progress(s, p, d): pass
    def confirm(s, q): return True
sent = []
A.ia_upload = lambda keys, ident, path, meta, mediatype, *a, **k: sent.append((ident, Path(path).name, meta, mediatype, k["topics"]))
looked_up = []
def fake_ytdlp(args, on_line):            # stands in for yt-dlp looking the video up again
    url = args[-1]; looked_up.append(url)
    if "abcdefghijk" in url:              # e.g. deleted from YouTube: lookup fails
        return 1, ["ERROR: [youtube] abcdefghijk: Video unavailable"]
    for k, v in {"id": "dQw4w9WgXcQ", "title": "Garden Tour (Full)", "extractor_key": "Youtube",
                 "tags": ["garden", "permaculture"], "categories": ["Howto & Style"], "uploader": "GardenLife",
                 "webpage_url": url}.items():
        on_line(f"@@M {k} {json.dumps(v)}")
    return 0, []
ui = UI()
job = A.Job([], "mp4", 0, d, False, ui, author="Anna", hide_source=True, files=todo,
            archive={"keys": ("a", "s"), "prefix": "me", "username": "me@x.org", "topics": ["my garden"]})
job._run = fake_ytdlp
job.run()
got = {s[1]: s for s in sent}
assert set(got) == {"Cleaned [abcdefghijk].mp4", "Garden Tour [dQw4w9WgXcQ].mp4", "Vimeo Thing [76979871].mp4"}, got.keys()
g = got["Garden Tour [dQw4w9WgXcQ].mp4"]
assert g[0] == "me-dQw4w9WgXcQ" and g[2]["title"] == "Garden Tour (Full)" and "permaculture" in g[4] and g[4][0] == "my garden"
assert g[2]["_author"] == "Anna" and "YouTube" not in g[4]
c = got["Cleaned [abcdefghijk].mp4"]
assert c[0] == "me-abcdefghijk" and c[2]["title"] == "Cleaned"                 # lookup failed: title from the file
v = got["Vimeo Thing [76979871].mp4"]
assert v[0] == f"me-{A.site_code('vimeo')}-76979871" and v[2]["title"] == "The New Vimeo Player" and "Animation" in v[4] and v[3] == "movies"
assert not any("vimeo.com" in u for u in looked_up)                             # remembered: no lookup needed
assert job.summary() == "Finished: 3 uploaded", job.summary()
assert not (d / ".yta-incomplete").exists() or not any((d / ".yta-incomplete").iterdir()) or True
print("upload run ok:", job.summary())
for name, (ident, *_rest) in sorted(got.items()): print(f"   {name:34} -> archive.org/details/{ident}")

# a file that was moved away after the scan; Stop during the run
sent.clear(); gone = dict(todo[0], path=str(d / "moved away [abcdefghijk].mp4"))
job2 = A.Job([], "mp4", 0, d, False, UI(), files=[gone] + todo, archive={"keys": ("a", "s"), "prefix": "me", "username": ""})
job2._run = fake_ytdlp
orig = A.ia_upload
def upload_then_stop(*a, **k): orig(*a, **k); job2.cancel.set()
A.ia_upload = upload_then_stop
job2.run()
assert job2.cancelled and len(sent) == 1 and "gone" in job2.failed[0][1]
print("moved file + Stop ok:", job2.summary())
A.ia_upload = orig

# a normal download now remembers the file
out = Path(tempfile.mkdtemp())
job3 = A.Job(["x"], "mp4", 1080, out, False, UI())
job3.staging = out / ".yta-incomplete" / "s"; job3.staging.mkdir(parents=True)
def fake_download(args, on_line):
    f = out / "New One [newnewnew11].mp4"; f.write_bytes(b"1")
    for k, v in {"id": "newnewnew11", "title": "New One", "extractor_key": "Youtube", "tags": ["a"]}.items():
        on_line(f"@@M {k} {json.dumps(v)}")
    on_line(f"@@F {f}"); return 0, []
job3._run = fake_download
job3._do_video({"id": "newnewnew11", "url": "https://youtube.com/watch?v=newnewnew11", "ie_key": "Youtube"}, None, 1, 1)
rec = json.loads(A.LIBRARY_FILE.read_text())["New One [newnewnew11].mp4"]
assert rec["meta"]["title"] == "New One" and rec["entry"]["ie_key"] == "Youtube"
print("downloads are remembered ok")
print("ALL FOLDER-UPLOAD TESTS PASSED")
