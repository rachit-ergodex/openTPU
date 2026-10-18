// Live OpenTPU/Ramulator bridge. No copied SCALE-Sim code; see docs/hbm_sim.md.
// C ABI is shared by the Verilator DPI wrapper and standalone integration tests.
#include <algorithm>
#include <array>
#include <cstdint>
#include <deque>
#include <fstream>
#include <iostream>
#include <memory>
#include <stdexcept>
#include <string>
#include <vector>
#include "ramulator/base/config.h"
#include "ramulator/base/factory.h"
#include "ramulator/frontend/i_frontend.h"
#include "ramulator/memory_system/i_memory_system.h"

namespace {
using u64 = uint64_t;
struct Request {
  int sid, port, tag;
  bool write;
  u64 address, next, end, remaining = 0, visible_fs = 0;
  std::vector<uint32_t> data;
};
struct Bridge {
  std::unique_ptr<Ramulator::IFrontEnd> frontend;
  std::unique_ptr<Ramulator::IMemorySystem> memory;
  int core_mhz, interface_mhz, pcs, depth, slices;
  u64 bytes, now_fs = 0, last_core_cycle = 0, mem_tick = 1, ui_tick = 1;
  u64 tck_fs, write_tail_fs;
  std::vector<std::vector<uint8_t>> shadow;
  std::vector<std::deque<std::shared_ptr<Request>>> queues;
  std::array<u64, 32> return_free_fs{}, reads{}, writes{}, rejected{};
  size_t round_robin = 0;

  explicit Bridge(const char* file) {
    auto cfg = Ramulator::Config::parse_config_file(file);
    auto p = cfg["opentpu"];
    core_mhz = p["core_mhz"].as<int>();
    interface_mhz = p["interface_mhz"].as<int>();
    pcs = p["active_pseudochannels"].as<int>();
    depth = p["queue_depth"].as<int>();
    slices = p["slices"].as<int>();
    bytes = p["slice_bytes"].as<u64>();
    write_tail_fs = p["write_tail_ps"].as<u64>() * 1000;
    if (core_mhz <= 0 || interface_mhz <= 0 || depth <= 0 || slices <= 0 ||
        pcs < 2 || pcs > 32 || (pcs & (pcs - 1)) || bytes == 0 ||
        bytes > (1ULL << 32) || bytes % 32 || slices * bytes > u64(pcs) * (512ULL << 20))
      throw std::runtime_error("invalid OpenTPU HBM bridge configuration");
    frontend.reset(Ramulator::Factory::create_frontend(cfg));
    memory.reset(Ramulator::Factory::create_memory_system(cfg));
    frontend->connect_memory_system(memory.get());
    memory->connect_frontend(frontend.get());
    tck_fs = u64(memory->get_tCK() * 1000000.0 + 0.5); // ns -> fs
    if (!tck_fs || memory->get_tx_bytes() != 32)
      throw std::runtime_error("HBM2 bridge requires 32-byte DRAM transactions");
    shadow.resize(slices);
    queues.resize(slices * 3);
  }

  void check_port(int sid, int port) const {
    if (sid < 0 || sid >= slices || port < 0 || port > 2)
      throw std::runtime_error("invalid HBM slice/port");
  }
  bool ready(int sid, int port) const {
    check_port(sid, port);
    return queues[sid * 3 + port].size() < size_t(depth);
  }
  // Byte-striped across enabled PCs. Translate into CacheLineInterleave(16 channels)
  // + RoBaRaCoCh (column, PC, sid, bankgroup, bank, row) without aliasing.
  u64 map(u64 logical, int& pc) const {
    const u64 tx = logical / 32;
    pc = tx % pcs;
    const u64 local = tx / pcs;
    const u64 intra_tx = ((local / 32) * 2 + pc % 2) * 32 + local % 32;
    return (intra_tx * 16 + pc / 2) * 32;
  }
  int submit(int sid, int port, u64 addr, int nbytes, int write, int tag,
             const uint32_t* data, const uint32_t* byte_mask) {
    check_port(sid, port);
    if (nbytes <= 0 || nbytes > 4096 || nbytes % 4 || addr % 4 ||
        addr > bytes || u64(nbytes) > bytes - addr)
      throw std::runtime_error("HBM request outside slice memory or invalid alignment/size");
    if (!ready(sid, port)) return 0;
    auto r = std::make_shared<Request>();
    *r = {sid, port, tag, bool(write), addr, addr / 32 * 32,
          (addr + nbytes + 31) / 32 * 32, 0, 0, {}};
    r->data.resize(nbytes / 4);
    auto& image = shadow[sid];
    if (image.size() < addr + nbytes) image.resize(addr + nbytes, 0);
    for (int i = 0; i < nbytes; ++i) {
      if (write) {
        if ((byte_mask[i / 32] >> (i % 32)) & 1)
          image[addr + i] = (data[i / 4] >> (8 * (i % 4))) & 255;
      } else {
        r->data[i / 4] |= uint32_t(image[addr + i]) << (8 * (i % 4));
      }
    }
    queues[sid * 3 + port].push_back(r);
    return 1;
  }
  bool done(const Request& r) const {
    return r.next == r.end && r.remaining == 0 && now_fs >= r.visible_fs;
  }
  void retire_writes() {
    for (auto& q : queues)
      while (!q.empty() && q.front()->write && done(*q.front())) q.pop_front();
  }
  void issue() {
    std::array<bool, 32> used{};
    // Fixed round-robin across slice ports, not dependent on Verilator process order.
    for (size_t j = 0; j < queues.size(); ++j) {
      auto& q = queues[(round_robin + j) % queues.size()];
      for (auto& r : q) {
        while (r->next < r->end) {
          int pc;
          u64 physical = map(u64(r->sid) * bytes + r->next, pc);
          if (used[pc]) break;
          ++r->remaining; // callbacks may be synchronous for coalesced writes
          bool accepted = frontend->receive_external_requests(
              r->write ? 1 : 0, physical, r->sid,
              [this, r, pc](Ramulator::Request&) {
                --r->remaining;
                u64 visible = now_fs + (r->write ? write_tail_fs : 0);
                if (!r->write) {
                  // Return path has the same 32B/PC/interface-cycle data width.
                  visible = std::max(visible, return_free_fs[pc]);
                  visible += (1000000000ULL + interface_mhz - 1) / interface_mhz;
                  return_free_fs[pc] = visible;
                }
                r->visible_fs = std::max(r->visible_fs, visible);
              }, 32);
          if (!accepted) { --r->remaining; ++rejected[pc]; break; }
          used[pc] = true;
          (r->write ? writes[pc] : reads[pc])++;
          r->next += 32;
        }
        if (r->next < r->end) break;
      }
    }
    round_robin = (round_robin + 1) % queues.size();
  }
  void advance(u64 core_cycle) {
    if (core_cycle < last_core_cycle) throw std::runtime_error("HBM time moved backwards");
    last_core_cycle = core_cycle;
    const u64 target = core_cycle * 1000000000ULL / core_mhz;
    for (;;) {
      const u64 mt = mem_tick * tck_fs;
      const u64 ut = ui_tick * 1000000000ULL / interface_mhz;
      now_fs = std::min(mt, ut);
      if (now_fs > target) break;
      // At coincident edges, complete DRAM work before issuing new interface work.
      if (mt <= ut) { memory->tick(); ++mem_tick; }
      if (ut <= mt) { issue(); ++ui_tick; }
      retire_writes();
    }
    now_fs = target;
    retire_writes();
  }
  int poll(int sid, int port, uint32_t* data, int* tag) {
    check_port(sid, port);
    auto& q = queues[sid * 3 + port];
    if (q.empty() || q.front()->write || !done(*q.front())) return 0;
    auto r = q.front(); q.pop_front();
    std::copy(r->data.begin(), r->data.end(), data);
    *tag = r->tag;
    return 1;
  }
  bool idle(int sid, bool writes_only) const {
    check_port(sid, 0);
    for (int p = 0; p < 3; ++p)
      for (auto& r : queues[sid * 3 + p])
        if (r->write == writes_only) return false;
    return true;
  }
  void load(int sid, const char* file) {
    check_port(sid, 0);
    std::ifstream in(file, std::ios::binary);
    if (!in) throw std::runtime_error("cannot open HBM input image");
    auto& v = shadow[sid];
    v.assign(std::istreambuf_iterator<char>(in), {});
    if (v.size() > bytes || v.size() % 4) throw std::runtime_error("invalid HBM image size");
    for (size_t i = 0; i < v.size(); i += 4) std::reverse(v.begin()+i, v.begin()+i+4);
  }
  void dump(int sid, const char* file) {
    check_port(sid, 0);
    if (!idle(sid, true) || !idle(sid, false))
      throw std::runtime_error("HBM dump with requests still outstanding");
    std::ofstream out(file, std::ios::binary);
    auto& v = shadow[sid];
    out.write(reinterpret_cast<const char*>(v.data()), v.size());
    std::array<char, 4096> zero{};
    for (u64 i = v.size(); i < bytes;) {
      u64 n = std::min<u64>(zero.size(), bytes-i);
      out.write(zero.data(), n); i += n;
    }
    if (!out) throw std::runtime_error("HBM dump write failed");
  }
};
std::unique_ptr<Bridge> bridge;
std::string error;
template <class F> int checked(F f) {
  try { error.clear(); return f(); }
  catch (const std::exception& e) { error = e.what(); return -1; }
}
}
extern "C" {
const char* otpu_hbm_error() { return error.c_str(); }
int otpu_hbm_init(const char* path) { return checked([&] {
  if (!bridge) bridge = std::make_unique<Bridge>(path); return 0; }); }
void otpu_hbm_close() { bridge.reset(); }
int otpu_hbm_load(int sid, const char* path) { return checked([&] { bridge->load(sid,path); return 0; }); }
int otpu_hbm_dump(int sid, const char* path) { return checked([&] { bridge->dump(sid,path); return 0; }); }
int otpu_hbm_ready(int sid, int port) { return checked([&] { return int(bridge->ready(sid,port)); }); }
int otpu_hbm_submit(int sid, int port, u64 addr, int size, int write, int tag,
                    const uint32_t* data, const uint32_t* mask) {
  return checked([&] { return bridge->submit(sid,port,addr,size,write,tag,data,mask); });
}
int otpu_hbm_advance(u64 cycle) { return checked([&] { bridge->advance(cycle); return 0; }); }
int otpu_hbm_poll(int sid, int port, uint32_t* data, int* tag) {
  return checked([&] { return bridge->poll(sid,port,data,tag); });
}
int otpu_hbm_idle(int sid, int writes) { return checked([&] { return int(bridge->idle(sid,writes)); }); }
u64 otpu_hbm_count(int pc, int kind) {
  if (pc < 0 || pc >= 32 || !bridge) return 0;
  return kind == 0 ? bridge->reads[pc] : kind == 1 ? bridge->writes[pc] : bridge->rejected[pc];
}
}
