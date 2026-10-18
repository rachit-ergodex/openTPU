#!/usr/bin/env python3
"""Build the pinned, optional Ramulator dependency without modifying an existing checkout."""
import argparse
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from opentpu.hbm_config import RAMULATOR_COMMIT


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--path", type=Path, default=ROOT / "build" / "ramulator2")
    p.add_argument("--jobs", type=int, default=4)
    args = p.parse_args()
    if args.jobs < 1:
        p.error("--jobs must be positive")
    root = args.path.resolve()
    if not root.exists():
        root.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--no-checkout", "https://github.com/CMU-SAFARI/ramulator2.git",
                        str(root)], check=True)
        subprocess.run(["git", "-C", str(root), "checkout", "--detach", RAMULATOR_COMMIT], check=True)
    sha = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    if sha != RAMULATOR_COMMIT:
        p.error(f"existing checkout is {sha}, expected {RAMULATOR_COMMIT}; choose a new --path")
    subprocess.run(["cmake", "-S", str(root), "-B", str(root / "build"),
                    "-DRAMULATOR_PYTHON_BINDINGS=OFF", "-DCMAKE_BUILD_TYPE=Release",
                    "-DCMAKE_POLICY_VERSION_MINIMUM=3.5"], check=True)
    subprocess.run(["cmake", "--build", str(root / "build"), "-j", str(args.jobs)], check=True)
    print(f"Ramulator built: {root}")
    if root != ROOT / "build" / "ramulator2":
        print(f"Set RAMULATOR_ROOT to {root}")


if __name__ == "__main__":
    main()
