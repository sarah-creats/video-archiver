import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
import http.client, http.server, os, tempfile, threading
from pathlib import Path
os.environ["YTD_DATA_DIR"] = tempfile.mkdtemp()
import video_archiver as A
K = ("AK", "SK")
ok_auth = lambda keys: ("Louis", "louis@x.de")
def bad_auth(keys): raise A.IAError("rejected", rejected=True)
def offline(keys): raise A.IAError("offline")
E = lambda **kw: A.explain_denied(K, "me-abc", kw.pop("remote", "v.mp4"), kw.pop("user", ""), **kw)
cases = {
 "keys":    E(check_auth=bad_auth, get_item=lambda i: {}),
 "dark":    E(check_auth=ok_auth, get_item=lambda i: {"is_dark": True, "metadata": {"uploader": "louis@x.de"}}),
 "theirs":  E(check_auth=ok_auth, get_item=lambda i: {"metadata": {"uploader": "other@y.org"}, "files": []}),
 "blocked": E(check_auth=ok_auth, get_item=lambda i: {"created": 1, "files": []}),
 "name":    E(check_auth=ok_auth, get_item=lambda i: {}, remote="__ia_thumb.jpg"),
 "account": E(check_auth=ok_auth, get_item=lambda i: {"metadata": {"uploader": "louis@x.de"}}),
 "unknown": E(check_auth=offline, get_item=lambda i: (_ for _ in ()).throw(A.IAError("x"))),
}
assert "no longer accepts your keys" in cases["keys"]
assert "darked" in cases["dark"] and "another account" in cases["theirs"] and "isn't showing it" in cases["blocked"]
assert "doesn't allow uploading a file with this name" in cases["name"]
assert cases["account"].startswith(A.ACCOUNT_REFUSED) and "s3help" in cases["account"] and cases["unknown"].startswith(A.ACCOUNT_REFUSED)
assert all("https://archive.org/details/me-abc" in m for m in cases.values())
for k, m in cases.items(): print(f"  {k:8} {m.lstrip(A.ACCOUNT_REFUSED)[:150]}...")

# real upload paths: single PUT and upload in parts both answer 403
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _deny(self):
        n = int(self.headers.get("Content-Length") or 0); self.rfile.read(n)
        body = b"<Error><Code>AccessDenied</Code><Message>Access Denied</Message></Error>"
        self.send_response(403); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    do_PUT = do_POST = _deny
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
A._ia_connection = lambda host: http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
A.wait_for_queue = lambda *a, **k: None
A.ia_check_auth = ok_auth; A.ia_item = lambda i: {}
A.IA_MULTIPART_OVER = 1000
f = Path(tempfile.mkdtemp()) / "Clip [abc].mp4"
for size, way in ((500, "one go"), (5000, "in parts")):
    f.write_bytes(b"x" * size)
    try: A.ia_upload(K, "me-abc", f, {"title": "t"}, "movies", lambda *a: None, threading.Event()); raise SystemExit("no error")
    except A.IAError as e:
        assert e.account and "Clip [abc].mp4" in str(e) and not str(e).startswith(A.ACCOUNT_REFUSED), str(e)
    print(f"403 while uploading {way}: diagnosed, marked as account-wide")
A.ia_item = lambda i: {"metadata": {"uploader": "other@y.org"}}
f.write_bytes(b"x" * 500)
try: A.ia_upload(K, "me-abc", f, {"title": "t"}, "movies", lambda *a: None, threading.Event())
except A.IAError as e: assert not e.account and "another account" in str(e)
print("403 on someone else's item: not account-wide (other videos still upload)")

# a run: after an account-wide refusal the rest is only downloaded
class UI:
    def __init__(s): s.logs = []
    def log(s, t, tag=None): s.logs.append((t, tag))
    def now(s, t): pass
    def progress(s, p, d): pass
    def confirm(s, q): return True
calls = []
def refuse(*a, **k):
    calls.append(a[1]); e = A.IAError("refused"); e.account = True; raise e
A.ia_upload = refuse; A.ia_item = lambda i: {}
ui = UI(); job = A.Job([], "mp4", 0, f.parent, False, ui, archive={"keys": K, "prefix": "me", "username": ""})
for vid in ("aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"):
    p = f.parent / f"V [{vid}].mp4"; p.write_bytes(b"1")
    job._archive(p, {"title": "V", "id": vid, "extractor_key": "Youtube"}, vid, {})
assert calls == ["me-aaaaaaaaaaa"] and len(job.failed) == 1 and job.skipped == 2
assert any("only downloaded" in t for t, _ in ui.logs)
print("account refused: 1 failure, the other 2 skipped with a note (no hammering)")

# thumbnail name refused -> uploaded under an ordinary name instead
sent = []
def picky(keys, ident, path, meta, mt, prog, cancel, on_status=None, topics=None, remote_name=None):
    if remote_name == "__ia_thumb.jpg": raise A.IAError("archive.org doesn't allow uploading a file with this name.")
    sent.append(remote_name)
A.ia_upload = picky
ui = UI(); job = A.Job([], "mp4", 0, f.parent, False, ui, archive={"keys": K, "prefix": "me", "username": "", "extras": True})
job.staging = f.parent / ".yta-incomplete" / "x"; job.staging.mkdir(parents=True)
thumb = job._extra_dir() / "Clip [abc].jpg"; thumb.write_bytes(b"\xff\xd8\xff" + b"0" * 100)
job._subtitles_for = lambda *a: []
job._upload_extras("me-abc", f, {"title": "t"}, "movies", {}, "abc", [])
assert sent == ["Clip [abc].thumb.jpg"] and any("thumbnail (as Clip [abc].thumb.jpg)" in t for t, _ in ui.logs)
print("thumbnail name refused: uploaded as an ordinary image instead")
print("ALL ACCESS-DENIED TESTS PASSED")
