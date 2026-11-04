import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
import http.server, os, re, shutil, tempfile, threading, time, types
from pathlib import Path
os.environ["YTD_DATA_DIR"] = tempfile.mkdtemp(); os.environ["APPDATA"] = tempfile.mkdtemp()
import video_archiver as A

# ---------------- the network-log reader ----------------
log = ('{"events":[{"params":{"url":"https://site.tv/watch/7"}},'
       '{"params":{"url":"https://ads.example/ads/preroll.mp4"}},'
       '{"params":{"url":"https://cdn.tv/hls/7/master.m3u8?token=a\\u0026exp=9"}},'
       '{"params":{"url":"https://cdn.tv/hls/7/720p.m3u8?token=a"}},'
       '{"params":{"url":"https://cdn.tv/hls/7/seg1.ts"}},{"params":{"url":"https://cdn.tv/hls/7/seg2.ts"}},'
       '{"params":{"url":"https://cdn.tv/hls/7/seg2.ts"}},{"params":{"url":"https://site.tv/poster.jpg"}}'
       # no closing brackets: a log cut off when the browser was closed is still read
       )
found, segments = A.parse_netlog(log)
assert found == ["https://cdn.tv/hls/7/master.m3u8?token=a&exp=9", "https://cdn.tv/hls/7/720p.m3u8?token=a",
                 "https://ads.example/ads/preroll.mp4"], found
assert segments == 2 and A.parse_netlog("") == ([], 0)
print("network log: main playlist first, ads last, cut-off log still read")

# ---------------- a fake web: players hidden in iframes and scripts ----------------
def page(title, body):
    return f'<html><head><meta property="og:title" content="{title}"><title>{title}</title></head><body>{body}</body></html>'
PAGES = {
    # A) iframe to a player host; the player host only answers with the right Referer
    "/a/watch/101": page("Folge 101", '<script src="https://www.googletagmanager.com/gtag.js"></script>'
                         '<iframe src="http://127.0.0.1:{port}/player/embed/x101"></iframe>'),
    "/a/watch/102": page("Folge 102", '<iframe src="http://127.0.0.1:{port}/player/embed/x102"></iframe>'),
    "/player/embed/x101": '<script>var cfg={file:"https:\\/\\/cdn.player.net\\/v\\/x101\\/index.m3u8"}</script>',
    "/player/embed/x102": '<script>var cfg={file:"https:\\/\\/cdn.player.net\\/v\\/x102\\/index.m3u8"}</script>',
    # B) the address is in the site's own script file
    "/b/v/cat": page("Cat", '<div id="p" data-id="cat"></div><script src="/b/js/player-cat.js"></script>'),
    "/b/js/player-cat.js": 'jwplayer("p").setup({sources:[{file:"/b/media/cat/master.m3u8"}]});',
    # C) two levels of players: page > mirror host > real player
    "/c/ep/1": page("Ep 1", '<iframe data-src="/c/mirror/1"></iframe>'),
    "/c/mirror/1": '<iframe src="/c/real/1"></iframe>',
    "/c/real/1": '<video><source src="https://cdn.c.tv/1.mp4" type="video/mp4"></video>',
    # D) a known site's embed: handed to yt-dlp, never fetched
    "/d/post": page("Trip", '<iframe src="https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ"></iframe>'),
    # E) only made while playing: nothing to find without capturing
    "/e/show/9": page("Show 9", '<div id="app"></div><script src="/e/app.js"></script>'),
    "/e/app.js": 'fetch(["/api","/token"].join("")).then(r=>r.json()).then(d=>player.load(d.u));',
}
fetched = []
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        fetched.append(self.path)
        body = PAGES.get(self.path)
        if self.path.startswith("/player/") and "/a/watch/" not in (self.headers.get("Referer") or ""):
            self.send_response(403); self.end_headers(); return        # player host checks the Referer
        if body is None:
            self.send_response(404); self.end_headers(); return
        data = body.replace("{port}", str(PORT)).encode()
        self.send_response(200); self.send_header("Content-Type", "text/html"); self.send_header("Content-Length", str(len(data)))
        self.end_headers(); self.wfile.write(data)
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H); PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
B = f"http://127.0.0.1:{PORT}"

core = A.plugin_core()
def fetch(u, ref):
    try: return A.fetch_page(u, referer=ref)[0]
    except A.PageError: return None
def deep(path, site=None):
    url = B + path; text = A.fetch_page(url)[0]
    return core["choose_deep"](text, url, "id", site or {}, fetch)

k, t, ref = deep("/a/watch/101")
assert (k, t, ref) == ("stream", "https://cdn.player.net/v/x101/index.m3u8", f"{B}/player/embed/x101"), (k, t, ref)
assert not any("googletagmanager" in f for f in fetched)
k, t, ref = deep("/b/v/cat"); assert (k, t, ref) == ("stream", f"{B}/b/media/cat/master.m3u8", f"{B}/b/v/cat")
k, t, ref = deep("/c/ep/1"); assert (k, t, ref) == ("stream", "https://cdn.c.tv/1.mp4", f"{B}/c/real/1")
fetched.clear(); k, t, ref = deep("/d/post"); assert k == "embed" and "youtube" in t and fetched == ["/d/post"]
k, t, ref = deep("/e/show/9"); assert (k, t) == (None, None)
print("deep reading: player iframe (with Referer check), site script, 2-level iframe, known embed untouched")

# ---------------- the wizard and the plugin it writes ----------------
a = A.analyze_site(B + "/a/watch/101", B + "/a/watch/102")
assert a["ok"] and any("inside the player page" in l for l in a["lines"]) and any("Good sign" in l for l in a["lines"]), a["lines"]
assert a["site"]["deep"] and a["site"]["prefer_host"] == "cdn.player.net"
a_b = A.analyze_site(B + "/b/v/cat"); assert a_b["ok"] and any("script files" in l for l in a_b["lines"])
e = A.analyze_site(B + "/e/show/9")
assert not e["ok"] and any("Capture from a browser window" in l for l in e["lines"])
# a captured address that's also inside the player page: plugin can find it for other videos
c = A.analyze_site(B + "/a/watch/101", stream="https://cdn.player.net/v/x101/index.m3u8?tok=1")
assert c["ok"] and any("inside the player page" in l for l in c["lines"]), c["lines"]
print("wizard: finds addresses in player pages/scripts, explains capture when needed")

common = types.ModuleType("yt_dlp.extractor.common")
class InfoExtractor:
    def _match_id(self, url): return re.match(self._VALID_URL, url).group("id")
    def _download_webpage(self, url, video_id, note=None, errnote=None, fatal=True, headers={}):
        try: return A.fetch_page(url, referer=headers.get("Referer"))[0]
        except A.PageError:
            if fatal: raise
            return False
    def url_result(self, url): return {"_type": "url", "url": url}
    def raise_no_formats(self, msg, expected=False): raise RuntimeError(msg)
    def _extract_m3u8_formats(self, u, vid, ext=None, m3u8_id=None, headers={}): return [{"url": u, "h": headers}]
    def _extract_mpd_formats(self, u, vid, mpd_id=None, headers={}): return [{"url": u, "h": headers}]
common.InfoExtractor = InfoExtractor
sys.modules.update({"yt_dlp": types.ModuleType("yt_dlp"), "yt_dlp.extractor": types.ModuleType("yt_dlp.extractor"),
                    "yt_dlp.extractor.common": common})
path = A.write_site_plugin(a["pattern"].replace("*", "*", 1), B + "/a/watch/101", a["site"])
ns = {}; exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), ns)
IE = next(v for k2, v in ns.items() if k2.endswith("IE") and k2 != "InfoExtractor")
info = IE()._real_extract(B + "/a/watch/102")
assert info["formats"][0]["url"] == "https://cdn.player.net/v/x102/index.m3u8"
assert info["http_headers"]["Referer"] == f"{B}/player/embed/x102" and info["title"] == "Folge 102"
print("plugin: found video 102 inside its player page, sends the player's Referer")

# ---------------- a real browser (skipped if none is installed) ----------------
browser = (A.find_browser() or next((Path(p) for p in map(shutil.which, ("google-chrome", "chromium", "chromium-browser")) if p), None)
           or next(iter(Path("/opt/pw-browsers").glob("chromium-*/chrome-linux/chrome")), None))
if not browser:
    print("ALL CAPTURE TESTS PASSED (real-browser capture skipped: no Chrome/Edge here)"); raise SystemExit(0)
PAGES["/live/7"] = page("Live 7", '<script>setInterval(function(){debugger;},300);'
                                  'if (window.outerWidth-window.innerWidth>160) document.body.innerHTML="DevTools";</script>'
                                  '<script src="/live/p.js"></script>')
PAGES["/live/p.js"] = 'addEventListener("load",()=>fetch(["/hls/","7/ma","ster.m3","u8"].join("")));'
PAGES["/hls/7/master.m3u8"] = "#EXTM3U"
cap = A.BrowserCapture(B + "/live/7", browser=browser)
cap.start(extra_args=("--headless=new", "--no-sandbox"))
time.sleep(6)
found, segments = cap.finish()
assert found and found[0] == f"{B}/hls/7/master.m3u8" and not cap.dir.exists(), found
print(f"real browser ({browser.name}): captured the address a DevTools-trap page builds while playing")
print("ALL CAPTURE TESTS PASSED")
