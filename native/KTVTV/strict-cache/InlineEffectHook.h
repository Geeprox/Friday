#ifndef FRIDAY_INLINE_EFFECT_HOOK_H
#define FRIDAY_INLINE_EFFECT_HOOK_H
#include "InstanceCookie.h"
#include <cstdint>

namespace fridaystrict::inlinefx {
enum Flag : uint32_t { Installed=1,Prepared=2,Bound=4,Owned=8,Muted=16,Bypass=32,Live=64,NotchPending=128,OutputReady=256 };
enum Fault : int32_t { Ok=0,Identity=1,Protection=2,ForeignSlot=3,Caller=4,Reentry=5,DirtyInit=6,
    UnitLayout=7,ShadowInstall=8,Binding=9,FrameShape=10,NextUnit=11,RetiredCallback=12,ShuttingDown=13,
    OutputBinding=14,OutputWrite=15,OutputStall=16,OutputClock=17 };
struct Status {
    uint64_t requestedRevision=0,appliedRevision=0,callbacks=0,frames=0;
    int32_t profile=0,wet=0,monitorGain=0;
    uint32_t peak=0,flags=0;
    int32_t fault=0;
};
static_assert(sizeof(Status)==56,"fixed scalar inline metrics");
// Original driver-call completion only. The vendor normalizes nonnegative
// results to zero, so these fields never claim written bytes or physical sound.
struct OutputStatus {
    uint64_t generation=0,completedCalls=0,lastCompletionNs=0;
    int32_t lastRawResult=0,stickyRawError=0;
    uint32_t inFlight=0;
    bool byteCountVerified=false;
};
// The caller has already authenticated the complete mapped payload and its
// relocations. This adapter checks only two fixed, expected-value GOT targets.
bool install(uintptr_t payloadBase,MemoryRange rw,MemoryRange relro);
int prepareOwner(int card,int device);
// Called while the existing Source holds strategy, then Single, locks.
int bind(uintptr_t single,uintptr_t effect);
bool configure(int profile,int wet,int gain,bool muted,bool bypass);
// Closing always succeeds. Opening requires fresh original output completion;
// absence during the first-completion grace is a bounded retry, never an open.
bool ownerMute(bool muted);
bool notch(int slot,int hz,int cutDb);
// Revocation never restores the original effect path for an owned instance.
void revoke();
Status snapshot();
OutputStatus outputSnapshot();
bool live();
bool fault();
// Module-exit only: payload STOP/join must finish before this DSO is unloaded.
void shutdown();
// Call only after payload dlclose completed its original STOP/join. This never
// dereferences the now-unmapped payload; it clears our retained DSP storage.
void finishAfterPayloadStop();

#if defined(FRIDAY_INLINEFX_TEST)
namespace test {
struct Calls {
    void (*init)(void*,int,int,int,int)=nullptr;
    int (*effect)(void*,char*,int,int)=nullptr;
    int (*write)(void*,char*,int,int)=nullptr;
    int (*transfer)(void*,char*,int)=nullptr;
    uint64_t (*nowNs)()=nullptr;
};
void calls(Calls);
void control(bool hold);
void init(uintptr_t caller,void* single,int card,int device,int channels,int soft);
int stream(void* effect,char* pcm,int bytes,int flag);
int transfer(uintptr_t caller,void* alsa,char* pcm,int bytes);
void reset();
}
#endif
}
#endif
