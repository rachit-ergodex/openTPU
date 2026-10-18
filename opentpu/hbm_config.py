"""Experimental HBM2 timing profiles; clocks are physical, not scaled RTL cycles."""
from dataclasses import asdict, dataclass


RAMULATOR_COMMIT = "c6c7a5de0cef35935d6fad7369e6271406798865"
SCALESIM_COMMIT = "f79d7bc39a80a21fc35bc667967b76a15b311f49"


@dataclass(frozen=True)
class HBMConfig:
    target: str = "f2"
    core_mhz: int = 200
    interface_mhz: int = 450
    active_pseudochannels: int = 32
    queue_depth: int = 32       # logical requests per slice port (A, B, SW)
    controller_queue_depth: int = 32
    org_preset: str = "HBM2_4Gb"  # 512 MiB per PC, 32 PCs = 16 GiB
    timing_preset: str = "HBM2_2000Mbps"

    def __post_init__(self):
        for name in ("core_mhz", "interface_mhz", "queue_depth", "controller_queue_depth"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.core_mhz > 10000 or self.interface_mhz > 10000:
            raise ValueError("clock exceeds supported 10 GHz")
        if type(self.active_pseudochannels) is not int or self.active_pseudochannels not in (2, 4, 8, 16, 32):
            raise ValueError("active_pseudochannels must be 2, 4, 8, 16 or 32")
        if self.org_preset != "HBM2_4Gb":
            raise ValueError("this bridge currently supports HBM2_4Gb organization only")

    @classmethod
    def for_target(cls, target, **overrides):
        if target not in ("f2", "asic"):
            raise ValueError("target must be f2 or asic")
        return cls(**dict({"target": target, "core_mhz": 200 if target == "f2" else 1000},
                          **overrides))

    @property
    def capacity_bytes(self):
        return self.active_pseudochannels * (512 << 20)

    def to_dict(self):
        return asdict(self)

    def ramulator_config(self):
        """Requires the pinned Ramulator Python config DSL, not its native bindings."""
        import ramulator as r
        dram = r.dram.HBM2(org_preset=self.org_preset, timing_preset=self.timing_preset)
        ctrl = r.controller.HBM12(
            dram=dram, scheduler=r.scheduler.FRFCFS(), row_policy=r.row_policy.Open(),
            addr_mapper=r.addr_mapper.RoBaRaCoCh(),
            refresh_manager=r.refresh_manager.AllBank(),
            read_buffer_size=self.controller_queue_depth,
            write_buffer_size=self.controller_queue_depth)
        mem = r.memory_system.GenericDRAM(
            clock_ratio=1, controllers=[ctrl for _ in range(16)],
            channel_mapper=r.channel_mapper.CacheLineInterleave(interleave_bits=0))
        return {"frontend": r.frontend.External(clock_ratio=1).to_config(),
                "memory_system": mem.to_config()}
