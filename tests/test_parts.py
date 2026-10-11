import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
import base64, hashlib, http.client, http.server, json, os, re, tempfile, threading, urllib.parse
from pathlib import Path
os.environ["YTD_DATA_DIR"] = tempfile.mkdtemp()
import video_archiver as A
A.IA_PART_SIZE = 1 << 20; A.IA_MULTIPART_OVER = 2 << 20; A.IA_PART_WAITS = (0.05, 0.05, 0.05)
A.UPLOADS_DIR = Path(os.environ["YTD_DATA_DIR"]) / "uploads"
A.wait_for_queue = lambda *a, **k: None

S = {"uploads": {}, "objects": {}, "drop": set(), "log": [], "initiate_status": 200, "single": []}
class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def body(self):
        n = int(self.headers.get("Content-Length") or 0); data = b""
        while len(data) < n:
            c = self.rfile.read(n - len(data))
            if not c: break
            data += c
        return data
    def reply(self, code, text=b""):
        self.send_response(code); self.send_header("Content-Length", str(len(text))); self.end_headers(); self.wfile.write(text)
    def do_POST(self):
        p = urllib.parse.urlsplit(self.path); q = urllib.parse.parse_qs(p.query, keep_blank_values=True); data = self.body()
        if "uploads" in q:
            S["log"].append(("init", dict(self.headers)))
            if S["initiate_status"] != 200: return self.reply(S["initiate_status"], b"<Error><Code>NotImplemented</Code></Error>")
            uid = f"u{len(S['uploads']) + 1}"; S["uploads"][uid] = {"key": p.path, "parts": {}}
            return self.reply(200, f"<InitiateMultipartUploadResult><UploadId>{uid}</UploadId></InitiateMultipartUploadResult>".encode())
        uid = q["uploadId"][0]; up = S["uploads"].get(uid)
        if not up: return self.reply(404, b"<Error><Code>NoSuchUpload</Code></Error>")
        parts = re.findall(r"<PartNumber>(\d+)</PartNumber><ETag>\"([0-9a-f]+)\"</ETag>", data.decode())
        for n, etag in parts:
            if hashlib.md5(up["parts"][int(n)]).hexdigest() != etag: return self.reply(400, b"<Error><Code>InvalidPart</Code></Error>")
        S["objects"][up["key"]] = b"".join(up["parts"][int(n)] for n, _ in parts); S["log"].append(("complete", len(parts)))
        return self.reply(200, b"<CompleteMultipartUploadResult/>")
    def do_PUT(self):
        p = urllib.parse.urlsplit(self.path); q = urllib.parse.parse_qs(p.query)
        n = int(self.headers.get("Content-Length")); 
        if "partNumber" in q:
            num = int(q["partNumber"][0])
            if q["uploadId"][0] not in S["uploads"]:
                self.body(); return self.reply(404, b"<Error><Code>NoSuchUpload</Code></Error>")
            if num in S["drop"]:          # Wi-Fi drop: read half, then cut the connection
                S["drop"].discard(num); self.rfile.read(n // 2); S["log"].append(("drop", num)); self.close_connection = True
                self.connection.shutdown(2); return
            data = self.body()
            assert base64.b64encode(hashlib.md5(data).digest()).decode() == self.headers["Content-MD5"]
            S["uploads"][q["uploadId"][0]]["parts"][num] = data; S["log"].append(("part", num))
            return self.reply(200)
        data = self.body(); S["single"].append(p.path); S["objects"][p.path] = data; return self.reply(200)
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
A._ia_connection = lambda host: http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10, blocksize=1 << 16)
A.ia_item = lambda i: {}

f = Path(tempfile.mkdtemp()) / "Big Video [abc].mp4"; data = os.urandom(5 * (1 << 20) + 12345); f.write_bytes(data)
meta = {"title": "Big Video", "_license": A.LICENSES["CC BY 4.0"], "_trial": True}
msgs, prog = [], []

# 1) connection drops in the middle of part 3: only part 3 is sent again
S["drop"] = {3}
A.ia_upload(("AK", "SK"), "me-abc", f, meta, "movies", lambda *a: prog.append(a[0]), threading.Event(), on_status=msgs.append)
assert S["objects"]["/me-abc/Big%20Video%20%5Babc%5D.mp4"] == data
parts_sent = [x[1] for x in S["log"] if x[0] == "part"]
assert parts_sent == [1, 2, 3, 4, 5, 6] and ("drop", 3) in S["log"], S["log"]
init = [x[1] for x in S["log"] if x[0] == "init"][0]
assert init["x-archive-auto-make-bucket"] == "1" and urllib.parse.unquote(init["x-archive-meta-licenseurl"][4:-1]) == A.LICENSES["CC BY 4.0"]
assert init["x-archive-meta-collection"] == "test_collection" and init["x-archive-size-hint"] == str(len(data))
assert any("connection lost" in m for m in msgs) and prog[-1] == len(data) and not list(A.UPLOADS_DIR.glob("*.json"))
print("drop mid-part: 6 parts, only part 3 repeated, file arrived intact; progress reached 100%")

# 2) Stop after 2 parts, app closed; next time only parts 3-6 are sent
S["log"].clear()
cancel = threading.Event()
def stop_after_two(done, total, *_):
    if done >= 2 * (1 << 20): cancel.set()
try:
    A.ia_upload(("AK", "SK"), "me-xyz", f, meta, "movies", stop_after_two, cancel); raise SystemExit("not stopped")
except A.Cancelled: pass
first = [x[1] for x in S["log"] if x[0] == "part"]
assert len(list(A.UPLOADS_DIR.glob("*.json"))) == 1, "progress must be saved"
S["log"].clear(); msgs.clear()
A.ia_upload(("AK", "SK"), "me-xyz", f, meta, "movies", lambda *a: None, threading.Event(), on_status=msgs.append)
second = [x[1] for x in S["log"] if x[0] == "part"]
assert S["objects"]["/me-xyz/Big%20Video%20%5Babc%5D.mp4"] == data and not [x for x in S["log"] if x[0] == "init"]
assert sorted(set(first) | set(second)) == [1, 2, 3, 4, 5, 6] and not set(first) & set(second), (first, second)
assert any("Continuing an unfinished upload" in m for m in msgs)
print(f"Stop + resume: first run sent parts {first}, second run only {second}")

# 3) the file changed since: start over instead of mixing two versions
cancel = threading.Event()
try: A.ia_upload(("AK", "SK"), "me-chg", f, meta, "movies", stop_after_two, cancel)
except A.Cancelled: pass
data2 = os.urandom(len(data)); f.write_bytes(data2); os.utime(f, (1, 1)); S["log"].clear()
A.ia_upload(("AK", "SK"), "me-chg", f, meta, "movies", lambda *a: None, threading.Event())
assert S["objects"]["/me-chg/Big%20Video%20%5Babc%5D.mp4"] == data2 and [x for x in S["log"] if x[0] == "init"]
print("changed file: started over, new content arrived")

# 4) archive.org forgot the upload -> clear message, next try starts fresh
cancel = threading.Event(); f.write_bytes(data)
try: A.ia_upload(("AK", "SK"), "me-old", f, meta, "movies", stop_after_two, cancel)
except A.Cancelled: pass
S["uploads"].clear()
try: A.ia_upload(("AK", "SK"), "me-old", f, meta, "movies", lambda *a: None, threading.Event()); raise SystemExit("should fail")
except A.IAError as e: assert "forgot" in str(e)
A.ia_upload(("AK", "SK"), "me-old", f, meta, "movies", lambda *a: None, threading.Event())
assert S["objects"]["/me-old/Big%20Video%20%5Babc%5D.mp4"] == data
print("expired upload: explained, then a fresh upload worked")

# 5) parts not supported -> one-go upload; small files never use parts
S["initiate_status"] = 501
A.ia_upload(("AK", "SK"), "me-one", f, meta, "movies", lambda *a: None, threading.Event(), on_status=msgs.append)
assert "/me-one/Big%20Video%20%5Babc%5D.mp4" in S["single"] and any("in one go" in m for m in msgs)
S["initiate_status"] = 200; small = f.with_name("Small [s].mp4"); small.write_bytes(b"x" * 1000)
A.ia_upload(("AK", "SK"), "me-s", small, meta, "movies", lambda *a: None, threading.Event(), remote_name="__ia_thumb.jpg")
assert S["single"][-1] == "/me-s/__ia_thumb.jpg"
print("fallback to one go + small files + custom remote name ok")

# 6) connection never comes back: clear error, progress kept for later
S["drop"] = set(range(1, 7)) ; A.IA_PART_WAITS = (0.01,)
class AlwaysDrop(set):
    def discard(self, x): pass
S["drop"] = AlwaysDrop(range(1, 7))
try: A.ia_upload(("AK", "SK"), "me-down", f, meta, "movies", lambda *a: None, threading.Event()); raise SystemExit("should fail")
except A.IAError as e: assert "progress is saved" in str(e), e
assert any(A.UPLOADS_DIR.glob("*.json"))
print("no connection: error says progress is saved")
print("ALL RESUMABLE-UPLOAD TESTS PASSED")
