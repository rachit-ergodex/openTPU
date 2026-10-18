"""Live Ramulator contract tests and bit-exact RTL checks (no FPGA synthesis)."""
import ctypes as C
import os
from pathlib import Path

import numpy as np
import pytest

from opentpu.hbm_config import HBMConfig
from opentpu import hbmsim
from opentpu.isasim import Config, Machine


def test_target_clocks_and_capacity():
    assert HBMConfig.for_target("f2").core_mhz == 200
    assert HBMConfig.for_target("asic").core_mhz == 1000
    assert HBMConfig().capacity_bytes == 16 << 30
    for kw in ({"core_mhz": 0}, {"queue_depth": 0}, {"active_pseudochannels": 3},
               {"active_pseudochannels": 2.0}):
        with pytest.raises(ValueError):
            HBMConfig(**kw)


@pytest.fixture
def ramulator_available():
    root = Path(os.environ.get("RAMULATOR_ROOT", "build/ramulator2"))
    if not (root / "libramulator.so").is_file():
        pytest.skip("pinned Ramulator library not built; see docs/hbm_sim.md")


class Bridge:
    def __init__(self, path, hbm, cfg):
        hbmsim.write_config(hbm, cfg, path)
        self.lib = C.CDLL(str(hbmsim.build_bridge()))
        self.lib.otpu_hbm_error.restype = C.c_char_p
        self.lib.otpu_hbm_init.argtypes = [C.c_char_p]
        self.lib.otpu_hbm_advance.argtypes = [C.c_uint64]
        self.lib.otpu_hbm_submit.argtypes = [C.c_int, C.c_int, C.c_uint64, C.c_int, C.c_int,
                                            C.c_int, C.POINTER(C.c_uint32), C.POINTER(C.c_uint32)]
        self.lib.otpu_hbm_poll.argtypes = [C.c_int, C.c_int, C.POINTER(C.c_uint32), C.POINTER(C.c_int)]
        self.lib.otpu_hbm_count.restype = C.c_uint64
        self.lib.otpu_hbm_close()
        assert self.lib.otpu_hbm_init(str(path).encode()) == 0, self.lib.otpu_hbm_error()
        self.cycle = 0

    def submit(self, address=0, size=32, sid=0, port=1, write=False, tag=0, data=None, mask=None):
        data = (C.c_uint32 * (size // 4))(*(data or [0] * (size // 4)))
        mask = (C.c_uint32 * ((size + 31) // 32))(*(mask or [0xFFFFFFFF] * ((size + 31) // 32)))
        return self.lib.otpu_hbm_submit(sid, port, address, size, int(write), tag, data, mask)

    def tick(self):
        self.cycle += 1
        assert self.lib.otpu_hbm_advance(self.cycle) == 0, self.lib.otpu_hbm_error()

    def poll(self, sid=0, port=1):
        buf, tag = (C.c_uint32 * 1024)(), C.c_int()
        rc = self.lib.otpu_hbm_poll(sid, port, buf, C.byref(tag))
        assert rc >= 0, self.lib.otpu_hbm_error()
        return (tag.value, list(buf)) if rc else None

    def wait(self, count=1, sid=0, port=1):
        replies = []
        for _ in range(10000):
            self.tick()
            response = self.poll(sid, port)
            if response:
                replies.append(response)
            if len(replies) == count:
                return replies
        pytest.fail("HBM response timeout")


def test_queue_backpressure_order_and_masks(ramulator_available, tmp_path):
    b = Bridge(tmp_path / "hbm.json", HBMConfig(queue_depth=2), Config(DRAM_BYTES=4096))
    try:
        assert b.submit(write=True, data=[0x11223344] * 8, mask=[0x55555555]) == 1
        assert b.submit(tag=17) == 1
        assert b.submit(tag=18) == 0   # no silent overcommit
        result = b.wait()[0]
        assert result[0] == 17
        assert result[1][:8] == [0x00220044] * 8
        assert b.lib.otpu_hbm_idle(0, 1) == 1
        assert b.lib.otpu_hbm_idle(0, 0) == 1
        assert b.submit(address=32, tag=21) == 1
        assert b.submit(address=0, tag=22) == 1
        assert [r[0] for r in b.wait(2)] == [21, 22]
        assert b.submit(address=4096) == -1
        assert b.lib.otpu_hbm_advance(b.cycle - 1) == -1
    finally:
        b.lib.otpu_hbm_close()


def test_memory_time_is_not_rescaled_with_core_clock(ramulator_available, tmp_path):
    times = []
    for target in ("f2", "asic"):
        cfg = HBMConfig.for_target(target)
        b = Bridge(tmp_path / (target + ".json"), cfg, Config(DRAM_BYTES=4096))
        try:
            assert b.submit() == 1
            b.wait()
            times.append((b.cycle, b.cycle * 1000 / cfg.core_mhz))
        finally:
            b.lib.otpu_hbm_close()
    # Same cold-row DRAM latency in ns, rounded up to the next core edge.
    assert times[1][0] > times[0][0]
    assert abs(times[0][1] - times[1][1]) < 5.01


@pytest.mark.parametrize("pcs", [2, 32])
def test_pseudochannel_striping_and_slice_isolation(ramulator_available, tmp_path, pcs):
    b = Bridge(tmp_path / "hbm.json", HBMConfig(active_pseudochannels=pcs),
               Config(S=2, DRAM_BYTES=4096))
    try:
        assert b.submit(size=1024, sid=0, write=True, data=[0xDEADBEEF] * 256) == 1
        assert b.submit(size=1024, sid=1) == 1
        assert b.wait(sid=1)[0][1][:256] == [0] * 256
        for _ in range(10000):
            if b.lib.otpu_hbm_idle(0, 1):
                break
            b.tick()
        assert b.lib.otpu_hbm_idle(0, 1)
        assert [b.lib.otpu_hbm_count(pc, 0) for pc in range(pcs)] == [32 // pcs] * pcs
        assert [b.lib.otpu_hbm_count(pc, 1) for pc in range(pcs)] == [32 // pcs] * pcs
        assert all(b.lib.otpu_hbm_count(pc, 0) == 0 for pc in range(pcs, 32))
    finally:
        b.lib.otpu_hbm_close()


@pytest.mark.parametrize("target,depth,pcs", [("f2", 1, 2), ("f2", 32, 32), ("asic", 32, 32)])
def test_hbm_rtl_matches_isa(ramulator_available, have_verilator, target, depth, pcs):
    cfg, programs, images = hbmsim.smoke_workload()
    golden = Machine(cfg, programs, images).run()
    drams, tmems, stats = hbmsim.run(
        cfg, programs, images, hbm=HBMConfig.for_target(target, queue_depth=depth,
                                                      active_pseudochannels=pcs),
        max_cycles=100000)
    np.testing.assert_array_equal(drams[0], golden.slices[0].dram)
    np.testing.assert_array_equal(tmems[0], golden.slices[0].tmem)
    assert stats["device_seconds"] == stats["cycles"] / (stats["hbm"]["core_mhz"] * 1e6)
    assert sum(c["read_tx"] for c in stats["hbm_channels"]) > 0
    assert sum(c["write_tx"] for c in stats["hbm_channels"]) > 0


def test_unsupported_memory_combinations_fail():
    with pytest.raises(ValueError, match="mutually exclusive"):
        hbmsim.run(None, [], [], axi=True)


def test_two_slice_rtl_quantized_writes_and_collective(ramulator_available, have_verilator):
    from opentpu import isa as I
    cfg = Config(S=2, D=32, MCOLS=2, ACT_BLOCKS=2, LANES=4,
                 TMEM_WORDS=256, IMEM_WORDS=256, DRAM_BYTES=4096)
    images = []
    for sid in range(2):
        image = np.zeros(4096, np.uint8)
        image[:128] = (np.arange(32, dtype=np.float32) - sid * 7).view(np.uint8)
        images.append(image)
    program = [I.ld(0, 0, 32), I.qst(0, 512, 1024, 1, 1, 32, 32, 1),
               I.gather(0, 64, 1, 4, 4, 4, 4), I.st(2048, 64, 8),
               I.ld(512, 128, 8), I.halt()]
    programs = [program, program]
    golden = Machine(cfg, programs, images).run()
    drams, tmems, stats = hbmsim.run(
        cfg, programs, images, hbm=HBMConfig(queue_depth=1, active_pseudochannels=2),
        max_cycles=100000)
    for sid in range(2):
        np.testing.assert_array_equal(drams[sid], golden.slices[sid].dram)
        np.testing.assert_array_equal(tmems[sid], golden.slices[sid].tmem)
    assert len(stats["instructions"]) == 2
