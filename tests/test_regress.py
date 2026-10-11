import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
import json, os, tempfile
from pathlib import Path
os.environ["YTD_DATA_DIR"] = tempfile.mkdtemp()
import video_archiver as A
class UI:
    def __init__(s): s.logs = []
    def log(s, t, tag=None): s.logs.append(t)
    def now(s, t): pass
    def progress(s, p, d): pass
    def confirm(s, q): return True
out = Path(tempfile.mkdtemp()); sent = []
A.ia_item = lambda i: {}
A.ia_upload = lambda keys, ident, path, meta, mt, *a, **k: sent.append((ident, meta["title"], k["topics"]))
job = A.Job(["https://www.youtube.com/watch?v=dQw4w9WgXcQ"], "mp4", 1080, out, False, UI(),
            archive={"keys": ("a", "s"), "prefix": "me", "username": ""})
def fake(args, cb):
    if "-J" in args:
        cb(json.dumps({"id": "dQw4w9WgXcQ", "title": "T", "extractor_key": "Youtube", "webpage_url": args[-1]})); return 0, []
    f = out / "T [dQw4w9WgXcQ].mp4"; f.write_bytes(b"1")
    for k, v in {"id": "dQw4w9WgXcQ", "title": "T", "extractor_key": "Youtube", "tags": ["x y"]}.items(): cb(f"@@M {k} {json.dumps(v)}")
    cb(f"@@F {f}"); return 0, []
job._run = fake; job.run()
assert sent == [("me-dQw4w9WgXcQ", "T", ["x y", "YouTube"])], sent
assert job.summary() == "Finished: 1 saved, 1 uploaded", job.summary()
# second run: file exists, already uploaded -> no re-upload
A.ia_item = lambda i: {"files": [{"name": "T [dQw4w9WgXcQ].mp4"}]}
job2 = A.Job(["https://www.youtube.com/watch?v=dQw4w9WgXcQ"], "mp4", 1080, out, False, UI(), archive={"keys": ("a", "s"), "prefix": "me", "username": ""})
job2._run = fake; job2.run(); assert len(sent) == 1 and job2.summary() == "Finished: 0 saved, 1 already in folder, 1 already on archive.org", job2.summary()
print("normal download + upload path unchanged:", job.summary(), "|", job2.summary())
