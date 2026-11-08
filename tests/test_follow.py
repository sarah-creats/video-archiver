"""Followed channels: only new videos, oldest first, into the channel's folder; what
fails is retried and given up on after a few tries."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
import os, tempfile, json
from pathlib import Path
os.environ["YTD_DATA_DIR"] = tempfile.mkdtemp()
import video_archiver as A

# links to follow
cu = A.channel_url
assert cu("https://www.youtube.com/@GentleWhispering") == "https://www.youtube.com/@GentleWhispering/videos"
assert cu("https://youtube.com/@GentleWhispering/") == "https://youtube.com/@GentleWhispering/videos"
assert cu("https://www.youtube.com/@GentleWhispering/featured") == "https://www.youtube.com/@GentleWhispering/videos"
assert cu("https://www.youtube.com/channel/UC123?si=x") == "https://www.youtube.com/channel/UC123/videos"
assert cu("https://www.youtube.com/@x/videos") == "https://www.youtube.com/@x/videos"          # already a tab
assert cu("https://www.youtube.com/@x/streams") == "https://www.youtube.com/@x/streams"
assert cu("https://www.youtube.com/playlist?list=PL1") == "https://www.youtube.com/playlist?list=PL1"
assert cu("https://soundcloud.com/someone") == "https://soundcloud.com/someone"
print("channel links: YouTube channels use their Videos tab, everything else stays")

# list_videos reads yt-dlp's flat list (here: the shape YouTube's Videos tab gives)
def flat(ids, extra=None):
    return {"_type": "playlist", "id": "UC1", "title": "Whisper Room - Videos", "channel": "Whisper Room",
            "entries": [{"_type": "url", "ie_key": "Youtube", "id": i, "title": f"Video {i}",
                         "url": f"https://www.youtube.com/watch?v={i}", **((extra or {}).get(i, {}))} for i in ids]}

class UI:
    def __init__(s): s.logs = []
    def log(s, t, tag=None): s.logs.append(t)
    def now(s, t): pass
    def progress(s, p, d): pass
    def confirm(s, q, title=None): return True

job = A.Job([], "mp4", 0, tempfile.mkdtemp(), True, UI())
calls = []
def fake_run(args, on_line, data=flat(["n3", "n2", "n1"])):
    calls.append(args)
    on_line(json.dumps(data))
    return 0, []
job._run = fake_run
title, entries = job.list_videos("https://www.youtube.com/@WhisperRoom/videos", 100)
assert title == "Whisper Room" and [e["id"] for e in entries] == ["n3", "n2", "n1"], (title, entries)
assert calls[0][:6] == ["-J", "--flat-playlist", "--yes-playlist", "-I", "1:100", "--"]
print("list_videos: channel name and ids from one quick flat listing")

# follow: "only new ones" marks everything that's there now as seen
data = {"channels": []}
ch = A.follow_add(data, "https://www.youtube.com/@WhisperRoom", title, entries, download_existing=False)
assert ch["url"] == "https://www.youtube.com/@WhisperRoom/videos" and set(ch["seen"]) == {"n1", "n2", "n3"}
assert A.follow_find(data, "https://www.youtube.com/@WhisperRoom") is ch
A.save_follows(data)
assert A.load_follows()["channels"][0]["title"] == "Whisper Room"

# later: two new videos, an upcoming premiere and a live stream
later = flat(["n6", "live1", "soon1", "n5", "n3", "n2", "n1"],
             {"live1": {"live_status": "is_live"}, "soon1": {"live_status": "is_upcoming"}})["entries"]
new = A.follow_new(ch, later)
assert [e["id"] for e in new] == ["n5", "n6"], new                     # oldest first, no live/upcoming
lines = [A.follow_line(ch, e) for e in new]
assert lines == ["https://www.youtube.com/watch?v=n5 | folder=Whisper Room",
                 "https://www.youtube.com/watch?v=n6 | folder=Whisper Room"], lines
assert A.parse_link_line(lines[0]) == ("https://www.youtube.com/watch?v=n5", {"folder": "Whisper Room"})
assert A.follow_line({"title": "A | B"}, new[0]).endswith("| folder=A - B")   # a | in the name can't break the line
print("new videos: oldest first, live and upcoming wait, lines go into the channel's folder")

# run them: n5 works, n6 fails -> n5 seen, n6 tried again next time
out = Path(tempfile.mkdtemp())
run_job = A.Job(lines, "mp4", 1080, out, False, UI())
run_job._acquire_tools = lambda: A.TOOL_LOCK.acquire()
def resolve(url, opts=None):
    vid = url.rsplit("=", 1)[1]
    e = {"id": vid, "title": f"Video {vid}", "webpage_url": url, "extractor_key": "Youtube"}
    if opts:
        e["_yta"] = opts
    return None, [e]
run_job._resolve = resolve
def dl(args, on_line):
    url, home = args[-1], next(a[5:] for a in args if str(a).startswith("home:"))
    if url.endswith("n6"):
        return 1, ["ERROR: [youtube] n6: This video is available to this channel's members"]
    f = Path(home) / "Video n5 [n5].mp4"
    f.write_bytes(b"x")
    on_line("@@F " + str(f))
    return 0, []
run_job._run = dl
run_job.run()
assert (out / "Whisper Room" / "Video n5 [n5].mp4").exists(), list(out.rglob("*"))
assert run_job.ok_ids() == {"n5"} and len(run_job.failed) == 1, (run_job.ok_ids(), run_job.failed)
gave_up = A.follow_record(ch, ["n5", "n6"], run_job.ok_ids())
assert gave_up == [] and "n5" in ch["seen"] and "n6" not in ch["seen"] and ch["fails"] == {"n6": 1}
assert [e["id"] for e in A.follow_new(ch, later)] == ["n6"]
print("results: what worked counts as seen, the failed one comes back next time")

# a video that keeps failing is given up on after 3 tries
assert A.follow_record(ch, ["n6"], set()) == [] and ch["fails"] == {"n6": 2}
assert A.follow_record(ch, ["n6"], set()) == ["n6"] and "n6" in ch["seen"] and ch["fails"] == {}
assert A.follow_new(ch, later) == []
print(f"given up after {A.FOLLOW_GIVE_UP} tries, so one members-only video doesn't block the channel")

# uploading: a video only counts once it's on archive.org too
j = A.Job([], "mp4", 0, tempfile.mkdtemp(), False, UI(), archive={"keys": ("a", "b"), "prefix": "p"})
j.done_ids |= {"a1", "a2"}
j.uploaded_ids |= {"a1"}
assert j.ok_ids() == {"a1"}
print("with upload on: downloaded but not uploaded yet counts as not done")

# "download all of them": nothing seen yet, so everything comes, oldest first
data2 = {"channels": []}
ch2 = A.follow_add(data2, "https://soundcloud.com/someone", "Someone", entries, download_existing=True)
assert ch2["seen"] == [] and [e["id"] for e in A.follow_new(ch2, entries)] == ["n1", "n2", "n3"]

# the daily task runs the installed app quietly
assert A.daily_task_command().endswith('" --check-followed')
assert A.FOLLOW_EVERY["6 hours"] == 6 and A.FOLLOW_EVERY["Never"] == 0
print("ALL FOLLOW TESTS PASSED")
