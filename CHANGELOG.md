# Changelog

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
