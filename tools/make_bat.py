"""Assemble Build_Video_Archiver.bat from the files in src/.

The builder is a single .bat file so people can download one file and run it.
It carries the build script and the app's source after two marker lines; when
run, it copies them back out into a temporary folder. Edit the files in src/,
then run:

    python tools/make_bat.py

and commit the regenerated Build_Video_Archiver.bat together with your change.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
OUT = ROOT / "Build_Video_Archiver.bat"
BUILD_MARK, APP_MARK = "#=== BUILD SCRIPT ===#", "#=== APP SOURCE ===#"


def read(name):
    return (SRC / name).read_text(encoding="utf-8").replace("\r\n", "\n")


def assemble():
    launcher, build, app = read("launcher.bat"), read("build.py"), read("video_archiver.py")
    for name, text in (("build.py", build), ("video_archiver.py", app)):
        if BUILD_MARK in text or APP_MARK in text:
            raise SystemExit(f"src/{name} must not contain the marker lines.")
    text = (launcher + BUILD_MARK + "\n" + build.strip("\n") + "\n\n" + APP_MARK + "\n" + app)
    return text.replace("\n", "\r\n")      # cmd.exe needs Windows line endings


if __name__ == "__main__":
    data = assemble()
    with open(OUT, "w", encoding="utf-8", newline="") as f:
        f.write(data)
    print(f"Wrote {OUT.name} ({len(data.encode('utf-8')):,} bytes)")
