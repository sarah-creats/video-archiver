"""Builds "Video Archiver.exe". Started by "Build Video Archiver.bat"; not meant to be run by hand.

usage: build.py WORK_DIR
"""

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request
import zipfile
from pathlib import Path

GH = "https://github.com"

# Pinned versions. Every download must match a checksum or the build stops.
# yt-dlp: immutable GitHub release; the hash below was copied from that release
# page (2026-10-09). The app updates yt-dlp to the newest version on first start.
YTDLP_VERSION = "2026.08.19"
YTDLP_URL = f"{GH}/yt-dlp/yt-dlp/releases/download/{YTDLP_VERSION}/yt-dlp.exe"
YTDLP_SUMS = f"{GH}/yt-dlp/yt-dlp/releases/download/{YTDLP_VERSION}/SHA2-256SUMS"
YTDLP_SHA256 = "66674953fe251b89f4d08c5f0e35e0728679bd67ab3d7d05c0562af101dd3e7a"

# Deno: checked against Deno's own .sha256sum AND an independent SHA-512 taken
# from Chocolatey's reviewed package for the same version.
DENO_VERSION = "v2.9.7"
DENO_URL = f"{GH}/denoland/deno/releases/download/{DENO_VERSION}/deno-x86_64-pc-windows-msvc.zip"
DENO_SUMS = DENO_URL + ".sha256sum"
DENO_SHA512 = ("cc541d118f16e854426033b1645a7594ea29c31e540c17f3db7b787dd6e08bd9"
               "e2cc4338ef02737530bdc4b2a0bdea47f64da73100ab33cf857fbfbb9234a364")
MIN_DENO = (2, 3, 0)

# FFmpeg: yt-dlp's own builds, the ones its maintainers recommend. They only
# publish rolling master builds and delete dated ones after a few weeks, so the
# file can't be pinned; it must match the release's checksum list instead.
FFMPEG_URL = GH + "/yt-dlp/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl-shared.zip"
FFMPEG_SUMS = GH + "/yt-dlp/FFmpeg-Builds/releases/download/latest/checksums.sha256"
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


# The app source sits next to this script; install paths, the icon and the
# shortcut helpers come from it, so installer and uninstaller always agree.
from video_archiver import (APP_BLURB, APP_NAME, INSTALL_DIR, INSTALL_EXE, UNINSTALL_KEY,
                     icon_ico, known_folder, make_shortcut, migrate_old_data, remove_old_install)


def say(msg=""):
    print(msg, flush=True)


def fetch(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": "yta-builder"})
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as out:
        total = int(r.headers.get("Content-Length") or 0)
        done, shown = 0, -10
        while chunk := r.read(1 << 20):
            out.write(chunk)
            done += len(chunk)
            if total and (pct := done * 100 // total) >= shown + 10:
                shown = pct
                print(f"\r      {pct:3d}%  ({done >> 20} of {total >> 20} MB)", end="", flush=True)
    print(f"\r      done  ({done >> 20} MB)".ljust(40), flush=True)


def fetch_text(url):
    req = urllib.request.Request(url, headers={"User-Agent": "yta-builder"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", "replace")


def sha256(path, algo="sha256"):
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def expected_hash(sums_text, filename):
    """Find the hash for filename in a checksum list, or the only hash in a single-file one."""
    for line in sums_text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == filename and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            return parts[0].lower()
    found = set(h.lower() for h in re.findall(r"\b[0-9a-fA-F]{64}\b", sums_text))
    return found.pop() if len(found) == 1 else None


def verify(path, sums_url, name):
    """Compare with the project's published checksum. Fails closed."""
    try:
        want = expected_hash(fetch_text(sums_url), name)
    except Exception:
        want = None
    if not want:
        raise SystemExit(f"Couldn't get the official checksum for {name}. Stopping to be safe.")
    if sha256(path) != want:
        raise SystemExit(f"{name} doesn't match its official checksum. Stopping to be safe.")
    say(f"      matches the official checksum")


def verify_pinned(path, algo, want, name):
    """Compare with a hash written into this builder. Fails closed."""
    if sha256(path, algo) != want.lower():
        raise SystemExit(f"{name} doesn't match the pinned {algo.upper()} in this builder. "
                         "Stopping to be safe.")
    say(f"      matches the pinned {algo.upper()}")


def extract_ffmpeg(zip_path, dest):
    """Copy ffmpeg.exe, ffprobe.exe and their DLLs out of the build's bin folder."""
    dest.mkdir(parents=True, exist_ok=True)
    wanted = 0
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            parts = info.filename.replace("\\", "/").split("/")
            if len(parts) < 2 or parts[-2] != "bin" or info.is_dir():
                continue
            name = parts[-1]
            if name.lower() in ("ffmpeg.exe", "ffprobe.exe") or name.lower().endswith(".dll"):
                with z.open(info) as src, open(dest / name, "wb") as out:
                    shutil.copyfileobj(src, out)
                wanted += name.lower() in ("ffmpeg.exe", "ffprobe.exe")
        for info in z.infolist():  # keep FFmpeg's license texts with the program
            parts = info.filename.replace("\\", "/").split("/")
            if len(parts) == 2 and parts[1].upper().startswith(("LICENSE", "COPYING")):
                with z.open(info) as src, open(dest / parts[1], "wb") as out:
                    shutil.copyfileobj(src, out)
    if wanted != 2:
        raise SystemExit("The FFmpeg download didn't contain ffmpeg.exe and ffprobe.exe.")


def extract_one(zip_path, member, dest):
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            if info.filename.replace("\\", "/").split("/")[-1].lower() == member.lower():
                with z.open(info) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
                return
    raise SystemExit(f"The download didn't contain {member}.")


def tool_version(cmd):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120, creationflags=NO_WINDOW)
        lines = (r.stdout or r.stderr).strip().splitlines()
        return lines[0][:70] if r.returncode == 0 and lines else None
    except Exception:
        return None


def pack(tools_dir, archive):
    with tarfile.open(archive, "w:xz", preset=6) as tar:
        for p in sorted(tools_dir.rglob("*")):
            tar.add(p, arcname=p.relative_to(tools_dir).as_posix(), recursive=False)


LICENSE_NOTE = """Licenses of the bundled tools
- yt-dlp (yt-dlp.exe): the project is Unlicense; this Windows build bundles GPLv3+ code.
  https://github.com/yt-dlp/yt-dlp/blob/master/THIRD_PARTY_LICENSES.txt
- FFmpeg (ffmpeg folder): GPL build; its license text is in the ffmpeg folder.
  Source: https://github.com/yt-dlp/FFmpeg-Builds
- Deno (deno.exe): MIT. https://github.com/denoland/deno/blob/main/LICENSE.md
If you give Video Archiver to someone else, these licenses (including the GPL's
obligations, e.g. offering the source) apply to that copy.
"""


def main():
    work = Path(sys.argv[1])
    tools, dl = work / "tools", work / "downloads"
    for d in (tools, dl):
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True)

    say(f"      Downloading yt-dlp {YTDLP_VERSION}...")
    fetch(YTDLP_URL, tools / "yt-dlp.exe")
    verify_pinned(tools / "yt-dlp.exe", "sha256", YTDLP_SHA256, "yt-dlp.exe")
    verify(tools / "yt-dlp.exe", YTDLP_SUMS, "yt-dlp.exe")

    say(f"      Downloading Deno {DENO_VERSION}...")
    fetch(DENO_URL, dl / "deno.zip")
    verify_pinned(dl / "deno.zip", "sha512", DENO_SHA512, "Deno")
    verify(dl / "deno.zip", DENO_SUMS, "deno-x86_64-pc-windows-msvc.zip")
    extract_one(dl / "deno.zip", "deno.exe", tools / "deno.exe")

    say("      Downloading FFmpeg (yt-dlp's build)...")
    fetch(FFMPEG_URL, dl / "ffmpeg.zip")
    verify(dl / "ffmpeg.zip", FFMPEG_SUMS, FFMPEG_URL.rsplit("/", 1)[1])
    say(f"      FFmpeg file SHA-256: {sha256(dl / 'ffmpeg.zip')}")
    extract_ffmpeg(dl / "ffmpeg.zip", tools / "ffmpeg")
    (tools / "LICENSES.txt").write_text(LICENSE_NOTE)
    shutil.rmtree(dl, ignore_errors=True)

    say("      Checking that the tools run...")
    versions = {
        "yt-dlp": tool_version([str(tools / "yt-dlp.exe"), "--version"]),
        "Deno": tool_version([str(tools / "deno.exe"), "--version"]),
        "FFmpeg": tool_version([str(tools / "ffmpeg" / "ffmpeg.exe"), "-version"]),
    }
    for name, v in versions.items():
        if not v:
            raise SystemExit(f"{name} was downloaded but doesn't start. Stopping.")
        say(f"      {name}: {v}")
    m = re.search(r"deno (\d+)\.(\d+)\.(\d+)", versions["Deno"])
    if not m or tuple(map(int, m.groups())) < MIN_DENO:
        raise SystemExit("This Deno is older than yt-dlp needs (2.3 or newer). Stopping.")

    say("      Compressing the tools (the slowest step, a few minutes)...")
    pack(tools, work / "tools.tar.xz")
    (work / "yta_bundle_id.txt").write_text(time.strftime("%Y%m%d-%H%M%S") + " " + versions["yt-dlp"])

    (work / "app.ico").write_bytes(icon_ico())
    say("      Building the exe...")
    sep = os.pathsep
    r = subprocess.run([sys.executable, "-m", "PyInstaller", "--onefile", "--windowed",
                        "--noconfirm", "--clean", "--log-level", "WARN", "--name", APP_NAME,
                        "--distpath", str(work / "dist"), "--workpath", str(work / "pyi"),
                        "--specpath", str(work), "--icon", str(work / "app.ico"),
                        "--add-data", f"{work / 'tools.tar.xz'}{sep}.",
                        "--add-data", f"{work / 'yta_bundle_id.txt'}{sep}.",
                        str(work / "video_archiver.py")])
    exe = work / "dist" / f"{APP_NAME}.exe"
    if r.returncode != 0 or not exe.exists():
        raise SystemExit("PyInstaller couldn't build the exe (see the messages above).")

    install(exe)


def register_app(exe):
    """List the app under Settings > Apps, with Uninstall pointing at the exe."""
    import winreg
    values = {"DisplayName": APP_NAME, "DisplayIcon": f"{exe},0",
              "DisplayVersion": time.strftime("%Y.%m.%d"), "Publisher": "Personal build",
              "Comments": APP_BLURB, "InstallLocation": str(exe.parent),
              "UninstallString": f'"{exe}" --uninstall'}
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as key:
        for name, value in values.items():
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
        for name, value in (("NoModify", 1), ("NoRepair", 1),
                            ("EstimatedSize", max(1, exe.stat().st_size // 1024))):
            winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, value)


def install(exe):
    """Copy the exe to its own Programs folder and add shortcuts. Nothing of
    other app is touched: own folder, file names and Apps entry."""
    left = remove_old_install(say)
    if left:
        say("      Note: YT Archiver (the old name) is probably still open, so these stay. Close it")
        say("      and delete them by hand: " + "; ".join(left))
    note = migrate_old_data()
    if note:
        say("      " + note)
    say(f"      Installing to {INSTALL_DIR}...")
    try:
        INSTALL_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(exe, INSTALL_EXE)
    except OSError:
        raise SystemExit(f"Couldn't write {INSTALL_EXE}. Close {APP_NAME} and run the builder again.")
    say(f"      Size: {INSTALL_EXE.stat().st_size >> 20} MB")
    if sys.platform != "win32":
        return

    desktop = known_folder("Desktop", Path.home() / "Desktop")
    old = desktop / f"{APP_NAME}.exe"   # the earlier version put a bare exe on the desktop
    if old.is_file():
        try:
            old.unlink()
            say(f"      Removed the old {old.name} from the desktop (replaced by a shortcut).")
        except OSError:
            say(f"      Note: couldn't remove the old {old.name} from the desktop. Close it "
                "and delete it by hand; the shortcut replaces it.")

    for lnk in (desktop / f"{APP_NAME}.lnk",
                known_folder("Programs", Path.home()) / f"{APP_NAME}.lnk"):
        if make_shortcut(lnk, INSTALL_EXE):
            say(f"      Shortcut: {lnk}")
        else:
            say(f"      Note: couldn't create the shortcut {lnk}")
    try:
        register_app(INSTALL_EXE)
        say("      Listed under Settings > Apps (uninstall from there or from Help).")
    except OSError:
        say("      Note: couldn't add the Settings > Apps entry. Help > Uninstall still works.")
    say("      Note: FFmpeg is GPL and the yt-dlp program includes GPLv3+ parts. If you give")
    say("      this exe to others, those licenses apply; see tools\\LICENSES.txt after first start.")
    os.startfile(INSTALL_EXE)


if __name__ == "__main__":
    main()
