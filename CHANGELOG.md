# Changelog

## 1.3.0

- **Ready-made download:** every GitHub release now has `Video-Archiver-<version>.exe`,
  built by GitHub from this repository. Run it and it offers to install itself (or to
  update the installed copy). The `.bat` builder still works for people who'd rather
  build their own. The release also carries the source code of the bundled GPL programs.
- **Follow channels** (Step 1 > Channels...): only new videos are fetched, oldest first,
  each channel into its own folder. Check by hand, when the app opens, every few hours
  while it's open, or once a day while it's closed (a Windows scheduled task). Live
  streams wait until they're over; a video that keeps failing is left out after 3 tries.
- **Original quality** (Step 2): the best video and audio exactly as the site has them,
  nothing converted (MKV), or the original audio track only (`.opus`/`.m4a`). Best for ASMR.
- Fixed: the window from 1.2.2 could hide the activity log on screens shorter than the
  whole app. The links and the log now keep their size and the settings scroll instead.

## 1.2.3

- Capture: a stream counts as live only if its playlist keeps reloading, not because of
  its server's name, so a site's saved videos are no longer mistaken for live-cam ads.
  Live lines start with [LIVE] in the list.
- Capture never uses a live stream or live-cam ad by itself any more. If only those
  load, it says so and uses nothing; click a [LIVE] line yourself if you really want it.
- When capture finds no video, the activity log lists the 8 biggest loads and what types
  of data the browser loaded, to help work out how the site sends its video.
- Stream pieces disguised as images are recognised from 100 KB (was 150 KB).

## 1.2.2

- Resizable window: drag the bars between the links, the settings and the activity log.
  The settings scroll when you make them small; Download and Stop always stay visible.
  Size and layout are remembered. **View > Make the activity log bigger** and
  **View > Reset the layout** do it in one click.
- Capture: live streams (their playlist reloads every few seconds) and known live-cam
  ad servers go to the bottom of the list and are marked LIVE/AD, so an ad box on the
  page no longer wins over the video you're watching.

## 1.2.1

- Capture finds videos on sites that hide them: the browser's log says what each load
  really is (content type and size), so a playlist like `/api/source?id=7` or stream
  pieces named `img12.jpg` are recognised. When several videos load (ads first), the
  one the browser loaded the most of wins.
- The capture window shows the browser's traffic live (requests read, MB of video), so
  you can see that it's working, and you can click a different address in the list.
- If nothing is found, the activity log lists the biggest loads, to help find out why.
- Hidden playlist links get their own id and the page's title, so `?id=7` and `?id=8`
  are two videos.

## 1.2.0

- Faster: the next video downloads while the previous one uploads, instead of
  waiting for the upload to finish. A second progress line shows the upload.
- Faster uploads: big files send 3 parts at the same time.
- Faster downloads: videos that come in many small pieces (HLS/DASH) load 4 pieces at once.
- New: **Keep a copy on this PC** switch in Step 4. Untick it to only upload: each video
  goes into a hidden temporary folder, is uploaded, then deleted. Videos that are
  already on archive.org aren't downloaded at all. If an upload fails, the file is
  moved into your folder instead, so **Upload files...** can finish it later.

## 1.1.1

- Capture: a small always-on-top window now shows the video addresses live while you
  play the video. Its buttons only react to the mouse, so pressing Space to play the
  video can no longer end the capture (which closed the browser after a second).
- Capture: picks the address the browser loaded the most stream pieces from, so the
  real video wins over ads that play first.
- Capture: the "unsupported command-line flag" warning bar no longer appears.
- Capture: closes Edge reliably, also when it hands its window to a second process;
  closing the browser window yourself finishes the capture too.

## 1.1.0

- New: **Capture a video's stream address** from a separate browser window (Edge, Chrome
  or Brave, fresh throwaway profile). Works on sites that refuse to play while DevTools
  are open, because DevTools stay closed; addresses come from the browser's own network log.
- New: the site wizard, and the plugins it builds, also look inside the player's own page
  (iframes, two levels deep) and the site's script files, and send the player's Referer.
- The site wizard has a "Capture from a browser window" button that fills in the stream
  address for you.
- Fixed: site links with a port number (`site.tv:8080/...`) now work in the site wizard.

## 1.0.0

First public version.

- Download from YouTube and 1,000+ other sites as MP3 or MP4 (up to 4K)
- Upload to archive.org with topics, author, license, language, thumbnail and subtitles
- Resumable uploads in parts; waits when archive.org's queue is full
- Upload files that were downloaded earlier (Upload files…)
- Trial mode using archive.org's test collection
- Plain-language explanations when archive.org refuses an upload; videos with a
  hidden item are asked about once, at the end of a run
- Teach-a-new-site wizard, stream links, generic page scanner fallback
- Cookies from Firefox or a cookies.txt file; YouTube PO-token helper
- Every builder download checked against a pinned checksum
