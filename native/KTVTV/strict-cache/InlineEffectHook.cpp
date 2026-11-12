#include "InlineEffectHook.h"
#include "../android/cpp/wet/InlineVoiceKernel.h"
#include <array>
#include <atomic>
#include <cerrno>
#include <cstring>
#include <new>
#include <pthread.h>
#include <sys/mman.h>
#include <time.h>
#include <type_traits>
#include <unistd.h>

namespace fridaystrict::inlinefx {
namespace {
constexpr uintptr_t Manager=0x9dec0,Init=0x3a1c0,InitSlot=0x9c560,InitReturn=0x39f3c;
constexpr uintptr_t EffectTable=0x97c90,EffectPoint=0x97ca0,EffectStream=0x49f1c;
constexpr uintptr_t WritePoint=0x97e10,WriteStream=0x4c3fc;
constexpr uintptr_t Transfer=0x542c0,TransferSlot=0x9b698,TransferReturn=0x43474;
constexpr uintptr_t MtkPoint=0x97a20,AlsaPoint=0x981c8;
constexpr uint64_t OutputDeadlineNs=250000000;
constexpr std::array<uintptr_t,6> Members{{0x10,0x18,0x20,0x28,0x30,0x38}};
constexpr std::array<uintptr_t,6> UnitPoints{{0x97f30,0x97d30,WritePoint,0x97b80,EffectPoint,0x98010}};
constexpr std::array<uintptr_t,13> EffectEntries{{0x4a2fc,0x4a358,0x49cf4,EffectStream,0x4a268,
    0x39d80,0x39d9c,0x39db8,0x39e24,0x39e40,0x39e7c,0x39eb8,0x39ed8}};
using InitFn=void(*)(void*,int,int,int,int);
using StreamFn=int(*)(void*,char*,int,int);
using TransferFn=int(*)(void*,char*,int);
static_assert(std::atomic<uintptr_t>::is_always_lock_free&&std::atomic<uint32_t>::is_always_lock_free);
struct State {
    pthread_mutex_t control=PTHREAD_MUTEX_INITIALIZER;
    uintptr_t base=0;MemoryRange rw{},relro{};
    std::array<uintptr_t,15> shadow{}; // two ABI prefix words and all 13 slots
    std::atomic<bool> installed{false},closing{false},prepared{false},bound{false},owned{false},revoked{false},ownerZero{true};
    std::atomic<int32_t> error{0};
    std::atomic<uintptr_t> single{0},effect{0},write{0},volume{0},retired{0};
    std::array<std::atomic<uintptr_t>,6> units{};
    std::atomic<uint64_t> generation{0};std::atomic<uint32_t> inFlight{0};
    std::atomic<uint32_t> patches{0},outputInFlight{0};
    std::atomic<uintptr_t> adapter{0},alsa{0},pcm{0},transfer{0};
    std::atomic<uint64_t> outputStarted{0},outputCompleted{0},outputLast{0};
    std::atomic<int32_t> outputRaw{0},outputError{0};
    int card=-1,device=-1;
    fridaywet::InlineVoiceKernel kernel;
#if defined(FRIDAY_INLINEFX_TEST)
    test::Calls calls{};
#endif
};
// The wrapper's .fini_array and C++ atexit order span translation units. Do not
// register a State/Kernel destructor that could race the payload's audio join.
// Root explicitly clears this process-lifetime storage after payload shutdown.
alignas(State) unsigned char stateStorage[sizeof(State)];
State& state=*new(stateStorage)State;
struct Control {bool locked=false;explicit Control(bool attempt=false){locked=(attempt?pthread_mutex_trylock(&state.control):pthread_mutex_lock(&state.control))==0;}
    ~Control(){if(locked)pthread_mutex_unlock(&state.control);}};
template<class T>T load(uintptr_t p){T value;std::memcpy(&value,reinterpret_cast<const void*>(p),sizeof value);return value;}
uint32_t gainWord(uintptr_t p){return __atomic_load_n(reinterpret_cast<const uint32_t*>(p),__ATOMIC_ACQUIRE);}
bool pointer(uintptr_t p){return p>=4096&&(p&7)==0;}
bool codePointer(uintptr_t p){return p>=4096&&(p&3)==0;}
void fail(int32_t reason){int32_t expected=Ok;state.error.compare_exchange_strong(expected,reason,std::memory_order_acq_rel);state.ownerZero.store(true,std::memory_order_release);state.kernel.ownerMute(true);}
uintptr_t shadowPoint(){return uintptr_t(state.shadow.data()+2);}
InitFn originalInit(){
#if defined(FRIDAY_INLINEFX_TEST)
    if(state.calls.init)return state.calls.init;
#endif
    return reinterpret_cast<InitFn>(state.base+Init);
}
StreamFn originalEffect(){
#if defined(FRIDAY_INLINEFX_TEST)
    if(state.calls.effect)return state.calls.effect;
#endif
    return reinterpret_cast<StreamFn>(state.base+EffectStream);
}
StreamFn originalWrite(){
#if defined(FRIDAY_INLINEFX_TEST)
    if(state.calls.write)return state.calls.write;
#endif
    return reinterpret_cast<StreamFn>(state.base+WriteStream);
}
TransferFn originalTransfer(){
#if defined(FRIDAY_INLINEFX_TEST)
    if(state.calls.transfer)return state.calls.transfer;
#endif
    return reinterpret_cast<TransferFn>(state.base+Transfer);
}
uint64_t nowNs(){
#if defined(FRIDAY_INLINEFX_TEST)
    if(state.calls.nowNs)return state.calls.nowNs();
#endif
    timespec now{};if(clock_gettime(CLOCK_MONOTONIC,&now))return 0;
    return uint64_t(now.tv_sec)*1000000000ULL+uint64_t(now.tv_nsec);
}
void clearOutput(){
    state.adapter.store(0);state.alsa.store(0);state.pcm.store(0);state.transfer.store(0);
    state.outputStarted.store(0);state.outputLast.store(0);state.outputRaw.store(0);state.outputError.store(0);
    // completed calls stay monotonic across stopped generations.
}
bool outputReady(){
    if(!state.bound.load(std::memory_order_acquire)||!state.owned.load(std::memory_order_acquire)
        ||state.error.load(std::memory_order_acquire)!=Ok)return false;
    const auto started=state.outputStarted.load(std::memory_order_acquire),last=state.outputLast.load(std::memory_order_acquire),now=nowNs();
    if(!started||!now||now<started||(last&&now<last)){fail(OutputClock);return false;}
    if(now-(last?last:started)>OutputDeadlineNs){fail(OutputStall);return false;}
    return last!=0;
}
bool sameSingle(uintptr_t single,uintptr_t* strategyOut=nullptr){
    if(!state.base||!state.rw.contains(state.base+Manager,0x300))return false;
    uintptr_t strategy=load<uintptr_t>(state.base+Manager+0x2e8);
    if(!pointer(strategy)||load<uintptr_t>(strategy)!=state.base+0x976d8)return false;
    if(!pointer(single)||load<uintptr_t>(single)!=state.base+0x97608
        ||load<uintptr_t>(state.base+Manager+0x2f8)!=single||load<uintptr_t>(strategy+0x10)!=single)return false;
    if(strategyOut)*strategyOut=strategy;return true;
}
bool emptyUnits(uintptr_t single){for(auto offset:Members)if(load<uintptr_t>(single+offset))return false;return true;}
bool layout(uintptr_t single){
    std::array<uintptr_t,6> units{};
    for(size_t i=0;i<units.size();i++){
        units[i]=load<uintptr_t>(single+Members[i]);if(!pointer(units[i])||load<uintptr_t>(units[i])!=state.base+UnitPoints[i])return false;
        for(size_t j=0;j<i;j++)if(units[j]==units[i])return false;
        if(load<int32_t>(units[i]+0x18)!=2||load<int32_t>(units[i]+0x1c)!=0||load<int32_t>(units[i]+0x20)!=48000)return false;
    }
    return load<uintptr_t>(units[4]+8)==0&&load<uintptr_t>(units[4]+0x10)==0
        &&load<uintptr_t>(units[0]+0x88)==0
        &&gainWord(units[5]+0x1048)==0&&gainWord(units[5]+0x104c)==0;
}
struct Locks {uintptr_t strategy=0,single=0;
    ~Locks(){if(single)pthread_mutex_unlock(reinterpret_cast<pthread_mutex_t*>(single+0x50));if(strategy)pthread_mutex_unlock(reinterpret_cast<pthread_mutex_t*>(strategy+0x28));}
    bool takeStrategy(uintptr_t p){if(pthread_mutex_trylock(reinterpret_cast<pthread_mutex_t*>(p+0x28)))return false;strategy=p;return true;}
    bool takeSingle(uintptr_t p){if(pthread_mutex_trylock(reinterpret_cast<pthread_mutex_t*>(p+0x50)))return false;single=p;return true;}
};
enum class Slot { Initialize,Output };
uintptr_t slotOffset(Slot slot){return slot==Slot::Initialize?InitSlot:TransferSlot;}
bool replaceSlot(Slot slot,uintptr_t expected,uintptr_t next){
    long pageSize=sysconf(_SC_PAGESIZE);if(pageSize<=0||(pageSize&(pageSize-1)))return false;
    uintptr_t address=state.base+slotOffset(slot),page=address&~(uintptr_t(pageSize)-1);
    if(!state.relro.contains(page,size_t(pageSize))||!state.rw.contains(page,size_t(pageSize))||page<state.base+0x96008)return false;
    if(mprotect(reinterpret_cast<void*>(page),size_t(pageSize),PROT_READ|PROT_WRITE))return false;
    bool changed=__atomic_compare_exchange_n(reinterpret_cast<uintptr_t*>(address),&expected,next,false,__ATOMIC_ACQ_REL,__ATOMIC_ACQUIRE);
    bool protectedAgain=mprotect(reinterpret_cast<void*>(page),size_t(pageSize),PROT_READ)==0;
    if(!protectedAgain){
        // A failed permission restoration is not an installed capability.
        if(changed){uintptr_t ours=next;(void)__atomic_compare_exchange_n(reinterpret_cast<uintptr_t*>(address),&ours,expected,false,__ATOMIC_ACQ_REL,__ATOMIC_ACQUIRE);}
        (void)mprotect(reinterpret_cast<void*>(page),size_t(pageSize),PROT_READ);fail(Protection);
    }
    return changed&&protectedAgain;
}
int streamHook(void* object,char* pcm,int bytes,int flag);
int transferHook(void* object,char* pcm,int bytes);
void afterInit(uintptr_t caller,void* object,int card,int device,int channels,int soft){
    Control guard(true);uintptr_t single=uintptr_t(object);
    bool valid=guard.locked&&state.installed.load(std::memory_order_acquire)&&!state.closing.load(std::memory_order_acquire)
        &&caller==state.base+InitReturn&&sameSingle(single)&&channels==2&&soft==0&&card>=0&&device>=0;
    bool clean=valid&&emptyUnits(single)&&state.inFlight.load(std::memory_order_acquire)==0&&state.outputInFlight.load(std::memory_order_acquire)==0;
    bool reservation=clean&&state.prepared.load(std::memory_order_acquire)&&state.card==card&&state.device==device;
    if(!guard.locked)fail(Reentry);else if(!valid)fail(Caller);else if(!clean)fail(DirtyInit);
    // This function is void and vendor start does not inspect a return value.
    // Always execute the original initializer, including unknown SDK callers.
    originalInit()(object,card,device,channels,soft);
    if(!clean)return;
    if(!layout(single)){fail(UnitLayout);state.prepared.store(false,std::memory_order_release);return;}
    uintptr_t effect=load<uintptr_t>(single+0x30);
    if(state.owned.load(std::memory_order_acquire))state.retired.store(state.effect.load(std::memory_order_acquire),std::memory_order_release);
    state.kernel.resetAfterStop();state.kernel.ownerMute(true);state.ownerZero.store(true,std::memory_order_release);clearOutput();
    state.bound.store(false,std::memory_order_release);state.revoked.store(false,std::memory_order_release);
    state.single.store(single,std::memory_order_release);state.effect.store(effect,std::memory_order_release);
    for(size_t i=0;i<Members.size();i++)state.units[i].store(load<uintptr_t>(single+Members[i]),std::memory_order_release);
    state.write.store(load<uintptr_t>(single+0x20),std::memory_order_release);state.volume.store(load<uintptr_t>(single+0x38),std::memory_order_release);
    state.owned.store(reservation,std::memory_order_release);state.generation.fetch_add(1,std::memory_order_acq_rel);
    uintptr_t expected=state.base+EffectPoint;
    if(!__atomic_compare_exchange_n(reinterpret_cast<uintptr_t*>(effect),&expected,shadowPoint(),false,__ATOMIC_ACQ_REL,__ATOMIC_ACQUIRE)){
        fail(ShadowInstall);state.prepared.store(false,std::memory_order_release);return;
    }
    // A normal unowned SDK path retains its original streamIn semantics.
    state.error.store(Ok,std::memory_order_release);
}
void initHook(void* single,int card,int device,int channels,int soft){
    afterInit(uintptr_t(__builtin_return_address(0)),single,card,device,channels,soft);
}
struct Flight {Flight(){state.inFlight.fetch_add(1,std::memory_order_acq_rel);}~Flight(){state.inFlight.fetch_sub(1,std::memory_order_release);}};
bool zeroGate(){return state.ownerZero.load(std::memory_order_acquire)||state.revoked.load(std::memory_order_acquire)
    ||!state.bound.load(std::memory_order_acquire)||state.closing.load(std::memory_order_acquire)||state.error.load(std::memory_order_acquire)!=Ok||!outputReady();}

// The one shared transferData PLT also serves capture and other SDK outputs.
// Only this exact Mtk writePcm call on the bound output object is observed.
bool outputIdentity(uintptr_t alsa){
    const auto adapter=state.adapter.load(std::memory_order_acquire),write=state.write.load(std::memory_order_acquire);
    return pointer(alsa)&&alsa==state.alsa.load(std::memory_order_acquire)&&pointer(adapter)&&pointer(write)
        &&load<uintptr_t>(write)==state.base+WritePoint&&load<uintptr_t>(write+0x48)==adapter
        &&load<uintptr_t>(adapter)==state.base+MtkPoint&&load<uintptr_t>(adapter+0x18)==alsa
        &&load<uintptr_t>(alsa)==state.base+AlsaPoint&&load<uintptr_t>(alsa+8)==state.pcm.load(std::memory_order_acquire)
        &&load<uintptr_t>(alsa+0x18)==state.transfer.load(std::memory_order_acquire);
}
int observeTransfer(uintptr_t caller,void* object,char* pcm,int bytes){
    const auto alsa=uintptr_t(object);
    const bool candidate=caller==state.base+TransferReturn&&state.owned.load(std::memory_order_acquire)
        &&state.bound.load(std::memory_order_acquire)&&alsa==state.alsa.load(std::memory_order_acquire);
    if(!candidate)return originalTransfer()(object,pcm,bytes);
    state.outputInFlight.fetch_add(1,std::memory_order_acq_rel);
    const auto generation=state.generation.load(std::memory_order_acquire);
    const bool identity=outputIdentity(alsa);
    if(!identity)fail(OutputBinding);
    // Retain the original transfer's exact arguments, return and I/O behavior.
    const int result=originalTransfer()(object,pcm,bytes);
    if(identity&&generation==state.generation.load(std::memory_order_acquire)&&state.owned.load(std::memory_order_acquire)
        &&state.bound.load(std::memory_order_acquire)&&alsa==state.alsa.load(std::memory_order_acquire)){
        const int32_t raw=int32_t(gainWord(alsa+0x10));
        state.outputRaw.store(raw,std::memory_order_release);
        if(result<0||raw<0){
            int32_t zero=0;state.outputError.compare_exchange_strong(zero,raw<0?raw:result,std::memory_order_acq_rel);fail(OutputWrite);
        }
        const auto at=nowNs();
        if(!at||at<state.outputStarted.load(std::memory_order_acquire)||at<state.outputLast.load(std::memory_order_acquire))fail(OutputClock);
        else {
            const auto last=state.outputLast.load(std::memory_order_acquire),started=state.outputStarted.load(std::memory_order_acquire);
            if(at-(last?last:started)>OutputDeadlineNs)fail(OutputStall);
            state.outputLast.store(at,std::memory_order_release);state.outputCompleted.fetch_add(1,std::memory_order_release);
        }
    }
    state.outputInFlight.fetch_sub(1,std::memory_order_release);return result;
}
int transferHook(void* object,char* pcm,int bytes){return observeTransfer(uintptr_t(__builtin_return_address(0)),object,pcm,bytes);}
int streamHook(void* object,char* pcm,int bytes,int flag){
    Flight flight;uintptr_t effect=uintptr_t(object);
    if(effect!=state.effect.load(std::memory_order_acquire)){
        if(effect==state.retired.load(std::memory_order_acquire)){if(pcm&&bytes>0&&bytes<=2048)std::memset(pcm,0,size_t(bytes));fail(RetiredCallback);return -ESTALE;}
        return originalEffect()(object,pcm,bytes,flag);
    }
    if(!state.owned.load(std::memory_order_acquire))return originalEffect()(object,pcm,bytes,flag);
    if(bytes<0||bytes>2048||(bytes&3)||(!pcm&&bytes)||flag!=0){fail(FrameShape);return -EINVAL;}
    if(!bytes)return 0;
    uintptr_t write=state.write.load(std::memory_order_acquire);
    if(load<uintptr_t>(effect)!=shadowPoint()||load<uintptr_t>(effect+0x10)!=write||!pointer(write)
        ||load<uintptr_t>(write)!=state.base+WritePoint||load<uintptr_t>(state.base+WritePoint+0x18)!=state.base+WriteStream){
        std::memset(pcm,0,size_t(bytes));fail(NextUnit);return -ESTALE;
    }
    uintptr_t volume=state.volume.load(std::memory_order_acquire);
    bool gainsZero=!pointer(volume)||gainWord(volume+0x1048)==0||gainWord(volume+0x104c)==0;
    // Recompute this render gate every block: a transient upstream zero must
    // not permanently latch the kernel's owner gate after a later fresh unmute.
    bool gate=gainsZero||zeroGate();state.kernel.ownerMute(gate);
    if(gate)std::memset(pcm,0,size_t(bytes));
    state.kernel.process(reinterpret_cast<int16_t*>(pcm),bytes/4);
    if(gainsZero||zeroGate())std::memset(pcm,0,size_t(bytes));
    return originalWrite()(reinterpret_cast<void*>(write),pcm,bytes,flag);
}
}

bool install(uintptr_t base,MemoryRange rw,MemoryRange relro){
    Control guard;if(!guard.locked)return false;
    if(state.installed.load(std::memory_order_acquire)){
        if(state.base!=base||state.closing.load(std::memory_order_acquire))return false;
        if(__atomic_load_n(reinterpret_cast<uintptr_t*>(base+InitSlot),__ATOMIC_ACQUIRE)!=uintptr_t(&initHook)
            ||__atomic_load_n(reinterpret_cast<uintptr_t*>(base+TransferSlot),__ATOMIC_ACQUIRE)!=uintptr_t(&transferHook)){
            fail(ForeignSlot);state.installed.store(false,std::memory_order_release);return false;
        }
        return true;
    }
    if(state.closing.load(std::memory_order_acquire)||(state.base&&state.base!=base))return false;
    if(!base||(base&7)||!rw.contains(base+Manager,0x300)||!relro.contains(base+EffectTable,120)
        ||!relro.contains(base+InitSlot,8)||!relro.contains(base+TransferSlot,8))return false;
    if(load<uint32_t>(base+0x39f38)!=0x97ffd94a||load<uint32_t>(base+0x43470)!=0x97ffac98
        ||load<uintptr_t>(base+EffectTable)!=0||load<uintptr_t>(base+EffectTable+8)!=base+0x97d08)return false;
    for(size_t i=0;i<EffectEntries.size();i++)if(load<uintptr_t>(base+EffectPoint+i*8)!=base+EffectEntries[i])return false;
    if(load<uintptr_t>(base+WritePoint+0x18)!=base+WriteStream)return false;
    if(__atomic_load_n(reinterpret_cast<uintptr_t*>(base+InitSlot),__ATOMIC_ACQUIRE)!=base+Init
        ||__atomic_load_n(reinterpret_cast<uintptr_t*>(base+TransferSlot),__ATOMIC_ACQUIRE)!=base+Transfer){fail(ForeignSlot);return false;}
    state.base=base;state.rw=rw;state.relro=relro;std::memcpy(state.shadow.data(),reinterpret_cast<const void*>(base+EffectTable),120);
    state.shadow[5]=uintptr_t(&streamHook);state.closing.store(false,std::memory_order_release);
    if(!replaceSlot(Slot::Output,base+Transfer,uintptr_t(&transferHook)))return false;
    state.patches.store(2,std::memory_order_release);
    if(!replaceSlot(Slot::Initialize,base+Init,uintptr_t(&initHook))){
        if(replaceSlot(Slot::Output,uintptr_t(&transferHook),base+Transfer))state.patches.store(0,std::memory_order_release);
        return false;
    }
    state.patches.store(3,std::memory_order_release);
    state.error.store(Ok,std::memory_order_release);state.installed.store(true,std::memory_order_release);return true;
}
int prepareOwner(int card,int device){
    Control guard(true);if(!guard.locked)return -EAGAIN;
    if(!state.installed.load(std::memory_order_acquire)||state.closing.load(std::memory_order_acquire))return -ENODEV;
    if(card<0||device<0)return -EINVAL;
    uintptr_t single=load<uintptr_t>(state.base+Manager+0x2f8),strategy=0;if(!sameSingle(single,&strategy))return -ESTALE;
    Locks locks;if(!locks.takeStrategy(strategy)||!locks.takeSingle(single))return -EAGAIN;
    int status=load<int32_t>(strategy+0x18);
    if((status!=-1&&status!=2)||!emptyUnits(single)||state.inFlight.load(std::memory_order_acquire)||state.outputInFlight.load(std::memory_order_acquire))return -EBUSY;
    if(state.owned.load(std::memory_order_acquire))state.retired.store(state.effect.load(std::memory_order_acquire),std::memory_order_release);
    state.kernel.resetAfterStop();state.kernel.ownerMute(true);state.ownerZero.store(true,std::memory_order_release);clearOutput();
    state.generation.fetch_add(1,std::memory_order_acq_rel);
    state.effect.store(0,std::memory_order_release);state.single.store(0,std::memory_order_release);state.write.store(0,std::memory_order_release);state.volume.store(0,std::memory_order_release);
    for(auto& unit:state.units)unit.store(0,std::memory_order_release);
    state.owned.store(false,std::memory_order_release);state.bound.store(false,std::memory_order_release);state.revoked.store(false,std::memory_order_release);
    state.card=card;state.device=device;state.error.store(Ok,std::memory_order_release);state.prepared.store(true,std::memory_order_release);return 0;
}
int bind(uintptr_t single,uintptr_t effect){
    Control guard(true);if(!guard.locked)return -EAGAIN;
    if(!state.installed.load(std::memory_order_acquire)||state.closing.load(std::memory_order_acquire))return -ENODEV;
    if(!state.owned.load(std::memory_order_acquire)||state.revoked.load(std::memory_order_acquire)||state.error.load(std::memory_order_acquire)
        ||single!=state.single.load(std::memory_order_acquire)||effect!=state.effect.load(std::memory_order_acquire)||!sameSingle(single)
        ||load<uintptr_t>(single+0x30)!=effect||load<uintptr_t>(effect)!=shadowPoint()||load<uintptr_t>(effect+0x10)!=load<uintptr_t>(single+0x20))return -ESTALE;
    for(size_t i=0;i<Members.size();i++){
        uintptr_t unit=load<uintptr_t>(single+Members[i]);
        if(unit!=state.units[i].load(std::memory_order_acquire)||!pointer(unit)
            ||load<uintptr_t>(unit)!=(i==4?shadowPoint():state.base+UnitPoints[i])
            ||load<int32_t>(unit+0x18)!=2||load<int32_t>(unit+0x1c)!=0||load<int32_t>(unit+0x20)!=48000)return -ESTALE;
    }
    if(!state.ownerZero.load(std::memory_order_acquire))return -EACCES;
    uintptr_t volume=load<uintptr_t>(single+0x38);
    if(volume!=state.volume.load(std::memory_order_acquire)||gainWord(volume+0x1048)||gainWord(volume+0x104c))return -EACCES;
    uintptr_t adapter=load<uintptr_t>(state.write.load(std::memory_order_acquire)+0x48);
    if(!pointer(adapter)||load<uintptr_t>(adapter)!=state.base+MtkPoint)return -ESTALE;
    uintptr_t alsa=load<uintptr_t>(adapter+0x18);
    if(!pointer(alsa)||load<uintptr_t>(alsa)!=state.base+AlsaPoint||!pointer(load<uintptr_t>(alsa+8))||!codePointer(load<uintptr_t>(alsa+0x18)))return -ESTALE;
    if(state.alsa.load(std::memory_order_acquire)){
        if(!outputIdentity(alsa))return -ESTALE;
    }else {
        const auto at=nowNs();if(!at)return -EIO;
        state.adapter.store(adapter);state.alsa.store(alsa);state.pcm.store(load<uintptr_t>(alsa+8));state.transfer.store(load<uintptr_t>(alsa+0x18));
        state.outputStarted.store(at,std::memory_order_release);
    }
    state.bound.store(true,std::memory_order_release);state.prepared.store(false,std::memory_order_release);return 0;
}
bool configure(int profile,int wet,int gain,bool muted,bool bypass){
    if(!live())return false;return state.kernel.configure(profile,wet,gain,muted,bypass);
}
bool ownerMute(bool muted){
    if(!muted&&(!live()||!outputReady())){state.ownerZero.store(true,std::memory_order_release);state.kernel.ownerMute(true);return false;}
    state.ownerZero.store(muted,std::memory_order_release);state.kernel.ownerMute(muted);
    return true;
}
bool notch(int slot,int hz,int cut){if(!live())return false;return state.kernel.notch(slot,hz,cut);}
void revoke(){state.revoked.store(true,std::memory_order_release);state.bound.store(false,std::memory_order_release);state.prepared.store(false,std::memory_order_release);state.ownerZero.store(true,std::memory_order_release);state.kernel.ownerMute(true);}
bool live(){
    if(!state.installed.load(std::memory_order_acquire)||!state.owned.load(std::memory_order_acquire)||!state.bound.load(std::memory_order_acquire)
        ||state.revoked.load(std::memory_order_acquire)||state.closing.load(std::memory_order_acquire)||state.error.load(std::memory_order_acquire)!=Ok)return false;
    (void)outputReady(); // a missing first completion has a bounded grace
    return state.error.load(std::memory_order_acquire)==Ok;
}
bool fault(){return state.error.load(std::memory_order_acquire)!=Ok;}
template<class T,class=void>struct NotchState {static bool pending(const T&){return false;}};
template<class T>struct NotchState<T,std::void_t<decltype(T::notchRequestedRevision),decltype(T::notchAppliedRevision)>> {
    static bool pending(const T& value){return value.notchRequestedRevision!=value.notchAppliedRevision;}
};
Status snapshot(){
    auto value=state.kernel.snapshot();Status status;status.requestedRevision=value.requestedRevision;status.appliedRevision=value.appliedRevision;
    status.callbacks=value.callbacks;status.frames=value.frames;status.profile=value.profile;status.wet=value.wet;status.monitorGain=value.monitorGain;status.peak=value.peak;
    status.flags=(state.installed.load()?Installed:0)|(state.prepared.load()?Prepared:0)|(state.bound.load()?Bound:0)|(state.owned.load()?Owned:0)
        |((value.muted||zeroGate())?Muted:0)|(value.bypass?Bypass:0)|(live()?Live:0)|(NotchState<decltype(value)>::pending(value)?NotchPending:0)
        |(outputReady()?OutputReady:0);
    status.fault=state.error.load();return status;
}
OutputStatus outputSnapshot(){
    OutputStatus status;status.generation=state.generation.load(std::memory_order_acquire);status.completedCalls=state.outputCompleted.load(std::memory_order_acquire);
    status.lastCompletionNs=state.outputLast.load(std::memory_order_acquire);status.lastRawResult=state.outputRaw.load(std::memory_order_acquire);
    status.stickyRawError=state.outputError.load(std::memory_order_acquire);status.inFlight=state.outputInFlight.load(std::memory_order_acquire);return status;
}
void shutdown(){
    if((!state.installed.load(std::memory_order_acquire)&&!state.patches.load(std::memory_order_acquire))||state.closing.exchange(true,std::memory_order_acq_rel))return;
    revoke();Control guard;if(!guard.locked||!state.base)return;
    for(auto slot:{Slot::Initialize,Slot::Output}){
        const auto offset=slotOffset(slot),hook=slot==Slot::Initialize?uintptr_t(&initHook):uintptr_t(&transferHook),original=state.base+(slot==Slot::Initialize?Init:Transfer);
        uintptr_t current=__atomic_load_n(reinterpret_cast<uintptr_t*>(state.base+offset),__ATOMIC_ACQUIRE);
        if(current==hook){if(!replaceSlot(slot,current,original))fail(Protection);}
        else if(current!=original)fail(ForeignSlot);
    }
    state.patches.store(0,std::memory_order_release);
    state.installed.store(false,std::memory_order_release);
}
void finishAfterPayloadStop(){
    // No payload reads: the environment has already completed its dlclose.
    // A violated producer fence remains gated rather than clearing live DSP.
    state.ownerZero.store(true,std::memory_order_release);state.kernel.ownerMute(true);
    if(state.inFlight.load(std::memory_order_acquire)||state.outputInFlight.load(std::memory_order_acquire)){fail(ShuttingDown);return;}
    state.kernel.resetAfterStop();state.single.store(0);state.effect.store(0);state.write.store(0);state.volume.store(0);state.retired.store(0);
    for(auto& unit:state.units)unit.store(0);
    state.prepared.store(false);state.bound.store(false);state.owned.store(false);state.revoked.store(true);state.installed.store(false);state.closing.store(true);
    state.base=0;state.rw={};state.relro={};state.card=state.device=-1;
    state.patches.store(0);clearOutput();
}
#if defined(FRIDAY_INLINEFX_TEST)
namespace test {
void calls(Calls value){state.calls=value;}
void control(bool hold){if(hold)pthread_mutex_lock(&state.control);else pthread_mutex_unlock(&state.control);}
void init(uintptr_t caller,void* single,int card,int device,int channels,int soft){afterInit(caller,single,card,device,channels,soft);}
int stream(void* effect,char* pcm,int bytes,int flag){return streamHook(effect,pcm,bytes,flag);}
int transfer(uintptr_t caller,void* alsa,char* pcm,int bytes){return observeTransfer(caller,alsa,pcm,bytes);}
void reset(){
    state.installed.store(false);state.closing.store(false);state.prepared.store(false);state.bound.store(false);state.owned.store(false);state.revoked.store(false);state.ownerZero.store(true);
    state.error.store(0);state.single.store(0);state.effect.store(0);state.write.store(0);state.volume.store(0);state.retired.store(0);state.generation.store(0);state.inFlight.store(0);
    for(auto& unit:state.units)unit.store(0);
    state.patches.store(0);state.outputInFlight.store(0);state.outputCompleted.store(0);clearOutput();
    state.base=0;state.rw={};state.relro={};state.card=state.device=-1;state.calls={};state.kernel.resetAfterStop();
}
}
#endif
}
