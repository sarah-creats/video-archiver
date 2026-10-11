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

The tests need Python 3.11 or newer. `test_folder.py` also needs `ffmpeg` and is
skipped without it. The app itself only runs on Windows, but the tests run anywhere.

## Guidelines

- Keep messages in the app short and in plain words; say what to do next.
- Every download the builder makes must be checked against a pinned checksum.
- Never log or store archive.org keys or cookies in plain text outside the app's folder.
- New features that upload content should keep the defaults on the safe side
  (for example, show where a video came from unless the user turns that off).
