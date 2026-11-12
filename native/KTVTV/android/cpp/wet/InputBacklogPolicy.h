#pragma once
#include <algorithm>
#include <cstdint>
namespace fridaywet {
struct BacklogPlan {bool failed;int discardBlocks;};
// Protect only the observer/return path: target <=10 ms queued, fail above100 ms.
// At most 8 nonblocking 128-frame reads per loop; never drain the OEM dry path.
inline BacklogPlan backlogPlan(int64_t available){
    if(available>4800)return {true,0};
    return {false,int(std::clamp<int64_t>((available-480+127)/128,0,8))};
}
}
