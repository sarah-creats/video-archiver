import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
import json, os, sys, tempfile
from pathlib import Path
local = Path(tempfile.mkdtemp()); os.environ["LOCALAPPDATA"] = str(local); os.environ.pop("YTD_DATA_DIR", None)
import video_archiver as A
assert A.APP_NAME == "Video Archiver" and A.DATA_DIR == local / "VideoArchiver" and A.OLD_DATA_DIR == local / "YTArchiver"
assert A.INSTALL_EXE == local / "Programs" / "Video Archiver" / "Video Archiver.exe"

# 1) old data folder is moved over: settings, keys, library, unfinished uploads
old = A.OLD_DATA_DIR
(old / "uploads").mkdir(parents=True); (old / "tools").mkdir()
(old / "settings.json").write_text(json.dumps({"ia_prefix": "louis", "author": "Louis", "out_dir": "C:/x/YT Archiver"}))
(old / "ia_keys.bin").write_bytes(b"\x01\x02secret"); (old / "library.json").write_text("{}")
(old / "uploads" / "abc.json").write_text('{"upload_id": "u1"}')
note = A.migrate_old_data()
assert note.startswith("Moved") and not old.exists()
assert A.load_settings()["ia_prefix"] == "louis" and A.IA_KEYS_FILE.read_bytes() == b"\x01\x02secret"
assert (A.DATA_DIR / "uploads" / "abc.json").exists() and A.load_settings()["out_dir"] == "C:/x/YT Archiver"
assert A.migrate_old_data() is None                     # only once
print("old settings/keys/upload progress moved:", note)

# 2) new folder already there (app started before): old one left alone
old.mkdir(); (old / "settings.json").write_text('{"ia_prefix": "stale"}')
assert A.migrate_old_data() is None and A.load_settings()["ia_prefix"] == "louis"

# 3) rename impossible (old app open): copy instead, tools skipped
import shutil; shutil.rmtree(A.DATA_DIR); (old / "tools").mkdir(); (old / "tools" / "yt-dlp.exe").write_bytes(b"x" * 10)
real_rename = Path.rename
Path.rename = lambda self, target: (_ for _ in ()).throw(PermissionError(13, "in use"))
note = A.migrate_old_data(); Path.rename = real_rename
assert note.startswith("Copied") and old.exists() and A.load_settings()["ia_prefix"] == "stale" and not (A.DATA_DIR / "tools").exists()
print("old folder in use: copied instead (tools skipped):", note[:60], "...")

# 4) test override: never touches anything
os.environ["YTD_DATA_DIR"] = str(local / "elsewhere"); assert A.migrate_old_data() is None; del os.environ["YTD_DATA_DIR"]

# 5) old install removed: only shortcuts pointing at the old exe, the old exe, its folder if empty
desk, progs = local / "Desktop", local / "Start"; desk.mkdir(); progs.mkdir()
A.OLD_INSTALL_DIR.mkdir(parents=True); A.OLD_INSTALL_EXE.write_bytes(b"MZ")
for lnk in (desk / "YT Archiver.lnk", progs / "YT Archiver.lnk"): lnk.write_text("old")
(desk / "YT Downloader.lnk").write_text("keep"); (desk / "YT Archiver.exe").write_bytes(b"MZ")
mine = {str(desk / "YT Archiver.lnk")}          # the Start-menu one points somewhere else
said = []
left = A.remove_old_install(said.append, desktop=desk, programs=progs,
                            is_ours=lambda lnk, exe: str(lnk) in mine and exe == A.OLD_INSTALL_EXE)
assert left == [] and not (desk / "YT Archiver.lnk").exists() and (progs / "YT Archiver.lnk").exists()
assert not A.OLD_INSTALL_EXE.exists() and not A.OLD_INSTALL_DIR.exists() and not (desk / "YT Archiver.exe").exists()
assert (desk / "YT Downloader.lnk").exists()
print("old install removed; YT Downloader and unrelated shortcuts untouched:", said)

# 6) old folder not empty (user files): folder stays
A.OLD_INSTALL_DIR.mkdir(); (A.OLD_INSTALL_DIR / "notes.txt").write_text("mine"); A.OLD_INSTALL_EXE.write_bytes(b"MZ")
A.remove_old_install(desktop=desk, programs=progs, is_ours=lambda l, e: False)
assert (A.OLD_INSTALL_DIR / "notes.txt").exists() and not A.OLD_INSTALL_EXE.exists()

# 7) builder's install step runs the cleanup + move first (non-Windows path)
import build as B
shutil.rmtree(A.DATA_DIR); (A.OLD_DATA_DIR / "settings.json").write_text('{"ia_prefix": "fromold"}')
B.remove_old_install = lambda say: []
out = []; B.say = out.append
exe = local / "built.exe"; exe.write_bytes(b"MZ" * 1000)
B.install(exe)
assert A.INSTALL_EXE.read_bytes() == exe.read_bytes() and A.load_settings()["ia_prefix"] == "fromold"
assert any("from YT Archiver" in o for o in out) and any("Installing to" in o and "Video Archiver" in o for o in out)
print("builder: old install removed, data moved, installed as", A.INSTALL_EXE.relative_to(local))
print("ALL RENAME TESTS PASSED")
