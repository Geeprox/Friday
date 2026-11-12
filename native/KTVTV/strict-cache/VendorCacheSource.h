#ifndef FRIDAY_VENDOR_CACHE_SOURCE_H
#define FRIDAY_VENDOR_CACHE_SOURCE_H
#include "StrictCacheCore.h"
#include <array>
#include <pthread.h>
#if defined(FRIDAY_SYSTEM_MODULE_SHIM)
#include "InlineEffectHook.h"
#endif
namespace fridaystrict {
struct VendorSymbols {
    uintptr_t manager=0,strategyVtable=0,singleVtable=0,cacheVtable=0,volumeVtable=0;
    int (*getData)(void*,char*,int)=nullptr;
    unsigned (*length)(void*)=nullptr;
    int (*tryLock)(uintptr_t)=[](uintptr_t p){return pthread_mutex_trylock(reinterpret_cast<pthread_mutex_t*>(p));};
    void (*unlock)(uintptr_t)=[](uintptr_t p){(void)pthread_mutex_unlock(reinterpret_cast<pthread_mutex_t*>(p));};
};
/* Exact, SHA-pinned arm64 layout, not a generic OEM plugin ABI. The loader is
 * solely responsible for establishing mapped-image and silent-start proof. */
class VendorCacheSource final:public Source {
    struct Binding {
        uintptr_t strategy=0,single=0,cache=0,loop=0,volume=0,effect=0;int card=0,device=0;
        bool operator==(const Binding& b)const{return strategy==b.strategy&&single==b.single&&cache==b.cache&&loop==b.loop&&volume==b.volume&&effect==b.effect&&card==b.card&&device==b.device;}
    };
    VendorSymbols symbols_;bool proof_;Binding current_{},pending_{};uint64_t pendingStart_=0,pendingNext_=0;unsigned pendingAttempts_=0;
public:
    struct Safety {uint64_t attempts=0,zeroStores=0,busyExhausted=0,identityRefused=0;int last=0;};
private:
    Safety safety_{};
    template<class T>static T load(uintptr_t at){T value;std::memcpy(&value,reinterpret_cast<const void*>(at),sizeof value);return value;}
    static bool pointer(uintptr_t p){return p>=4096&&(p&7)==0;}
    struct Locks {
        const VendorSymbols& symbols;uintptr_t a=0,b=0;
        explicit Locks(const VendorSymbols& s):symbols(s){}
        ~Locks(){if(b)symbols.unlock(b);if(a)symbols.unlock(a);}
        bool first(uintptr_t p){if(symbols.tryLock(p))return false;a=p;return true;}
        bool second(uintptr_t p){if(symbols.tryLock(p))return false;b=p;return true;}
    };
    int inspect(Locks& locks,int card,int device,Binding& b){
        auto& strategy=b.strategy;auto& single=b.single;auto& cache=b.cache;auto& loop=b.loop;b.card=card;b.device=device;
        if(!proof_)return -EACCES;strategy=load<uintptr_t>(symbols_.manager+0x2e8);
        if(!pointer(strategy)||load<uintptr_t>(strategy)!=symbols_.strategyVtable)return -ESTALE;
        if(!locks.first(strategy+0x28))return -EAGAIN;
        if(load<int>(strategy+0x18)!=0||load<int>(strategy+0x58)!=card||load<int>(strategy+0x5c)!=device
            ||load<int>(strategy+0x60)!=2||load<int>(strategy+0x64)!=48000)return -ESTALE;
        single=load<uintptr_t>(strategy+0x10);
        if(!pointer(single)||load<uintptr_t>(single)!=symbols_.singleVtable)return -ESTALE;
        if(!locks.second(single+0x50))return -EAGAIN;
        cache=load<uintptr_t>(single+0x28);
        if(!pointer(cache)||load<uintptr_t>(cache)!=symbols_.cacheVtable)return -ESTALE;
        loop=load<uintptr_t>(cache+0x48);if(!pointer(loop))return -ESTALE;
        b.effect=load<uintptr_t>(single+0x30);
        b.volume=load<uintptr_t>(single+0x38);if(!pointer(b.volume)||load<uintptr_t>(b.volume)!=symbols_.volumeVtable)return -ESTALE;return 0;
    }
    void finishPending(int result){
        if(!result){safety_.zeroStores++;safety_.last=1;}
        else if(result==-EAGAIN){safety_.busyExhausted++;safety_.last=2;}
        else {safety_.identityRefused++;safety_.last=3;}
        pending_=Binding{};
    }
public:
    VendorCacheSource(VendorSymbols symbols,bool proved):symbols_(symbols),proof_(proved&&pointer(symbols.manager)&&symbols.getData&&symbols.length&&symbols.tryLock&&symbols.unlock){}
    bool proved()const override{
#if defined(FRIDAY_SYSTEM_MODULE_SHIM)
        return proof_&&(inlinefx::snapshot().flags&1)!=0;
#else
        return proof_;
#endif
    }
    uint32_t capabilities()const override{
#if defined(FRIDAY_SYSTEM_MODULE_SHIM)
        return FRIDAY_CACHE_INLINE_CAPABILITIES;
#else
        return FRIDAY_CACHE_CAPABILITIES;
#endif
    }
    int prepareOwner(int card,int device)override{
#if defined(FRIDAY_SYSTEM_MODULE_SHIM)
        return inlinefx::prepareOwner(card,device);
#else
        (void)card;(void)device;return 0;
#endif
    }
    void reset()override{
#if defined(FRIDAY_SYSTEM_MODULE_SHIM)
        if(current_.strategy)inlinefx::revoke();
#endif
        current_=Binding{};
    }
    bool pending()const{return pending_.strategy!=0;}
    Safety safety()const{return safety_;}
    void revokeOwned(uint64_t now)override{
        if(!current_.strategy)return;
        pending_=current_;reset();pendingStart_=pendingNext_=now;pendingAttempts_=0;maintenance(now);
    }
    void rebindForOwner(uint64_t now)override{
#if defined(FRIDAY_SYSTEM_MODULE_SHIM)
        (void)now;inlinefx::ownerMute(true);current_=Binding{};
#else
        revokeOwned(now);
#endif
    }
    void abandonPending(){if(pending())finishPending(-EAGAIN);}
    void maintenance(uint64_t now)override{
        if(!pending())return;
        if(now<pendingStart_){finishPending(-EAGAIN);return;}
        if(now-pendingStart_>=100||pendingAttempts_>=10){finishPending(-EAGAIN);return;}
        if(now<pendingNext_)return;
        ++pendingAttempts_;++safety_.attempts;
        Locks locks(symbols_);Binding observed{};int result=inspect(locks,pending_.card,pending_.device,observed);
        if(!result&&!(observed==pending_))result=-ESTALE;
        if(!result){
            /* One-way owned-expiry failsafe only. Exact inspected VolUnit fields
             * are aligned float multipliers, consumed by streamIn; no setter,
             * gain increase, START, device call or arbitrary address is exposed. */
            __atomic_store_n(reinterpret_cast<uint32_t*>(observed.volume+0x1048),0,__ATOMIC_RELEASE);
            __atomic_store_n(reinterpret_cast<uint32_t*>(observed.volume+0x104c),0,__ATOMIC_RELEASE);
            finishPending(0);
        }else if(result!=-EAGAIN)finishPending(result);
        else if(pendingAttempts_>=10||now-pendingStart_>=100)finishPending(-EAGAIN);
        else pendingNext_=now+10;
    }
    int bind(int card,int device)override{
        reset();Locks locks(symbols_);Binding b{};int status=inspect(locks,card,device,b);if(status)return status;
        /* Consume only this observer cache, once, to discard a previous lease's
         * PCM. Dry playback does not read this cache. Empty get enables cache
         * production; no polling or wait for new samples occurs here. */
        unsigned bytes=symbols_.length(reinterpret_cast<void*>(b.loop));if(bytes>10240||bytes%4)return -ESTALE;
        std::array<int16_t,5120> scratch{};int count=symbols_.getData(reinterpret_cast<void*>(b.cache),reinterpret_cast<char*>(scratch.data()),bytes?int(bytes):512);
        friday_cache_wipe(scratch.data(),sizeof scratch);
        if(count!=(bytes?int(bytes):0))return -ESTALE;
#if defined(FRIDAY_SYSTEM_MODULE_SHIM)
        status=inlinefx::bind(b.single,b.effect);if(status)return status;
#endif
        current_=b;pending_=Binding{};return 0;
    }
    int check()override{Locks locks(symbols_);Binding b{};int status=inspect(locks,current_.card,current_.device,b);
        if(!status&&!(b==current_))status=-ESTALE;
#if defined(FRIDAY_SYSTEM_MODULE_SHIM)
        if(!status&&!inlinefx::live())status=-ESTALE;
#endif
        return status;}
    int read128(int16_t* stereo)override{
        friday_cache_wipe(stereo,512);Locks locks(symbols_);Binding b{};int status=inspect(locks,current_.card,current_.device,b);
        if(!status&&!(b==current_))status=-ESTALE;
#if defined(FRIDAY_SYSTEM_MODULE_SHIM)
        if(!status&&!inlinefx::live())status=-ESTALE;
#endif
        if(status)return status;
        unsigned available=symbols_.length(reinterpret_cast<void*>(b.loop));if(available>10240||available%4)return -ESTALE;
        /* Drop a stale observer backlog in one bounded cache get. Never label
         * discarded or empty samples as fresh input. Caller retries later. */
        if(available>1024){std::array<int16_t,5120> scratch{};int n=symbols_.getData(reinterpret_cast<void*>(b.cache),reinterpret_cast<char*>(scratch.data()),int(available));friday_cache_wipe(scratch.data(),sizeof scratch);return n==int(available)?-EAGAIN:-ESTALE;}
        int n=symbols_.getData(reinterpret_cast<void*>(b.cache),reinterpret_cast<char*>(stereo),512);
        if(n==512)return 0;friday_cache_wipe(stereo,512);return n==0?-EAGAIN:-ESTALE;
    }
#if defined(FRIDAY_SYSTEM_MODULE_SHIM)
    int configureInline(int profile,int wet,int gain,bool muted,bool bypass)override{
        const int status=check();if(status)return status;return inlinefx::configure(profile,wet,gain,muted,bypass)?0:-EINVAL;
    }
    int muteInline(bool muted)override{
        const int status=check();if(status)return status;return inlinefx::ownerMute(muted)?0:-EAGAIN;
    }
    int notchInline(int slot,int hz,int cut)override{
        const int status=check();if(status)return status;return inlinefx::notch(slot,hz,cut)?0:-EINVAL;
    }
    int inlineStatus(FridayInlineMetrics& output)override{
        const int status=check();if(status)return status;const auto s=inlinefx::snapshot();
        output={s.requestedRevision,s.appliedRevision,s.callbacks,s.frames,s.profile,s.wet,s.monitorGain,s.peak,s.flags,s.fault};return 0;
    }
    void readerLost()override{inlinefx::ownerMute(true);}
#endif
};
}
#endif
