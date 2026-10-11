import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
import os, tempfile
from pathlib import Path
os.environ["YTD_DATA_DIR"] = tempfile.mkdtemp()
import video_archiver as A
items = {"me-aaaaaaaaaaa": {"is_dark": True}, "me-ccccccccccc": {"is_dark": True}}
A.ia_item = lambda i: items.get(i, {})
order = []
def fake_upload(keys, ident, path, meta, mt, *a, **k):
    order.append(ident); items[ident] = {"metadata": {"uploader": ""}, "files": [{"name": Path(path).name}]}
A.ia_upload = fake_upload
class UI:
    def __init__(s, pick=(), never=False): s.pick, s.never, s.logs, s.reviews = list(pick), never, [], []
    def log(s, t, tag=None): s.logs.append(t)
    def now(s, t): pass
    def progress(s, p, d): pass
    def confirm(s, q, title=None): raise AssertionError("must not ask in the middle of a run")
    def review_blocked(s, its):
        s.reviews.append([i["name"] for i in its]); order.append("REVIEW")
        return {"upload": s.pick, "never": s.never}
d = Path(tempfile.mkdtemp())
vids = ["aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc", "ddddddddddd"]
files = []
for v in vids:
    p = d / f"V {v[0]} [{v}].mp4"; p.write_bytes(b"1")
    files.append({"path": str(p), "vid": v, "meta": {"title": "V", "id": v, "extractor_key": "Youtube"}, "entry": {}, "lookup": False})
arch = {"keys": ("a", "s"), "prefix": "me", "username": ""}

# 1) run carries on; one question at the end; tick one video
ui = UI(pick=["me-ccccccccccc"])
job = A.Job([], "mp4", 0, d, False, ui, archive=arch, files=files); job.staging = d / ".s" / "1"; job.run()
assert order == ["me-bbbbbbbbbbb", "me-ddddddddddd", "REVIEW", "me-ccccccccccc-2"], order
assert ui.reviews == [["V a [aaaaaaaaaaa].mp4", "V c [ccccccccccc].mp4"]]
assert any("You'll be asked about it at the end" in l for l in ui.logs)
assert job.uploaded == 3 and len(job.failed) == 1 and "aaaaaaaaaaa" in job.failed[0][1]
assert A.renamed_items() == {"me-ccccccccccc": "me-ccccccccccc-2"} and A.blocked_skips() == set()
print("run carried on, asked once at the end:", order, "|", job.summary())

# 2) next run: skip all + don't ask again -> remembered; third run: silent skip, no window
order.clear(); ui = UI(never=True)
job = A.Job([], "mp4", 0, d, False, ui, archive=arch, files=files[:1]); job.staging = d / ".s" / "2"; job.run()
assert order == ["REVIEW"] and A.blocked_skips() == {"me-aaaaaaaaaaa"}
order.clear(); ui = UI()
job = A.Job([], "mp4", 0, d, False, ui, archive=arch, files=files); job.staging = d / ".s" / "3"; job.run()
assert ui.reviews == [] and job.skipped == 1 and "REVIEW" not in order and order == []
assert any("you chose to skip it" in l for l in ui.logs)
print("'don't ask again' remembered: next run skips it silently;", job.summary())

# 3) Stop during the run: no review window; older UIs without the window skip safely
A.save_settings(ia_blocked_skip=[]); order.clear(); ui = UI()
job = A.Job([], "mp4", 0, d, False, ui, archive=arch, files=files[:1]); job.staging = d / ".s" / "4"
job.cancel.set(); job.run(); assert ui.reviews == [] and job.cancelled
class OldUI(UI):
    review_blocked = property(lambda s: (_ for _ in ()).throw(AttributeError()))
job = A.Job([], "mp4", 0, d, False, OldUI(), archive=arch, files=files[:1]); job.staging = d / ".s" / "5"; job.run()
assert len(job.failed) == 1 and "blocked" in job.failed[0][1]
print("Stop: no window; older UIs: blocked videos skipped")
print("ALL BLOCKED-ITEM TESTS PASSED")
