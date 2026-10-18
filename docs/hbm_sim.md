# OpenTPU HBM2 cycle simulation — first development milestone

Status: experimental RTL reference backend. This executes the real OpenTPU
sequencer, MXU, VPU, quantizer, DMA, SRAM arbitration and collectives in Verilator,
coupled live to Ramulator. No FPGA synthesis is needed. It is **not yet the fast
architectural simulator**, and it is **not calibrated against F2 hardware**.

The F2 profile uses a **200 MHz core**, as specified for this project. The ASIC
profile uses a **1 GHz core target**, with the same HBM and RTL pipeline configuration
for a controlled comparison. Neither profile establishes achievable silicon timing.

## Source references

OpenTPU baseline: `ee8f118dc98f113860fdc1b9b62adb7d4c1c0b7b`.

SCALE-Sim v3 source was inspected, not just the paper. Reference checkout:
[scalesim-project/scale-sim-v3 at f79d7bc](https://github.com/scalesim-project/scale-sim-v3/tree/f79d7bc39a80a21fc35bc667967b76a15b311f49).

| Source file | How it informs this implementation |
|---|---|
| `README_ramulator.md` | Establishes the existing demand-trace / Ramulator / latency-replay workflow. Our backend instead injects requests online. |
| `scalesim/memory/read_port.py` | Finite read queues and memory completion delays; the bridge must propagate backpressure. |
| `scalesim/memory/write_port.py` | Finite write queues and completion accounting; HALT must wait for writes. |
| `scalesim/memory/double_buffered_scratchpad_mem.py` | Operand stalls delay subsequent demand. Overlapping waits must not simply be added together. |
| `scalesim/memory/read_buffer.py` | Distinguishes active storage and prefetch capacity; use the actual RTL FIFO in the reference backend. |
| `scalesim/compute/systolic_compute_ws.py` | Separates prefetch, demand and array utilization. Its generic weight-stationary mapping is not assumed to equal OpenTPU's stationary activations / streamed weights. |

No SCALE-Sim source is copied or vendored. Its MIT license applies to any future
code reuse; retain its copyright and license if code is adapted.

Ramulator is pinned to
[`c6c7a5de0cef35935d6fad7369e6271406798865`](https://github.com/CMU-SAFARI/ramulator2/tree/c6c7a5de0cef35935d6fad7369e6271406798865)
(2.1, in the `ramulator2` repository). API references: `ExternalFrontEnd`,
`GenericDRAMSystem`, `HBM12Controller`, `controller_base.cpp`, and the HBM2 Python
configuration DSL. This revision supports callbacks for reads and writes. Write
callbacks occur at command issue (or coalescing); the bridge adds nCWL+nBL before
exposing completion. Do not substitute a Ramulator version that silently omits
write callbacks.

## Build and run

Requires Python >=3.10, numpy, PyYAML, a C++20 compiler, CMake, make, Git and
Verilator 5. Ramulator is optional; ordinary OpenTPU builds do not link it.

```bash
python -m pip install numpy pyyaml pytest
python tools/hbm/build_ramulator.py --jobs 4
python -m opentpu.hbmsim --target both --output build/hbm-smoke.json
python -m pytest -q tests/test_hbm_sim.py
```

Use `RAMULATOR_ROOT=/absolute/path/to/ramulator2` for an existing pinned build.
The setup helper does not reset or overwrite existing checkouts. CMake fetches
Ramulator's upstream fmt/yaml-cpp dependencies. The Python DSL is loaded directly
from that checkout; the native Python bindings are not required.

The command executes LD -> QACT -> MM -> ST using actual instruction encodings,
compares every DRAM and TMEM byte/word against the functional simulator, then
reports core cycles, device seconds, target configuration and per-PC transactions.
This tiny synthetic workload is a correctness check, **not Qwen/Gemma TPS**.

Initial validation with the pinned sources, 32 active PCs and default queue depths:

| Target | Core clock | Core cycles | Device time | Numerical check |
|---|---:|---:|---:|---|
| F2 exploration profile | 200 MHz | 264 | 1.320 microseconds | Bit-exact |
| ASIC exploration profile | 1 GHz | 301 | 0.301 microseconds | Bit-exact |

The faster core needs more cycles because memory latency is held fixed in physical
time. This small kernel shows about 4.39x speedup; it is not an inference-throughput
prediction. `tests/test_hbm_sim.py`, `tests/test_rtlsim_build.py` and
`tests/test_target.py` pass together (28 tests). Coverage includes finite queues,
ordered tags, partial writes, clock independence, PC striping, slice isolation,
and two-slice QST/GATHER execution against the ISA simulator.

The pip Verilator 5.48 wheel used during development lacked configured C++20/PCH
make flags. For that wheel only, put its `bin` directory on PATH and run with:
`MAKEFLAGS='CFG_CXXFLAGS_PCH_I=-include CFG_CXXFLAGS_STD=-std=c++20'`.
A normally configured Verilator installation should not require this workaround.

Existing compiler outputs can run unchanged:

```python
from opentpu import hbmsim
from opentpu.hbm_config import HBMConfig

# cfg, programs and images are the existing compiler/ISA inputs.
drams, tmems, stats = hbmsim.run(
    cfg, programs, images,
    hbm=HBMConfig.for_target("f2", active_pseudochannels=32),
    trace=True,
)
```

`stats['trace']` retains the existing RTL trace format for Lens integration.
Use `HBMConfig.for_target('asic')` for 1 GHz. Set `active_pseudochannels` to
2, 4, 8, 16 or 32 to examine connectivity. Architecture changes still use the
existing `Config` and `uarch` parameters; they are not inferred from a clock label.

## Timing contract

- Three logical memory ports per slice: A, B and SW. Finite queue capacity is in
  **logical requests**, not bytes; chunk requests split into 32-byte DRAM transactions.
- All slices share one HBM timing instance. Each slice retains its ISA-private
  memory address space. Slice offsets are included in channel mapping.
- Stripe 32-byte transactions across enabled PCs, then columns, bank hierarchy
  and rows. This is an explicit exploration mapping, not a claim about the F2 build.
- Model 16 physical HBM channels, each with two pseudochannels; **32 total PCs**.
  `HBM2_4Gb` supplies 512 MiB per PC (16 GiB aggregate). Restricting active PCs also
  restricts addressable capacity in this mapping. Out-of-range accesses fail.
- F2 and ASIC profiles both use the upstream `HBM2_2000Mbps` timing preset,
  FR-FCFS, open rows and all-bank refresh. Several upstream timing parameters
  are explicitly estimates. Controller queues default to 32 reads / 32 writes
  **per physical channel**, shared by its two PCs.
- The independent 450 MHz interface admits at most one 32-byte transaction per
  PC per interface edge: 460.8 decimal GB/s aggregate ceiling. Read returns also
  obey a 32-byte/PC/interface-cycle bandwidth limit. This cap does not assert
  F2's exact DRAM timing matches the chosen 2000 Mbps preset.
- Core ports remain bottlenecks: a single D=128 B port can admit at most 128 bytes
  per core cycle (25.6 GB/s at 200 MHz), before stalls. Enabling 32 PCs does not
  imply that a single slice can saturate aggregate HBM bandwidth.
- Core, interface and DRAM events use physical time (integer femtoseconds).
  Interface edges derive from an integer tick count, avoiding accumulated ratio
  rounding. The 200 MHz core period is 5 ns; the ASIC period is 1 ns. Ramulator
  ticks retain the same physical period for both.
- Requests are admitted at core rising edges. At the following falling edge the
  bridge advances one core interval and drives responses for the next rising
  edge. Same-time DRAM completions precede new interface requests. Multiple SV
  instances advance the same timestamp idempotently.
- Read responses retire in order **per port**, retaining B's requester tag.
  HALT/read-idle/write-idle reflect pending operations. Rejected Ramulator sends
  remain queued and retry; no requests disappear on backpressure.
- Shadow data is sampled/updated on logical admission; this represents ideal
  store forwarding for functional validation. The RTL scoreboard controls data
  hazards. Physical write timing is tracked separately. Partial writes charge
  whole DRAM transactions but do not model a controller-specific read/modify/write.

## Accuracy boundaries and remaining work

This milestone uses **actual core RTL timing**, but an **abstract memory adapter**.
It bypasses the AWS AXI shell and does not yet model AXI burst grouping, IDs,
crossbar topology, controller-specific buffering, explicit CDC pipeline latency,
PCIe/host time, or the actual F2 channel-to-slice wiring. No performance result
should be labelled a measured or calibrated F2 result.

The core's existing per-slice 32-bit address limit remains. Full memory images and
RTL state dumps can be expensive; this first implementation is intended for small
kernel/representative-layer validation. There is no claim of fast full-model runs.

Host pokes and repeated runs are explicitly rejected by the HBM mode for now.
HBM is mutually exclusive with the existing AXI/LiteDRAM models. Production RTL
selects none of this code unless `OTPU_RAMULATOR` is defined and `AXI=3` is chosen.

Next milestones:

1. Correlate actual F2 AXI widths, outstanding limits, CDC delays and channel mapping
   using the working 200 MHz image and memory microbenchmarks.
2. Add compact trace/state fixtures for representative MLP and attention layers;
   compare event timestamps and stall counters, not only numerical outputs.
3. Build the faster OpenTPU instruction/resource model, using SCALE-Sim's demand
   separation and this live RTL/HBM backend as the timing reference. Validate it
   across backpressure, partial tiles, overlapping instructions and multiple slices.
4. Run Qwen3-0.6B / Gemma-3 1B prefill and decode at specified contexts; report
   cycles/token, device TTFT, TPS, per-PC utilization and stall causes. Report
   host-inclusive TTFT separately. No blanket 5x conversion from FPGA to ASIC.
