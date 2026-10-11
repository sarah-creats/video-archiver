"""Run all tests: python tests/run_all.py   (each test runs in its own process)"""
import subprocess, sys
from pathlib import Path

here = Path(__file__).resolve().parent
failed = 0
for test in sorted(here.glob("test_*.py")):
    r = subprocess.run([sys.executable, str(test)], capture_output=True, text=True, timeout=600)
    last = (r.stdout.strip().splitlines() or ["(no output)"])[-1]
    ok = r.returncode == 0
    failed += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {test.name:22} {last if ok else (r.stderr.strip().splitlines() or [''])[-1]}")
print(f"\n{'All tests passed.' if not failed else f'{failed} test file(s) failed.'}")
sys.exit(1 if failed else 0)
