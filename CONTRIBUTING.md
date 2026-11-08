# Contributing

Thanks for helping! Bug reports, fixes and ideas are all welcome.

## Making a change

1. Edit the files in `src/` (never edit `Build_Video_Archiver.bat` by hand).
2. Rebuild the one-file builder and run the tests:

   ```
   python tools/make_bat.py
   python tests/run_all.py
   ```

3. Commit your change **and** the regenerated `Build_Video_Archiver.bat`.
   GitHub runs the same checks on every pull request.

The tests need Python 3.11 or newer. `test_folder.py` and `test_formats.py` also need
`ffmpeg`/`ffprobe` (and `pip install yt-dlp` for the second) and are skipped without them.
The app itself only runs on Windows, but the tests run anywhere.

## Making a release

1. Set `APP_VERSION` in `src/video_archiver.py` (for example `1.3.0`), add the changes to
   `CHANGELOG.md`, run `python tools/make_bat.py` and commit.
2. On GitHub: **Releases > Draft a new release**, create the tag `v` + that version
   (`v1.3.0`), paste the changelog section as the description, attach
   `Build_Video_Archiver.bat`, and publish.
3. The **release** workflow (Actions tab) then builds `Video-Archiver-<version>.exe` on
   GitHub's Windows machine and attaches it to the release, with `SHA256SUMS.txt`,
   `SOURCES-AND-LICENSES.txt` and the source code of yt-dlp and FFmpeg. It takes about
   10 minutes. If the tag and `APP_VERSION` don't match, it stops and says so.

To test a build without publishing anything: **Actions > release > Run workflow**; the
files appear under the run as "video-archiver-windows".

## Guidelines

- Keep messages in the app short and in plain words; say what to do next.
- Every download the builder makes must be checked against a pinned checksum.
- Never log or store archive.org keys or cookies in plain text outside the app's folder.
- New features that upload content should keep the defaults on the safe side
  (for example, show where a video came from unless the user turns that off).
