// Pure anonymous-memory fixtures only. No DSO, PCM, Binder, device or service.
#include "InlineEffectHook.h"
#if defined(FRIDAY_VENDOR_INLINE_INTEGRATION_TEST)
#if !defined(FRIDAY_SYSTEM_MODULE_SHIM)
#error Vendor inline integration must compile the real shim Source path
#endif
#include "VendorCacheSource.h"
#endif
#include <array>
#include <atomic>
#include <cassert>
#include <cerrno>
#include <cstring>
#include <iostream>
#include <pthread.h>
#include <sys/mman.h>
#include <unistd.h>
#include <thread>
#include <vector>
using namespace fridaystrict;
namespace fx=fridaystrict::inlinefx;
namespace {
struct Fixture;Fixture* fixture=nullptr;int checks=0;
void checked(bool value,int line){++checks;if(!value){std::cerr<<"check "<<checks<<" failed at line "<<line<<"\n";std::abort();}}
#define check(value) checked((value),__LINE__)
template<class T>void put(uintptr_t at,T value){std::memcpy(reinterpret_cast<void*>(at),&value,sizeof value);}
template<class T>T get(uintptr_t at){T value;std::memcpy(&value,reinterpret_cast<void*>(at),sizeof value);return value;}
struct Fixture {
    uintptr_t base=0,strategy=0,single=0,adapter=0,alsa=0;std::array<uintptr_t,6> units{};
    alignas(16) std::array<char,0x2200> strategyBytes{};
    alignas(16) std::array<char,0xb0> singleBytes{};
    std::array<std::array<char,0x2200>,6> unitBytes{};
    alignas(16) std::array<char,0x2200> adapterBytes{};
    alignas(16) std::array<char,0x40> alsaBytes{},pcmBytes{};
    int initCalls=0,effectCalls=0,writeCalls=0,lastBytes=0,lastFlag=-1;char* lastPcm=nullptr;bool badUnit=false;
    std::array<int16_t,1024> written{};
    std::atomic<bool> pauseWrite{false},enteredWrite{false},leaveWrite{false};
    std::atomic<bool> pauseTransfer{false},enteredTransfer{false},leaveTransfer{false};
    std::atomic<uint64_t> time{1000000000};
    bool autoCompletion=true;int transferCalls=0,transferResult=0,rawResult=0,transferBytes=0;char* transferPcm=nullptr;void* transferObject=nullptr;
    Fixture(){
        fx::test::reset();fixture=this;
        base=uintptr_t(mmap(nullptr,0xa0000,PROT_READ|PROT_WRITE,MAP_ANON|MAP_PRIVATE,-1,0));assert(base!=uintptr_t(MAP_FAILED));
        strategy=uintptr_t(strategyBytes.data());single=uintptr_t(singleBytes.data());
        put<uintptr_t>(base+0x9dec0+0x2e8,strategy);put<uintptr_t>(base+0x9dec0+0x2f8,single);
        put<uintptr_t>(strategy,base+0x976d8);put<uintptr_t>(strategy+0x10,single);put<int32_t>(strategy+0x18,-1);put<uintptr_t>(single,base+0x97608);
        assert(!pthread_mutex_init(reinterpret_cast<pthread_mutex_t*>(strategy+0x28),nullptr));
        assert(!pthread_mutex_init(reinterpret_cast<pthread_mutex_t*>(single+0x50),nullptr));
        constexpr uintptr_t entries[]{0x4a2fc,0x4a358,0x49cf4,0x49f1c,0x4a268,0x39d80,0x39d9c,0x39db8,0x39e24,0x39e40,0x39e7c,0x39eb8,0x39ed8};
        put<uintptr_t>(base+0x97c90,0);put<uintptr_t>(base+0x97c98,base+0x97d08);
        for(int i=0;i<13;i++)put<uintptr_t>(base+0x97ca0+i*8,base+entries[i]);
        put<uintptr_t>(base+0x97e10+0x18,base+0x4c3fc);put<uint32_t>(base+0x39f38,0x97ffd94a);put<uintptr_t>(base+0x9c560,base+0x3a1c0);
        put<uint32_t>(base+0x43470,0x97ffac98);put<uintptr_t>(base+0x9b698,base+0x542c0);
        adapter=uintptr_t(adapterBytes.data());alsa=uintptr_t(alsaBytes.data());
        fx::test::calls({init,effect,write,transfer,now});
    }
    ~Fixture(){fx::shutdown();fx::test::reset();pthread_mutex_destroy(reinterpret_cast<pthread_mutex_t*>(single+0x50));pthread_mutex_destroy(reinterpret_cast<pthread_mutex_t*>(strategy+0x28));munmap(reinterpret_cast<void*>(base),0xa0000);fixture=nullptr;}
    // Anonymous fixture ranges include the host's 16 KiB permission page.
    // Production receives the independently authenticated payload mappings;
    // a page that crosses their RELRO boundary must be refused.
    MemoryRange rw()const{return {base+0x97378,0xa0000-0x97378};}MemoryRange relro()const{return rw();}
    bool install(){return fx::install(base,rw(),relro());}
    void foreignSlot(uintptr_t value,uintptr_t offset=0x9c560){
        auto size=uintptr_t(sysconf(_SC_PAGESIZE));auto page=(base+offset)&~(size-1);
        assert(!mprotect(reinterpret_cast<void*>(page),size,PROT_READ|PROT_WRITE));put<uintptr_t>(base+offset,value);
        assert(!mprotect(reinterpret_cast<void*>(page),size,PROT_READ));
    }
    static void init(void* object,int card,int device,int channels,int soft){
        auto& f=*fixture;check(uintptr_t(object)==f.single);check(card==2&&device==0&&channels==2&&soft==0);++f.initCalls;
        constexpr uintptr_t member[]{0x10,0x18,0x20,0x28,0x30,0x38};constexpr uintptr_t point[]{0x97f30,0x97d30,0x97e10,0x97b80,0x97ca0,0x98010};
        for(int i=0;i<6;i++){
            f.unitBytes[i].fill(0);f.units[i]=uintptr_t(f.unitBytes[i].data());put<uintptr_t>(f.single+member[i],f.units[i]);put<uintptr_t>(f.units[i],f.base+point[i]);
            put<int32_t>(f.units[i]+0x18,2);put<int32_t>(f.units[i]+0x1c,0);put<int32_t>(f.units[i]+0x20,48000);
        }
        if(f.badUnit)put<int32_t>(f.units[4]+0x20,44100);
    }
    static int effect(void*,char*,int,int){++fixture->effectCalls;return 17;}
    static int write(void* object,char* pcm,int bytes,int flag){
        auto& f=*fixture;check(uintptr_t(object)==f.units[2]);++f.writeCalls;f.lastPcm=pcm;f.lastBytes=bytes;f.lastFlag=flag;
        if(bytes)std::memcpy(f.written.data(),pcm,size_t(bytes));
        f.enteredWrite.store(true,std::memory_order_release);
        while(f.pauseWrite.load(std::memory_order_acquire)&&!f.leaveWrite.load(std::memory_order_acquire))std::this_thread::yield();
        if(f.autoCompletion&&fx::snapshot().flags&fx::Bound)f.completeOutput();
        return 31;
    }
    static uint64_t now(){return fixture->time.load(std::memory_order_acquire);}
    static int transfer(void* object,char* pcm,int bytes){
        auto& f=*fixture;++f.transferCalls;f.transferObject=object;f.transferPcm=pcm;f.transferBytes=bytes;
        f.enteredTransfer.store(true,std::memory_order_release);
        while(f.pauseTransfer.load(std::memory_order_acquire)&&!f.leaveTransfer.load(std::memory_order_acquire))std::this_thread::yield();
        put<int32_t>(uintptr_t(object)+0x10,f.rawResult);return f.transferResult;
    }
    int completeOutput(uintptr_t caller=0){return fx::test::transfer(caller?caller:base+0x43474,reinterpret_cast<void*>(alsa),pcmBytes.data(),512);}
    void start(bool owned=true,uintptr_t caller=0){
        if(owned)check(fx::prepareOwner(2,0)==0);
        fx::test::init(caller?caller:base+0x39f3c,reinterpret_cast<void*>(single),2,0,2,0);
        // Model the subsequent original start's fixed chain, without audio.
        put<uintptr_t>(units[4]+0x10,units[2]);put<int32_t>(strategy+0x18,0);
        put<uintptr_t>(units[2]+0x48,adapter);put<uintptr_t>(adapter,base+0x97a20);put<uintptr_t>(adapter+0x18,alsa);
        put<uintptr_t>(alsa,base+0x981c8);put<uintptr_t>(alsa+8,uintptr_t(pcmBytes.data()));put<uintptr_t>(alsa+0x18,base+0x1234);
    }
    void stop(){for(auto offset:{0x10,0x18,0x20,0x28,0x30,0x38})put<uintptr_t>(single+offset,0);put<int32_t>(strategy+0x18,2);}
    void gain(bool on){put<uint32_t>(units[5]+0x1048,on?0x3f800000:0);put<uint32_t>(units[5]+0x104c,on?0x3f800000:0);}
    int render(std::array<int16_t,256>& pcm,int bytes=512,int flag=0){return fx::test::stream(reinterpret_cast<void*>(units[4]),reinterpret_cast<char*>(pcm.data()),bytes,flag);}
};
template<class T>bool zero(const T& samples){for(auto value:samples)if(value)return false;return true;}
}

#if defined(FRIDAY_VENDOR_INLINE_INTEGRATION_TEST)
namespace {
// macOS pthread_mutex_t is 64 bytes and overlaps the Android Strategy's
// +0x58..0x64 metadata. The actual fixed-offset lock is used ONLY by prepare,
// then destroyed before metadata is written. Source's existing Symbols seam
// maps the same logical addresses to separate real host mutexes. This verifies
// Source/Core/Hook/DSP wiring and lock order, not Android mutex ABI/concurrency.
struct RuntimeCache {
    Fixture& memory;
    alignas(16) std::array<char,64> loop{};
    pthread_mutex_t strategyLock=PTHREAD_MUTEX_INITIALIZER,singleLock=PTHREAD_MUTEX_INITIALIZER;
    bool strategyHeld=false,singleHeld=false;int available=0,gets=0,lengths=0;
    uint64_t deliveredFrames=0;int16_t raw=12000;
    static RuntimeCache* active;
    explicit RuntimeCache(Fixture& f):memory(f){active=this;}
    ~RuntimeCache(){check(!strategyHeld&&!singleHeld);pthread_mutex_destroy(&strategyLock);pthread_mutex_destroy(&singleLock);active=nullptr;}
    uintptr_t loopAddress()const{return uintptr_t(loop.data());}
    VendorSymbols symbols(){
        VendorSymbols result;result.manager=memory.base+0x9dec0;
        result.strategyVtable=memory.base+0x976d8;result.singleVtable=memory.base+0x97608;
        result.cacheVtable=memory.base+0x97b80;result.volumeVtable=memory.base+0x98010;
        result.tryLock=[](uintptr_t at){auto& c=*active;check(at==c.memory.strategy+0x28||at==c.memory.single+0x50);
            const bool first=at==c.memory.strategy+0x28;
            check(first?!c.strategyHeld&&!c.singleHeld:c.strategyHeld&&!c.singleHeld);
            int result=pthread_mutex_trylock(first?&c.strategyLock:&c.singleLock);
            if(!result){if(first)c.strategyHeld=true;else c.singleHeld=true;}return result;};
        result.unlock=[](uintptr_t at){auto& c=*active;
            if(at==c.memory.single+0x50){check(c.strategyHeld&&c.singleHeld);c.singleHeld=false;check(!pthread_mutex_unlock(&c.singleLock));}
            else {check(at==c.memory.strategy+0x28&&c.strategyHeld&&!c.singleHeld);c.strategyHeld=false;check(!pthread_mutex_unlock(&c.strategyLock));}};
        result.length=[](void* object)->unsigned{auto& c=*active;check(uintptr_t(object)==c.loopAddress()&&c.strategyHeld&&c.singleHeld);++c.lengths;return unsigned(c.available);};
        result.getData=[](void* object,char* data,int bytes){auto& c=*active;
            check(uintptr_t(object)==c.memory.units[3]&&c.strategyHeld&&c.singleHeld&&bytes>0&&bytes<=10240&&bytes%4==0);++c.gets;
            if(c.available<bytes)return 0;
            for(int i=0;i<bytes;i+=2)std::memcpy(data+i,&c.raw,2);
            c.available-=bytes;c.deliveredFrames+=unsigned(bytes/4);return bytes;};
        return result;
    }
};
RuntimeCache* RuntimeCache::active=nullptr;
struct Integration {
    Fixture memory;RuntimeCache cache{memory};VendorCacheSource source{cache.symbols(),true};
    Core core{{10001,10002},source,900,100,UINT64_C(0x1122334455667788)};
    Peer owner{1,0,0,10001,100},reader{2,0,0,10002,200};FridayCachePacket packet{};
    bool nativeStrategyLock=true;uint64_t lease=77;
    Integration(){check(memory.install());check(source.proved()&&source.capabilities()==FRIDAY_CACHE_INLINE_CAPABILITIES);}
    ~Integration(){core.disconnected(owner,now());restoreNativeStrategy();}
    uint64_t now()const{return memory.time.load(std::memory_order_acquire)/1000000;}
    void tick(uint64_t ms=3){memory.time.fetch_add(ms*1000000,std::memory_order_release);}
    void restoreNativeStrategy(){if(!nativeStrategyLock){check(!pthread_mutex_init(reinterpret_cast<pthread_mutex_t*>(memory.strategy+0x28),nullptr));nativeStrategyLock=true;}}
    void releaseNativeStrategy(){check(nativeStrategyLock);check(!pthread_mutex_destroy(reinterpret_cast<pthread_mutex_t*>(memory.strategy+0x28)));nativeStrategyLock=false;}
    FridayCacheRequest request(Peer& p,int op){
        FridayCacheRequest r{};r.magic=FRIDAY_CACHE_MAGIC;r.version=1;r.opcode=uint16_t(op);r.request_id=p.lastRequest+1;
        if(op==FRIDAY_CACHE_HELLO)r.flags=p.uid==10001?FRIDAY_CACHE_OWNER:FRIDAY_CACHE_READER;
        else {r.lease=lease;r.generation=core.generation();if(p.role==FRIDAY_CACHE_OWNER){r.owner_pid=p.pid;r.card=2;}else r.reader_pid=p.pid;}
        if(op==FRIDAY_CACHE_BEGIN||op==FRIDAY_CACHE_ATTACH)r.generation=0;
        if(op==FRIDAY_CACHE_READ)r.frames=128;return r;
    }
    int send(Peer& p,FridayCacheRequest r){
        const auto bytes=core.handle(p,r,now(),packet);
        check(packet.header.capabilities==63&&packet.header.opcode==r.opcode&&packet.header.request_id==r.request_id);
        check(bytes==sizeof(FridayCacheResponse)+(packet.header.frames?512:!packet.header.status&&r.opcode==FRIDAY_CACHE_INLINE_STATUS?56:0));
        if(packet.header.status){check(packet.header.frames==0);check(zero(packet.pcm));}
        check(memory.initCalls<=1);return packet.header.status;
    }
    int send(Peer& p,int op){return send(p,request(p,op));}
    int mute(bool value){auto r=request(owner,FRIDAY_CACHE_INLINE_MUTE);r.flags=value;return send(owner,r);}
    int configure(Peer& p,int profile,int wet,int gain=500){auto r=request(p,FRIDAY_CACHE_INLINE_CONFIGURE);r.flags=unsigned(profile)|(unsigned(wet)<<3)|(unsigned(gain)<<12);return send(p,r);}
    int notch(int slot,int hz,int cut){auto r=request(owner,FRIDAY_CACHE_INLINE_NOTCH);r.flags=unsigned(slot)|(unsigned(hz)<<3)|(unsigned(cut)<<16);return send(owner,r);}
    FridayInlineMetrics status(Peer& p){check(send(p,FRIDAY_CACHE_INLINE_STATUS)==0);return packet.inline_metrics;}
    void start(){
        check(send(owner,FRIDAY_CACHE_HELLO)==0);check(send(reader,FRIDAY_CACHE_HELLO)==0);check(memory.initCalls==0);
        restoreNativeStrategy();check(send(owner,FRIDAY_CACHE_BEGIN)==0);check(memory.initCalls==0);releaseNativeStrategy();
        // Source.prepareOwner already made the real reservation. Do not invoke
        // prepare a second time through the standalone fixture helper.
        memory.start(false);check(memory.initCalls==1);
        put<int32_t>(memory.strategy+0x58,2);put<int32_t>(memory.strategy+0x5c,0);
        put<int32_t>(memory.strategy+0x60,2);put<int32_t>(memory.strategy+0x64,48000);
        put<uintptr_t>(memory.units[3]+0x48,cache.loopAddress());
        memory.gain(true);check(send(owner,FRIDAY_CACHE_BIND)==-EACCES); // actual zero prerequisite
        memory.gain(false);check(send(owner,FRIDAY_CACHE_BIND)==0);check(send(reader,FRIDAY_CACHE_ATTACH)==0);
        check(configure(reader,0,0)==0);check(memory.initCalls==1);
    }
    void warm(Peer& p,int blocks=16){
        for(int i=0;i<blocks;i++){cache.available=512;check(send(p,FRIDAY_CACHE_READ)==0);check(packet.header.frames==128);
            for(auto sample:packet.pcm)check(sample==cache.raw);tick();}
    }
    void gain(){put<uint32_t>(memory.units[5]+0x1048,0x3f000000);put<uint32_t>(memory.units[5]+0x104c,0x3f800000);}
    std::array<int16_t,256> render(int blocks=1,int16_t input=6000){
        std::array<int16_t,256> pcm{};for(int n=0;n<blocks;n++){pcm.fill(input);check(memory.render(pcm)==31);tick();}return pcm;
    }
    void audible(){start();warm(reader);memory.completeOutput();check(mute(false)==0);gain();check(!zero(render(6)));}
};
void integrationLifecycle(){
    Integration h;h.start();auto generation=h.core.generation();auto initial=h.status(h.owner);
    check((initial.flags&77)==77&&(initial.flags&fx::Muted)&&!(initial.flags&fx::OutputReady));
    check(h.mute(false)==-ESTALE&&h.core.generation()==generation); // no raw warmup
    h.memory.autoCompletion=false;h.warm(h.reader);check(h.cache.deliveredFrames==2048);
    check(h.mute(false)==-EAGAIN&&h.core.generation()==generation); // raw alone cannot open
    check(zero(h.render()));check(h.memory.effectCalls==0);
    h.memory.completeOutput();check(h.status(h.reader).flags&fx::OutputReady);h.memory.autoCompletion=true;
    check(h.mute(false)==0);h.gain();auto dry=h.render(6);
    // This is already post-Vol (.5) PCM. A .5 reference must not multiply dry
    // again: raw cache=12000, post-Vol input/output=6000 once ramps settle.
    for(auto sample:dry)check(sample==6000);check(h.memory.lastBytes==512&&h.memory.lastFlag==0);
    constexpr int wet[]{0,250,320,280,360,180,160};
    for(int profile=1;profile<7;profile++){
        check(h.configure(h.reader,profile,wet[profile])==0);auto pending=h.status(h.reader);check(pending.requested_revision!=pending.applied_revision);
        auto pcm=h.render(6,3000);for(auto sample:pcm)check(std::abs(int(sample))<=16384);
        auto applied=h.status(h.reader);check(applied.profile==profile&&applied.wet==wet[profile]&&applied.monitor_gain==500);
        check(applied.requested_revision==applied.applied_revision&&(applied.flags&fx::OutputReady));
    }
    for(int slot=0;slot<8;slot++)check(h.notch(slot,250+slot*500,6)==0);
    check(h.status(h.reader).flags&fx::NotchPending);h.render(6,3000);check(!(h.status(h.reader).flags&fx::NotchPending));
    int gets=h.cache.gets;check(!pthread_mutex_lock(&h.cache.strategyLock));
    check(h.send(h.reader,FRIDAY_CACHE_INLINE_STATUS)==-EAGAIN&&h.core.generation()==generation&&h.cache.gets==gets);
    h.render();check(!pthread_mutex_unlock(&h.cache.strategyLock)); // bounded Source busy never blocks render
    h.core.disconnected(h.reader,h.now());check(zero(h.render()));check(h.mute(false)==-ESTALE&&h.core.generation()==generation);
    Peer replacement{3,0,0,10002,201};check(h.send(replacement,FRIDAY_CACHE_HELLO)==0);check(h.send(replacement,FRIDAY_CACHE_ATTACH)==-EBUSY);
    check(h.mute(true)==0);h.memory.gain(false);auto callbacks=h.status(h.owner).callbacks;
    check(h.send(h.owner,FRIDAY_CACHE_BIND)==0&&h.core.generation()>generation);check(h.memory.initCalls==1&&h.memory.effectCalls==0);
    check(h.send(h.reader,FRIDAY_CACHE_READ)==-ESTALE);check(h.send(replacement,FRIDAY_CACHE_ATTACH)==0);
    check(h.mute(false)==-ESTALE);h.warm(replacement);check(h.configure(replacement,2,250)==0);
    check(zero(h.render(3)));check(h.status(replacement).callbacks>callbacks);check(h.mute(false)==0);h.gain();check(!zero(h.render(6)));
    check(h.memory.initCalls==1&&h.memory.effectCalls==0);check(h.source.safety().zeroStores==0);
}
void integrationOutputFaults(){
    {Integration h;h.audible();auto before=h.status(h.reader).frames;h.memory.rawResult=-32;h.memory.transferResult=-1;
        check(h.memory.completeOutput()==-1);check(h.memory.initCalls==1);h.cache.available=512;
        check(h.send(h.reader,FRIDAY_CACHE_INLINE_STATUS)==-ESTALE&&h.core.generation()==0);
        check(h.source.safety().zeroStores==1);check(zero(h.render()));check(fx::snapshot().frames>before);
        h.memory.rawResult=h.memory.transferResult=0;h.memory.completeOutput();check(h.mute(false)==-ESTALE);
        check(fx::outputSnapshot().stickyRawError==-32&&!fx::live()&&h.memory.effectCalls==0);}
    {Integration h;h.audible();h.memory.autoCompletion=false;auto before=h.status(h.reader).frames;
        h.tick(251);h.cache.available=512;int gets=h.cache.gets;
        check(h.send(h.owner,FRIDAY_CACHE_INLINE_STATUS)==-ESTALE&&h.core.generation()==0);check(h.cache.gets==gets&&h.cache.available==512);
        check(fx::snapshot().fault==fx::OutputStall);check(zero(h.render()));check(fx::snapshot().frames>before&&h.memory.initCalls==1);}
}
void integrationStaleObjects(){
    // Each authorized inline Source method must immediately revoke on its
    // actual identity check failing; an unauthorized request must not revoke.
    for(int op:{FRIDAY_CACHE_INLINE_CONFIGURE,FRIDAY_CACHE_INLINE_STATUS,FRIDAY_CACHE_INLINE_MUTE,FRIDAY_CACHE_INLINE_NOTCH}){
        Integration h;h.audible();auto generation=h.core.generation();auto bad=h.request(h.reader,FRIDAY_CACHE_INLINE_CONFIGURE);bad.generation--;
        check(h.send(h.reader,bad)==-ESTALE&&h.core.generation()==generation&&fx::live());
        alignas(16) std::array<char,64> replacement{};put<uintptr_t>(h.memory.units[3]+0x48,uintptr_t(replacement.data()));
        Peer& peer=(op==FRIDAY_CACHE_INLINE_MUTE||op==FRIDAY_CACHE_INLINE_NOTCH)?h.owner:h.reader;auto r=h.request(peer,op);
        if(op==FRIDAY_CACHE_INLINE_CONFIGURE)r.flags=500<<12;
        if(op==FRIDAY_CACHE_INLINE_MUTE)r.flags=1;
        if(op==FRIDAY_CACHE_INLINE_NOTCH)r.flags=1000<<3|6<<16;
        int gets=h.cache.gets;check(h.send(peer,r)==-ESTALE&&h.core.generation()==0);check(h.cache.gets==gets);
        check(h.source.safety().identityRefused==1&&h.source.safety().zeroStores==0);
        check(zero(h.render())&&h.memory.effectCalls==0&&h.memory.initCalls==1);
    }
}
}
int main(){integrationLifecycle();integrationOutputFaults();integrationStaleObjects();
    std::cout<<"vendor inline integration: "<<checks<<" real Source/Core/Hook/Kernel checks passed; host sidecar lock ABI adapter, no vendor/device/audio execution\n";}
#else
int main(){
    {Fixture f;put<uintptr_t>(f.base+0x9c560,0x12345678);check(!f.install());check(get<uintptr_t>(f.base+0x9c560)==0x12345678);}
    {Fixture f;put<uintptr_t>(f.base+0x9b698,0x12345678);check(!f.install());check(get<uintptr_t>(f.base+0x9c560)==f.base+0x3a1c0);check(get<uintptr_t>(f.base+0x9b698)==0x12345678);}
    {Fixture f;check(f.install());auto hook=get<uintptr_t>(f.base+0x9c560);check(hook!=f.base+0x3a1c0);check(f.install());
        check(fx::snapshot().flags&fx::Installed);check(fx::prepareOwner(-1,0)==-EINVAL);
        auto beforeBusy=fx::snapshot();fx::test::control(true);check(fx::prepareOwner(2,0)==-EAGAIN);check(fx::bind(f.single,0)==-EAGAIN);fx::test::control(false);
        auto afterBusy=fx::snapshot();check(std::memcmp(&beforeBusy,&afterBusy,sizeof beforeBusy)==0);
        auto before=fx::snapshot();put<int32_t>(f.strategy+0x18,0);check(fx::prepareOwner(2,0)==-EBUSY);check(fx::snapshot().flags==before.flags);put<int32_t>(f.strategy+0x18,-1);
        pthread_mutex_lock(reinterpret_cast<pthread_mutex_t*>(f.strategy+0x28));check(fx::prepareOwner(2,0)==-EAGAIN);pthread_mutex_unlock(reinterpret_cast<pthread_mutex_t*>(f.strategy+0x28));
        f.start();check(f.initCalls==1);check(get<uintptr_t>(f.units[4])!=f.base+0x97ca0);check(!fx::live());
        std::array<int16_t,256> pcm{};pcm.fill(9000);check(f.render(pcm)==31&&zero(pcm));check(f.effectCalls==0);
        check(fx::bind(f.single,f.units[4])==0&&fx::live());check(fx::configure(2,250,1000,false,false));f.completeOutput();
        // The render callback must not acquire either lifecycle vendor mutex.
        pthread_mutex_lock(reinterpret_cast<pthread_mutex_t*>(f.strategy+0x28));pthread_mutex_lock(reinterpret_cast<pthread_mutex_t*>(f.single+0x50));
        fx::ownerMute(false);f.gain(true);for(int i=0;i<10;i++){pcm.fill(9000);check(f.render(pcm)==31);}
        pthread_mutex_unlock(reinterpret_cast<pthread_mutex_t*>(f.single+0x50));pthread_mutex_unlock(reinterpret_cast<pthread_mutex_t*>(f.strategy+0x28));
        check(!zero(pcm));check(f.lastPcm==reinterpret_cast<char*>(pcm.data())&&f.lastBytes==512&&f.lastFlag==0);check(f.effectCalls==0);
        auto rendered=fx::snapshot();check(rendered.callbacks>=11&&rendered.frames>=1408);
        check(fx::bind(f.single,f.units[4])==-EACCES); // owner gate is open
        fx::ownerMute(true);f.gain(false);auto beforeRebind=fx::snapshot();
        check(fx::bind(f.single,f.units[4])==0);check(fx::snapshot().callbacks==beforeRebind.callbacks);
        check(get<uintptr_t>(f.units[4])!=f.base+0x97ca0); // same shadow; no reinitialization
        fx::ownerMute(false);
        f.gain(false);pcm.fill(9000);check(f.render(pcm)==31&&zero(pcm));
        // An actual zero-gain block cannot permanently swallow the next unmute.
        f.gain(true);for(int i=0;i<4;i++){pcm.fill(9000);check(f.render(pcm)==31);}check(!zero(pcm));
        check(fx::notch(0,1000,6));check(fx::snapshot().flags&fx::NotchPending);for(int i=0;i<8;i++){pcm.fill(9000);f.render(pcm);}check(!(fx::snapshot().flags&fx::NotchPending));
        fx::ownerMute(true);pcm.fill(9000);check(f.render(pcm)==31&&zero(pcm));
        fx::revoke();fx::ownerMute(false);check(!fx::live());check(!fx::configure(2,250,1000,false,false));check(fx::bind(f.single,f.units[4])==-ESTALE);
        pcm.fill(9000);check(f.render(pcm)==31&&zero(pcm));check(f.effectCalls==0);
        f.stop();f.start();check(fx::bind(f.single,f.units[4])==0);check(fx::snapshot().flags&fx::Muted);f.completeOutput();
        f.gain(true);fx::ownerMute(false);check(fx::configure(0,0,1000,false,true));pcm.fill(9000);f.render(pcm);
        // Hold a fake output callback, then present apparently empty units.
        f.pauseWrite.store(true);f.enteredWrite.store(false);f.leaveWrite.store(false);
        std::thread audio([&]{std::array<int16_t,256> block{};block.fill(5000);f.render(block);});
        while(!f.enteredWrite.load(std::memory_order_acquire))std::this_thread::yield();
        fx::revoke();f.stop();check(fx::prepareOwner(2,0)==-EBUSY); // no render-thread DSP reset while in flight
        f.leaveWrite.store(true,std::memory_order_release);audio.join();f.pauseWrite.store(false);check(fx::prepareOwner(2,0)==0);
        fx::shutdown();check(get<uintptr_t>(f.base+0x9c560)==f.base+0x3a1c0);check(get<uintptr_t>(f.base+0x9b698)==f.base+0x542c0);fx::shutdown();check(get<uintptr_t>(f.base+0x9c560)==f.base+0x3a1c0);
    }
    {Fixture f;check(f.install());f.start(false);std::array<int16_t,256> pcm{};pcm.fill(77);check(f.render(pcm)==17&&f.effectCalls==1&&f.writeCalls==0);check(fx::bind(f.single,f.units[4])==-ESTALE);}
    {Fixture f;check(f.install());f.start(false,f.base+0x39f40);check(f.initCalls==1&&fx::fault());check(get<uintptr_t>(f.units[4])==f.base+0x97ca0);check(fx::bind(f.single,f.units[4])==-ESTALE);}
    {Fixture f;check(f.install());f.badUnit=true;f.start();check(f.initCalls==1&&fx::fault());check(get<uintptr_t>(f.units[4])==f.base+0x97ca0);check(fx::bind(f.single,f.units[4])==-ESTALE);}
    {Fixture f;check(f.install());f.start();check(fx::bind(f.single,f.units[4])==0);std::array<int16_t,256> pcm{};check(f.render(pcm,510)==-EINVAL);check(f.writeCalls==0&&fx::fault());}
    {Fixture f;check(f.install());f.start();check(fx::bind(f.single,f.units[4])==0);std::array<int16_t,256> pcm{};put<uintptr_t>(f.units[4]+0x10,f.units[3]);check(f.render(pcm)==-ESTALE&&f.writeCalls==0);}
    {Fixture f;check(f.install());f.start();put<uintptr_t>(f.single+0x28,f.units[3]+8);check(fx::bind(f.single,f.units[4])==-ESTALE);}
    {Fixture f;check(f.install());f.start();auto vptr=get<uintptr_t>(f.units[4]);auto before=fx::snapshot();fx::shutdown();check(get<uintptr_t>(f.units[4])==vptr);check(fx::snapshot().callbacks==before.callbacks);check(fx::snapshot().flags&fx::Owned);}
    {Fixture f;check(f.install());f.foreignSlot(0x12345678);check(!f.install());check(get<uintptr_t>(f.base+0x9c560)==0x12345678);fx::shutdown();check(get<uintptr_t>(f.base+0x9c560)==0x12345678);}
    {Fixture f;check(f.install());f.foreignSlot(0x12345678,0x9b698);check(!f.install());fx::shutdown();check(get<uintptr_t>(f.base+0x9b698)==0x12345678);check(get<uintptr_t>(f.base+0x9c560)==f.base+0x3a1c0);}
    {Fixture f;put<uintptr_t>(f.base+0x97cb8,f.base+0x49f20);check(!f.install());check(get<uintptr_t>(f.base+0x9c560)==f.base+0x3a1c0);}
    if(sysconf(_SC_PAGESIZE)>4096){Fixture f;check(!fx::install(f.base,{f.base+0x97378,31224},{f.base+0x97378,0x5c88}));check(get<uintptr_t>(f.base+0x9c560)==f.base+0x3a1c0);}
    {Fixture f;check(f.install());f.start();fx::shutdown();
        // Simulate a completed payload unload, including inaccessible GOT.
        check(!mprotect(reinterpret_cast<void*>(f.base),0xa0000,PROT_NONE));
        fx::shutdown();fx::shutdown();fx::finishAfterPayloadStop();fx::shutdown();fx::finishAfterPayloadStop();
        check(!(fx::snapshot().flags&(fx::Installed|fx::Owned|fx::Bound|fx::Live)));check(fx::snapshot().flags&fx::Muted);
    }
    {Fixture f;check(f.install());f.start();check(fx::bind(f.single,f.units[4])==0);f.autoCompletion=false;
        check(fx::live()&&!fx::fault());check(!(fx::snapshot().flags&fx::OutputReady));check(!fx::ownerMute(false));check(fx::snapshot().flags&fx::Muted);
        // Capture's call and another output object must not manufacture output readiness.
        auto before=fx::outputSnapshot();f.completeOutput(f.base+0x4a634);check(fx::outputSnapshot().completedCalls==before.completedCalls);
        alignas(16) std::array<char,64> other{};check(fx::test::transfer(f.base+0x43474,other.data(),f.pcmBytes.data(),128)==0);
        check(fx::outputSnapshot().completedCalls==before.completedCalls);check(!fx::ownerMute(false));
        // Even a nonnegative raw byte-like result is not a verified byte count.
        f.rawResult=17;check(f.completeOutput()==0);auto done=fx::outputSnapshot();
        check(done.completedCalls==before.completedCalls+1&&done.lastCompletionNs==f.time.load());check(done.lastRawResult==17&&!done.byteCountVerified);
        check(f.transferObject==reinterpret_cast<void*>(f.alsa)&&f.transferPcm==f.pcmBytes.data()&&f.transferBytes==512);
        check(fx::snapshot().flags&fx::OutputReady);check(fx::ownerMute(false));f.gain(true);check(fx::configure(0,0,1000,false,true));
        std::array<int16_t,256> pcm{};for(int i=0;i<4;i++){pcm.fill(7000);f.render(pcm);}check(!zero(pcm));
        auto frames=fx::snapshot().frames;int writes=f.writeCalls;
        f.time.fetch_add(250000001);pcm.fill(7000);check(f.render(pcm)==31&&zero(pcm));
        check(f.writeCalls==writes+1&&fx::snapshot().frames>frames);check(!fx::live()&&fx::snapshot().fault==fx::OutputStall);
        check(!fx::ownerMute(false));f.rawResult=0;check(f.completeOutput()==0);check(!fx::live());check(!(fx::snapshot().flags&fx::OutputReady));
        check(fx::bind(f.single,f.units[4])==-ESTALE); // a rebind cannot clear sticky output failure
        auto oldGeneration=fx::outputSnapshot().generation,oldCompletions=fx::outputSnapshot().completedCalls;
        f.stop();f.start();check(fx::bind(f.single,f.units[4])==0);check(!fx::fault());check(fx::outputSnapshot().generation>oldGeneration);
        check(fx::outputSnapshot().completedCalls>=oldCompletions&&fx::outputSnapshot().lastCompletionNs==0);check(!fx::ownerMute(false));f.completeOutput();check(fx::ownerMute(false));
    }
    {Fixture f;check(f.install());f.start();check(fx::bind(f.single,f.units[4])==0);f.autoCompletion=false;
        f.time.fetch_add(250000001);check(!fx::live()&&fx::snapshot().fault==fx::OutputStall);check(!fx::ownerMute(false));
        std::array<int16_t,256> pcm{};pcm.fill(8000);check(f.render(pcm)==31&&zero(pcm));
    }
    {Fixture f;check(f.install());f.start();check(fx::bind(f.single,f.units[4])==0);f.autoCompletion=false;f.completeOutput();
        check(fx::ownerMute(false));f.gain(true);check(fx::configure(0,0,1000,false,true));
        f.rawResult=-32;f.transferResult=-1;auto count=fx::outputSnapshot().completedCalls;
        check(f.completeOutput()==-1);check(fx::outputSnapshot().completedCalls==count+1);check(fx::outputSnapshot().stickyRawError==-32);
        check(!fx::live()&&fx::snapshot().fault==fx::OutputWrite);std::array<int16_t,256> pcm{};pcm.fill(9000);check(f.render(pcm)==31&&zero(pcm));
        f.rawResult=0;f.transferResult=0;f.completeOutput();check(fx::outputSnapshot().stickyRawError==-32&&!fx::live());check(fx::ownerMute(true));
    }
    {Fixture f;check(f.install());f.start();check(fx::bind(f.single,f.units[4])==0);f.autoCompletion=false;
        f.pauseTransfer.store(true);f.enteredTransfer.store(false);std::thread output([&]{f.completeOutput();});
        while(!f.enteredTransfer.load(std::memory_order_acquire))std::this_thread::yield();
        check(fx::outputSnapshot().inFlight==1);auto before=fx::outputSnapshot().completedCalls;
        fx::revoke();f.stop();check(fx::prepareOwner(2,0)==-EBUSY);
        f.leaveTransfer.store(true,std::memory_order_release);output.join();check(fx::outputSnapshot().inFlight==0);check(fx::outputSnapshot().completedCalls==before);
        check(fx::prepareOwner(2,0)==0);f.start();check(fx::bind(f.single,f.units[4])==0);check(!fx::ownerMute(false));
    }
    {Fixture f;check(f.install());f.start();check(fx::bind(f.single,f.units[4])==0);f.autoCompletion=false;f.completeOutput();
        put<uintptr_t>(f.adapter+0x18,f.alsa+8);int before=f.transferCalls;check(f.completeOutput()==0);check(f.transferCalls==before+1);
        check(!fx::live()&&fx::snapshot().fault==fx::OutputBinding);
    }
    {Fixture f;check(f.install());f.start();check(fx::bind(f.single,f.units[4])==0);f.time.store(1);check(!fx::live()&&fx::snapshot().fault==fx::OutputClock);}
    std::cout<<"inline effect hook: "<<checks<<" pure memory/ABI/gate/lifecycle checks passed\n";
}
#endif
