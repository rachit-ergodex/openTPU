"""Live RTL + Ramulator HBM2 reference backend (experimental, uncalibrated).

Use run() with existing compiled programs, or `python -m opentpu.hbmsim --target both`
for a small numerical correctness/clock comparison. This is not yet the fast
architectural simulator and the demonstration is not a full-model TPS benchmark.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

from .hbm_config import HBMConfig, RAMULATOR_COMMIT, SCALESIM_COMMIT

ROOT = Path(__file__).resolve().parent.parent


def ramulator_root():
    path = Path(os.environ.get("RAMULATOR_ROOT", ROOT / "build" / "ramulator2")).resolve()
    if not (path / "libramulator.so").exists():
        raise RuntimeError(f"Ramulator library missing in {path}; see docs/hbm_sim.md")
    sha = subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True).strip()
    if sha != RAMULATOR_COMMIT:
        raise RuntimeError(f"Ramulator revision {sha} is unsupported; expected {RAMULATOR_COMMIT}")
    return path


def build_bridge():
    root = ramulator_root()
    source = ROOT / "sim" / "ramulator" / "hbm_bridge.cpp"
    compiler = os.environ.get("CXX", "g++")
    digest = hashlib.sha256(source.read_bytes() + (root / "libramulator.so").read_bytes()
                            + str(root).encode() + compiler.encode()).hexdigest()[:16]
    out = ROOT / "build" / "ramulator_bridge" / digest
    lib = out / "libotpu_hbm.so"
    if lib.exists():
        return lib
    out.mkdir(parents=True, exist_ok=True)
    # Publish atomically: concurrent tests must not load a half-written shared library.
    with tempfile.TemporaryDirectory(dir=out) as tmp:
        target = Path(tmp) / lib.name
        subprocess.run([compiler, "-std=c++20", "-O2", "-shared", "-fPIC",
                        str(source), "-I" + str(root / "src"),
                        "-I" + str(root / "ext" / "fmt" / "include"),
                        "-L" + str(root), "-lramulator", "-Wl,-rpath," + str(root),
                        "-o", str(target)], check=True)
        target.replace(lib)
    return lib


def bridge_flags():
    lib = build_bridge()
    return ["+define+OTPU_RAMULATOR", "-CFLAGS", "-std=c++20", "-LDFLAGS",
            shlex.quote(str(lib)) + " " + shlex.quote("-Wl,-rpath," + str(lib.parent))]


def write_config(hbm, cfg, path, dram_bytes=None):
    root = ramulator_root()
    sys.path.insert(0, str(root / "python"))
    try:
        config = hbm.ramulator_config()
        from ramulator.dram.hbm2 import HBM2
        timing = dict(zip(HBM2.timing_params,
                          config["memory_system"]["controllers"][0]["dram"]["timing"]))
    finally:
        sys.path.pop(0)
    size = dram_bytes or cfg.DRAM_BYTES
    if size % 32 or cfg.S * size > hbm.capacity_bytes:
        raise ValueError("HBM slice storage must be 32-byte aligned and fit enabled PCs")
    config["opentpu"] = dict(hbm.to_dict(), slices=cfg.S, slice_bytes=size,
                             write_tail_ps=(timing["nCWL"] + timing["nBL"]) * timing["tCK_ps"])
    Path(path).write_text(json.dumps(config, indent=2) + "\n")


def run(cfg, programs, images, *, hbm=None, **kwargs):
    """Same state/statistics return convention as rtlsim.run()."""
    from . import rtlsim
    if kwargs.get("axi") or kwargs.get("ldc"):
        raise ValueError("HBM and AXI/LDC are mutually exclusive")
    kwargs.update(axi=False, ldc=0)
    return rtlsim.run(cfg, programs, images, hbm=hbm or HBMConfig(), **kwargs)


def smoke_workload():
    """LD -> QACT -> MM -> ST; actual ISA encodings and deterministic data."""
    import numpy as np
    from . import isa as I
    from .isasim import Config
    cfg = Config(S=1, D=128, MCOLS=4, ACT_BLOCKS=4, LANES=4,
                 TMEM_WORDS=1024, IMEM_WORDS=256, DRAM_BYTES=16384)
    rng = np.random.default_rng(7)
    img = np.zeros(cfg.DRAM_BYTES, np.uint8)
    img[:512] = rng.standard_normal(128).astype(np.float32).view(np.uint8)
    img[4096:8192] = rng.integers(-127, 128, 4096).astype(np.int8).view(np.uint8)
    img[8192:8320] = np.ones(32, np.float32).view(np.uint8)
    program = [I.ld(0, 0, 128), I.qact(0, 1, 0, 1, 128),
               I.mm(4096, 8192, 256, 32, 1, 128, 32, 1, 0, 4),
               I.st(12288, 256, 32), I.halt()]
    return cfg, [program], [img]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=("f2", "asic", "both"), default="both")
    parser.add_argument("--active-pcs", type=int, default=32)
    parser.add_argument("--queue-depth", type=int, default=32)
    parser.add_argument("--output", type=Path)
    opts = parser.parse_args()
    import numpy as np
    from .isasim import Machine
    cfg, programs, images = smoke_workload()
    golden = Machine(cfg, programs, images)
    golden.run()
    results = []
    for target in (["f2", "asic"] if opts.target == "both" else [opts.target]):
        hbm = HBMConfig.for_target(target, active_pseudochannels=opts.active_pcs,
                                   queue_depth=opts.queue_depth)
        drams, tmems, stats = run(cfg, programs, images, hbm=hbm, max_cycles=100000)
        for s, want in enumerate(golden.slices):
            np.testing.assert_array_equal(drams[s], want.dram)
            np.testing.assert_array_equal(tmems[s], want.tmem)
        stats.update(workload="synthetic LD-QACT-MM-ST, not full-model inference",
                     bit_exact=True, ramulator_commit=RAMULATOR_COMMIT,
                     scalesim_reference_commit=SCALESIM_COMMIT)
        results.append(stats)
    report = json.dumps(results, indent=2) + "\n"
    if opts.output:
        opts.output.parent.mkdir(parents=True, exist_ok=True)
        opts.output.write_text(report)
    print(report, end="")


if __name__ == "__main__":
    main()
