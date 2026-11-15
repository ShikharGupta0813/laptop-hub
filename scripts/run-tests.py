"""Run the test suite module by module, a few at a time, each with its own timeout.

A single `unittest discover` run can look hung on Windows, so every module is its own child process.

    python scripts/run-tests.py                  # every tests/test_*.py, 4 at a time
    python scripts/run-tests.py test_notes test_auth
    python scripts/run-tests.py --workers 1 --timeout 600

Prints one line per module and a summary. Exit code is 1 if any module failed or timed out.
"""
import argparse
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def run_module(name: str, timeout: int) -> tuple[str, str, float, str]:
    started = time.monotonic()
    try:
        proc = subprocess.run([sys.executable, "-m", "unittest", f"tests.{name}"], cwd=ROOT, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=timeout,
                              stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired:
        return name, "TIMEOUT", time.monotonic() - started, f"no result after {timeout}s"
    took = time.monotonic() - started
    out = (proc.stderr or "") + (proc.stdout or "")
    ran = re.search(r"^Ran (\d+) tests?", out, re.M)
    last = [line for line in out.strip().splitlines() if line.strip()][-1:] or [""]
    detail = f"{ran.group(1)} tests, {last[0]}" if ran else last[0]
    return name, "ok" if proc.returncode == 0 else "FAIL", took, detail


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("modules", nargs="*", help="module names (default: all tests/test_*.py)")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=420, help="seconds allowed per module")
    args = parser.parse_args()
    names = [m.removeprefix("tests.").removesuffix(".py") for m in args.modules] or \
        sorted(p.stem for p in (ROOT / "tests").glob("test_*.py"))
    started = time.monotonic()
    results = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        for result in pool.map(lambda n: run_module(n, args.timeout), names):
            results.append(result)
            print(f"{result[1]:7} {result[2]:6.1f}s  {result[0]}  {result[3] if result[1] != 'ok' else ''}", flush=True)
    bad = [r for r in results if r[1] != "ok"]
    print(f"\n{len(results) - len(bad)}/{len(results)} modules passed in {time.monotonic() - started:.0f}s")
    for r in bad:
        print(f"  {r[1]}: {r[0]}  ({r[3]})")
    slow = sorted(results, key=lambda r: -r[2])[:5]
    print("slowest: " + ", ".join(f"{r[0]} {r[2]:.0f}s" for r in slow))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
