#include "StrictCacheCore.h"
#include "StrictCacheConfig.h"
#include "StrictCacheClient.h"
#include "VendorCacheSource.h"
#include "Sha256.h"
#include "InstanceCookie.h"
#include <array>
#include <cassert>
#include <chrono>
#include <cstdio>
#include <thread>
#include <vector>
using namespace fridaystrict;
struct FakeSource:Source {
    bool proof=true,running=true;int reads=0,binds=0,resets=0,mutes=0,bindStatus=0,readStatus=0,checkStatus=0;
    bool proved()const override{return proof;}
    int bind(int card,int device)override{binds++;return bindStatus?bindStatus:running&&card==3&&device==1?0:-ESTALE;}
    int check()override{return checkStatus?checkStatus:running?0:-ESTALE;}
    int read128(int16_t* p)override{reads++;for(int i=0;i<256;i++)p[i]=int16_t(i+1);return checkStatus?checkStatus:readStatus;}
    void reset()override{resets++;}
    void revokeOwned(uint64_t)override{mutes++;reset();}
};
struct Harness {
    FakeSource source;Core core{{10001,10002},source,900,100,0x1122334455667788};Peer owner{1,0,0,10001,100},reader{2,0,0,10002,200};FridayCachePacket reply{};uint64_t now=1;
    FridayCacheRequest request(Peer& p,int op){FridayCacheRequest r{};r.magic=FRIDAY_CACHE_MAGIC;r.version=1;r.opcode=uint16_t(op);r.request_id=p.lastRequest+1;
        if(op!=1){r.lease=77;r.generation=core.generation();if(p.role==1){r.owner_pid=p.pid;r.card=3;r.device=1;}else r.reader_pid=p.pid;}
        if(op==1)r.flags=p.uid==10001?1:2;if(op==2)r.generation=0;if(op==7)r.generation=0;if(op==6)r.frames=128;return r;}
    int call(Peer& p,FridayCacheRequest r){core.handle(p,r,now,reply);return reply.header.status;}
    int call(Peer& p,int op){return call(p,request(p,op));}
    void open(){assert(call(owner,1)==0);assert(call(reader,1)==0);assert(core.generation()==0);assert(call(owner,2)==0);assert(call(owner,3)==0);assert(call(reader,7)==0);}
};
void coreTests(){
    {Harness h;assert(!h.call(h.owner,1));assert(h.reply.header.sequence==0x1122334455667788&&h.reply.header.generation==0&&h.reply.header.lease==0&&h.reply.header.frames==0);assert(h.source.reads==0);}
    {FakeSource source;Core core({10001,10002},source,900,100,0);Peer peer{1,0,0,10001,100};FridayCacheRequest r{};r.magic=FRIDAY_CACHE_MAGIC;r.version=1;r.opcode=1;r.flags=1;r.request_id=1;FridayCachePacket p{};core.handle(peer,r,1,p);assert(p.header.status==-EACCES&&p.header.capabilities==0&&p.header.sequence==0);}
    assert(configAuthority(0,0600,1,false));assert(configAuthority(1041,0600,1,false));assert(!configAuthority(10001,0600,1,false));assert(!configAuthority(0,0644,1,false));assert(!configAuthority(0,0600,2,false));assert(!configAuthority(1041,0777,1,true));
    for(const char* bad:{"owner_uid=GUARD_UID\nreader_uid=MAIN_UID\n","owner_uid=0\nreader_uid=10002\n","owner_uid=10001\nowner_uid=10002\nreader_uid=10003\n","owner_uid=10001\nreader_uid=10002\npath=/dev/snd\n"}){
        char text[257]{};std::strcpy(text,bad);Config c;assert(parseConfig(text,std::strlen(text),c)==-EINVAL);}
    {char text[257]="owner_uid=10001\nreader_uid=10002\n";Config c;assert(!parseConfig(text,std::strlen(text),c));assert(c.ownerUid==10001&&c.readerUid==10002);}
    {Harness h;h.source.running=false;assert(!h.call(h.owner,1));assert(!h.call(h.reader,1));assert(!h.call(h.owner,2));auto gen=h.core.generation();
        assert(h.call(h.reader,7)==-ESTALE&&h.core.generation()==gen);h.source.running=true;assert(!h.call(h.owner,3));assert(h.core.generation()>gen);assert(!h.call(h.reader,7));assert(!h.call(h.reader,6));assert(h.reply.header.frames==128&&h.source.reads==1);}
    {Harness h;h.open();auto gen=h.core.generation();Peer other{3,0,0,10001,101};assert(!h.call(other,1));assert(h.call(other,2)==-EBUSY);assert(h.core.generation()==gen);
        Peer observer{4,0,0,10002,201};assert(!h.call(observer,1));assert(h.call(observer,7)==-EBUSY);assert(h.core.generation()==gen);
        auto wrong=h.request(h.reader,7);wrong.lease=88;assert(h.call(h.reader,wrong)==-ESTALE&&h.core.generation()==gen);assert(!h.call(h.reader,6));}
    {Harness h;h.open();auto gen=h.core.generation();Peer probe{3,0,0,10001,100};assert(!h.call(probe,1));h.core.disconnected(probe);
        assert(h.core.generation()==gen&&h.source.mutes==0);assert(!h.call(h.owner,4));assert(!h.call(h.reader,6));h.core.disconnected(h.owner);assert(h.source.mutes==1);}
    {Harness h;assert(!h.call(h.owner,1));assert(!h.call(h.owner,2));h.core.disconnected(h.owner);assert(h.source.mutes==0);}
    {Harness h;h.open();auto old=h.request(h.reader,6);assert(!h.call(h.owner,2));assert(!h.call(h.owner,3));auto gen=h.core.generation();assert(h.call(h.reader,old)==-ESTALE&&h.core.generation()==gen);
        assert(!h.call(h.reader,7));assert(!h.call(h.reader,6));h.core.disconnected(h.owner);assert(h.call(h.reader,6)==-ESTALE&&h.source.reads==1);}
    {Harness h;h.open();auto gen=h.core.generation();auto stale=h.request(h.owner,4);stale.generation--;assert(h.call(h.owner,stale)==-ESTALE&&h.core.generation()==gen);
        h.now+=750;assert(h.call(h.reader,6)==-ESTALE&&h.core.generation()==0&&h.source.reads==0);}
    {Harness h;h.open();h.now+=200;assert(!h.call(h.owner,4));h.now+=700;assert(!h.call(h.reader,6));h.source.checkStatus=-ESTALE;assert(h.call(h.owner,4)==-ESTALE);assert(!h.core.generation());}
    {Harness h;h.open();auto gen=h.core.generation();h.source.checkStatus=-EAGAIN;
        for(int n=0;n<6;n++){h.now+=200;assert(h.call(h.owner,4)==0&&h.core.generation()==gen);assert(h.reply.header.frames==0&&h.reply.header.sequence==0);
            assert(h.call(h.reader,6)==-EAGAIN);assert(h.reply.header.frames==0&&h.reply.header.sequence==0);for(auto sample:h.reply.pcm)assert(sample==0);}
        assert(h.source.mutes==0);h.source.checkStatus=-ESTALE;assert(h.call(h.owner,4)==-ESTALE&&h.core.generation()==0&&h.source.mutes==1);}
    {Harness h;h.open();h.source.readStatus=-EAGAIN;assert(h.call(h.reader,6)==-EAGAIN);for(auto v:h.reply.pcm)assert(v==0);assert(h.core.generation());
        h.source.readStatus=-ESTALE;assert(h.call(h.reader,6)==-ESTALE&&!h.core.generation());}
    {Harness h;h.open();auto r=h.request(h.reader,6);r.reserved=1;assert(h.call(h.reader,r)==-EPROTO&&h.source.reads==0);r=h.request(h.reader,6);r.frames=129;assert(h.call(h.reader,r)==-EPROTO);
        r=h.request(h.reader,6);r.magic=0;assert(h.call(h.reader,r)==-EPROTO);r=h.request(h.reader,6);r.request_id=h.reader.lastRequest;assert(h.call(h.reader,r)==-EPROTO);}
    {Harness h;Peer attacker{3,0,0,99999,300};auto r=h.request(attacker,1);r.flags=1;assert(h.call(attacker,r)==-EACCES&&!attacker.role&&!h.core.generation());
        h.source.proof=false;assert(h.call(h.owner,1)==-EACCES&&h.reply.header.capabilities==0);}
    {Harness h;h.open();auto old=h.request(h.reader,6);auto oldGeneration=h.core.generation();h.core.disconnected(h.reader);Peer other{3,0,0,10002,201};assert(!h.call(other,1));assert(h.call(other,7)==-EBUSY);
        assert(!h.call(h.owner,3)&&h.core.generation()>oldGeneration);assert(!h.call(other,7));
        assert(h.call(h.reader,old)==-ESTALE);assert(h.call(h.reader,6)==-ESTALE);assert(h.call(h.reader,7)==-EBUSY);assert(h.source.reads==0);assert(!h.call(other,6));}
    {Harness h;h.open();assert(!h.call(h.owner,5));assert(h.call(h.reader,6)==-ESTALE);assert(h.source.reads==0);}
    {Harness h;h.open();h.source.bindStatus=-EAGAIN;assert(h.call(h.owner,3)==-EAGAIN&&h.source.mutes==1);assert(h.call(h.reader,6)==-ESTALE);}
    {Harness h;assert(!h.call(h.owner,1));assert(!h.call(h.reader,1));assert(!h.call(h.owner,2));h.source.bindStatus=-EAGAIN;
        assert(h.call(h.owner,3)==-EAGAIN);h.now++;h.source.bindStatus=0;assert(!h.call(h.owner,3));h.source.checkStatus=-EAGAIN;
        assert(h.call(h.reader,7)==-EAGAIN);h.now++;h.source.checkStatus=0;assert(!h.call(h.reader,7));assert(!h.call(h.reader,6));}
    {Harness h;h.open();for(int n=0;n<100000;n++){h.now++;if(n%100==0)assert(!h.call(h.owner,4));assert(!h.call(h.reader,6));assert(h.reply.header.sequence==uint64_t(n+1));}assert(h.source.reads==100000);}
}
struct VendorFixture {
    alignas(16) std::array<unsigned char,1024> manager{},strategy{},single{},cache{},loop{};
    alignas(16) std::array<unsigned char,0x1100> volume{},replacementVolume{};int available=0,calls=0,busy=0;std::vector<uintptr_t> locks;
    static VendorFixture* active;
    template<class T>static void set(uintptr_t p,T value){std::memcpy(reinterpret_cast<void*>(p),&value,sizeof value);}
    uintptr_t m(){return reinterpret_cast<uintptr_t>(manager.data());}uintptr_t s(){return reinterpret_cast<uintptr_t>(strategy.data());}
    uintptr_t a(){return reinterpret_cast<uintptr_t>(single.data());}uintptr_t c(){return reinterpret_cast<uintptr_t>(cache.data());}uintptr_t l(){return reinterpret_cast<uintptr_t>(loop.data());}
    uintptr_t v(){return reinterpret_cast<uintptr_t>(volume.data());}
    uint32_t gain(){uint32_t value;std::memcpy(&value,volume.data()+0x1048,4);return value;}
    VendorFixture(){active=this;set(m()+0x2e8,s());set(s(),uintptr_t(0x10000));set(s()+0x18,0);set(s()+0x58,3);set(s()+0x5c,1);set(s()+0x60,2);set(s()+0x64,48000);
        set(s()+0x10,a());set(a(),uintptr_t(0x20000));set(a()+0x28,c());set(c(),uintptr_t(0x30000));set(c()+0x48,l());set(a()+0x38,v());set(v(),uintptr_t(0x40000));set(v()+0x1048,uint32_t(0x3f800000));set(v()+0x104c,uint32_t(0x3f800000));}
    VendorSymbols symbols(){VendorSymbols v;v.manager=m();v.strategyVtable=0x10000;v.singleVtable=0x20000;v.cacheVtable=0x30000;v.volumeVtable=0x40000;
        v.tryLock=[](uintptr_t p){auto& f=*active;assert(p==f.s()+0x28||p==f.a()+0x50);if((f.busy==1&&p==f.s()+0x28)||(f.busy==2&&p==f.a()+0x50))return EBUSY;
            if(p==f.a()+0x50)assert(f.locks.size()==1&&f.locks[0]==f.s()+0x28);f.locks.push_back(p);return 0;};
        v.unlock=[](uintptr_t p){auto& f=*active;assert(!f.locks.empty()&&f.locks.back()==p);f.locks.pop_back();};
        v.length=[](void* p)->unsigned{assert(uintptr_t(p)==active->l());return unsigned(active->available);};
        v.getData=[](void* p,char* bytes,int n){auto& f=*active;assert(uintptr_t(p)==f.c()&&f.locks.size()==2);assert(n>0&&n<=10240&&n%4==0);f.calls++;
            if(f.available<n)return 0;for(int i=0;i<n;i++)bytes[i]=char((i%127)+1);f.available-=n;return n;};return v;}
};VendorFixture* VendorFixture::active=nullptr;
void vendorTests(){
    VendorFixture f;VendorCacheSource source(f.symbols(),true);assert(source.proved());assert(source.bind(3,1)==0&&f.calls==1);std::array<int16_t,256> pcm{};
    assert(source.read128(pcm.data())==-EAGAIN);for(auto v:pcm)assert(!v);f.available=512;assert(source.read128(pcm.data())==0);assert(pcm[0]!=0);
    int calls=f.calls;f.busy=1;assert(source.read128(pcm.data())==-EAGAIN&&f.calls==calls);f.busy=2;assert(source.read128(pcm.data())==-EAGAIN&&f.calls==calls);assert(f.locks.empty());f.busy=0;
    f.available=2048;assert(source.read128(pcm.data())==-EAGAIN&&f.available==0);for(auto v:pcm)assert(!v);
    f.available=10244;assert(source.read128(pcm.data())==-ESTALE);f.available=0;VendorFixture::set(f.s()+0x64,44100);assert(source.check()==-ESTALE);
    VendorFixture::set(f.s()+0x64,48000);VendorFixture::set(f.s()+0x18,-1);assert(source.bind(3,1)==-ESTALE);VendorFixture::set(f.s()+0x18,0);
    assert(source.bind(4,1)==-ESTALE);VendorFixture::set(f.a(),uintptr_t(0x90000));assert(source.bind(3,1)==-ESTALE);assert(f.locks.empty());
    VendorFixture::set(f.a(),uintptr_t(0x20000));assert(source.bind(3,1)==0);VendorFixture::set(f.c()+0x48,uintptr_t(0));assert(source.check()==-ESTALE);
    VendorCacheSource unproved(f.symbols(),false);assert(!unproved.proved()&&unproved.bind(3,1)==-EACCES);
}
void expiryTests(){
    {VendorFixture f;VendorCacheSource source(f.symbols(),true);assert(!source.bind(3,1));source.revokeOwned(100);assert(f.gain()==0&&!source.pending());assert(source.safety().zeroStores==1&&source.safety().attempts==1);}
    {VendorFixture f;VendorCacheSource source(f.symbols(),true);source.revokeOwned(100);assert(f.gain()!=0&&source.safety().attempts==0);}
    {VendorFixture f;VendorCacheSource source(f.symbols(),true);assert(!source.bind(3,1));f.busy=1;source.revokeOwned(100);assert(source.pending()&&f.gain()!=0);source.maintenance(105);assert(source.safety().attempts==1);
        f.busy=0;source.maintenance(110);assert(!source.pending()&&f.gain()==0&&source.safety().zeroStores==1);}
    {VendorFixture f;VendorCacheSource source(f.symbols(),true);assert(!source.bind(3,1));f.busy=2;source.revokeOwned(100);for(int i=1;i<20;i++)source.maintenance(100+10*i);
        assert(!source.pending()&&source.safety().attempts==10&&source.safety().busyExhausted==1&&f.gain()!=0&&f.locks.empty());}
    {VendorFixture f;VendorCacheSource source(f.symbols(),true);assert(!source.bind(3,1));f.busy=1;source.revokeOwned(100);f.busy=0;
        auto other=reinterpret_cast<uintptr_t>(f.replacementVolume.data());VendorFixture::set(other,uintptr_t(0x40000));VendorFixture::set(other+0x1048,uint32_t(0x3f800000));VendorFixture::set(f.a()+0x38,other);
        source.maintenance(110);assert(!source.pending()&&source.safety().identityRefused==1&&f.gain()!=0);uint32_t gain;std::memcpy(&gain,f.replacementVolume.data()+0x1048,4);assert(gain!=0);}
    {VendorFixture f;VendorCacheSource source(f.symbols(),true);assert(!source.bind(3,1));f.busy=1;source.revokeOwned(100);f.busy=0;assert(!source.bind(3,1));
        source.maintenance(110);source.maintenance(1000);assert(!source.pending()&&source.safety().zeroStores==0&&f.gain()!=0);}
    {VendorFixture f;VendorCacheSource source(f.symbols(),true);assert(!source.bind(3,1));f.busy=1;source.revokeOwned(100);f.busy=0;source.maintenance(1000);
        assert(source.safety().busyExhausted==1&&source.safety().zeroStores==0&&f.gain()!=0);}
    {VendorFixture f;VendorCacheSource source(f.symbols(),true);assert(!source.bind(3,1));f.busy=1;source.revokeOwned(100);source.reset();source.abandonPending();
        assert(!source.pending()&&source.safety().busyExhausted==1&&f.gain()!=0);}
}
// Keep intentionally process-lived published strings reachable for LeakSanitizer.
const char* retainedCookies[16]{};unsigned retainedCookieCount=0;
void cookieTests(){
    alignas(16) std::array<char,64> rx{};std::memcpy(rx.data(),"1.1.3",6);uintptr_t original=uintptr_t(rx.data()),slot=original;
    MemoryRange code{original,rx.size()},data{uintptr_t(&slot),sizeof slot};
    InstanceCookie cookie;assert(!cookie.publish(&slot,code,data,0));assert(slot==original);
    assert(cookie.publish(&slot,code,data,UINT64_C(0x1122334455667788)));retainedCookies[retainedCookieCount++]=cookie.text();const char* borrowed=cookie.text();
    assert(!std::strcmp(borrowed,"1.1.3+friday.1122334455667788"));assert(slot==uintptr_t(borrowed));assert(cookie.cookie()==UINT64_C(0x1122334455667788));
    InstanceCookie other;assert(!other.publish(&slot,code,data,2));cookie.restore();assert(slot==original&&cookie.cookie()==0);assert(!std::strcmp(borrowed,"1.1.3+friday.1122334455667788"));
    assert(!other.publish(&slot,code,MemoryRange{uintptr_t(&slot)+1,sizeof slot},2));assert(!other.publish(&slot,MemoryRange{original,5},data,2));
    std::memcpy(rx.data(),"1.1.4",6);assert(!other.publish(&slot,code,data,2));std::memcpy(rx.data(),"1.1.3",6);
    const char outsider[]="1.1.3";slot=uintptr_t(outsider);assert(!other.publish(&slot,code,data,2));slot=original;
    assert(other.publish(&slot,code,data,2));retainedCookies[retainedCookieCount++]=other.text();const char* secondBorrow=other.text();slot=uintptr_t(outsider);other.restore();
    assert(slot==uintptr_t(outsider));assert(!std::strcmp(secondBorrow,"1.1.3+friday.0000000000000002"));slot=original;
    const char* afterUnload=nullptr;{InstanceCookie scoped;assert(scoped.publish(&slot,code,data,3));afterUnload=scoped.text();retainedCookies[retainedCookieCount++]=afterUnload;}
    assert(slot==original&&!std::strcmp(afterUnload,"1.1.3+friday.0000000000000003"));
    FridayCacheClient client=friday_cache_client_init();client.instance_cookie=7;assert(friday_cache_same_instance(&client,7));assert(!friday_cache_same_instance(&client,8));assert(!friday_cache_same_instance(&client,0));assert(client.sequence==0);
    alignas(16) std::array<char,64> secondRx{};std::memcpy(secondRx.data(),"1.1.3",6);uintptr_t secondSlot=uintptr_t(secondRx.data());
    InstanceCookie instanceA,instanceB;assert(instanceA.publish(&slot,code,data,7));
    assert(instanceB.publish(&secondSlot,MemoryRange{uintptr_t(secondRx.data()),secondRx.size()},MemoryRange{uintptr_t(&secondSlot),sizeof secondSlot},8));
    retainedCookies[retainedCookieCount++]=instanceA.text();retainedCookies[retainedCookieCount++]=instanceB.text();client.instance_cookie=instanceA.cookie();
    assert(!friday_cache_same_instance(&client,instanceB.cookie()));assert(std::strcmp(instanceA.text(),instanceB.text()));
}
void transportTests(){
    assert(friday_cache_remaining(100,-1)==-EIO);assert(friday_cache_remaining(100,100)==-ETIMEDOUT);assert(friday_cache_remaining(INT64_MAX,1)==250);assert(friday_cache_remaining(2,1)==1);
    {int64_t before=friday_cache_now_ms();int result=friday_cache_retry_pause(before+100);assert(result==0||result==-ETIMEDOUT);assert(friday_cache_now_ms()-before>=1&&friday_cache_now_ms()-before<200);assert(friday_cache_retry_pause(before)==-ETIMEDOUT);}
    assert(friday_cache_request_size_valid(64,0));assert(!friday_cache_request_size_valid(63,0));assert(!friday_cache_request_size_valid(65,0));assert(!friday_cache_request_size_valid(64,1));
    assert(!friday_cache_peer_values(1041,900,900));assert(friday_cache_peer_values(1041,900,901)==-EACCES);assert(friday_cache_peer_values(1000,900,0)==-EACCES);
    for(int kind:{SOCK_SEQPACKET,SOCK_DGRAM}){
        int pair[2];if(socketpair(AF_UNIX,kind,0,pair))continue;
        for(int fd:pair)assert(fcntl(fd,F_SETFL,fcntl(fd,F_GETFL)|O_NONBLOCK)==0);
        FridayCacheClient client{};client.fd=pair[0];client.role=2;client.server_pid=900;client.lease=77;client.generation=102;
        auto request=friday_cache_request(&client,FRIDAY_CACHE_READ);request.frames=128;
        std::thread server([&]{FridayCacheRequest r{};assert(!friday_cache_wait(pair[1],POLLIN,friday_cache_now_ms()+100));assert(recv(pair[1],&r,sizeof r,0)==sizeof r);
            FridayCachePacket p{};auto& h=p.header;h.magic=FRIDAY_CACHE_MAGIC;h.version=1;h.opcode=r.opcode;h.request_id=r.request_id;h.generation=r.generation;h.lease=r.lease;h.sequence=1;h.server_pid=900;h.capabilities=31;h.sample_rate=48000;h.channels=2;h.pcm_bits=16;h.frames=128;
            p.pcm[0]=123;assert(send(pair[1],&p,sizeof p,0)==sizeof p);friday_cache_wipe(&p,sizeof p);});
        FridayCachePacket reply{};assert(friday_cache_transport_until(&client,&request,&reply,friday_cache_now_ms()+100,900)==0);assert(reply.pcm[0]==123);server.join();
        close(pair[0]);close(pair[1]);break;
    }
    for(int bad=0;bad<10;bad++){
        int pair[2];assert(socketpair(AF_UNIX,SOCK_DGRAM,0,pair)==0);for(int fd:pair)assert(fcntl(fd,F_SETFL,fcntl(fd,F_GETFL)|O_NONBLOCK)==0);
        FridayCacheClient client{};client.fd=pair[0];client.role=2;client.server_pid=900;client.lease=77;client.generation=102;
        auto request=friday_cache_request(&client,FRIDAY_CACHE_READ);request.frames=128;
        std::thread server([&]{FridayCacheRequest r{};assert(!friday_cache_wait(pair[1],POLLIN,friday_cache_now_ms()+100));assert(recv(pair[1],&r,sizeof r,0)==sizeof r);
            struct {FridayCachePacket p;char extra[8];} wire{};auto& h=wire.p.header;h.magic=FRIDAY_CACHE_MAGIC;h.version=1;h.opcode=r.opcode;h.request_id=r.request_id;h.generation=r.generation;h.lease=r.lease;h.sequence=1;h.server_pid=900;h.capabilities=31;h.sample_rate=48000;h.channels=2;h.pcm_bits=16;h.frames=128;
            size_t bytes=sizeof wire.p;
            switch(bad){case 0:bytes=32;break;case 1:bytes++;break;case 2:h.magic=0;break;case 3:h.server_pid++;break;case 4:h.capabilities=15;break;case 5:h.generation++;break;case 6:h.sequence=0;break;case 7:h.frames=127;break;case 8:h.request_id++;break;case 9:h.status=1;break;}
            assert(send(pair[1],&wire,bytes,0)==ssize_t(bytes));friday_cache_wipe(&wire,sizeof wire);});
        FridayCachePacket reply{};int result=friday_cache_transport_until(&client,&request,&reply,friday_cache_now_ms()+100,900);
        assert(result==((bad==5||bad==6)?-ESTALE:-EPROTO));friday_cache_wipe(&reply,sizeof reply);server.join();close(pair[0]);close(pair[1]);
    }
    {int pair[2];assert(socketpair(AF_UNIX,SOCK_DGRAM,0,pair)==0);for(int fd:pair)assert(fcntl(fd,F_SETFL,fcntl(fd,F_GETFL)|O_NONBLOCK)==0);
        FridayCacheClient c{};c.fd=pair[0];auto r=friday_cache_request(&c,1);FridayCachePacket p{};int64_t before=friday_cache_now_ms();assert(friday_cache_transport_until(&c,&r,&p,before+20,900)==-ETIMEDOUT);assert(friday_cache_now_ms()-before<200);close(pair[0]);close(pair[1]);}
    for(uint64_t cookie:{UINT64_C(0),UINT64_C(7),UINT64_C(8)}){
        int pair[2];assert(socketpair(AF_UNIX,SOCK_DGRAM,0,pair)==0);for(int fd:pair)assert(fcntl(fd,F_SETFL,fcntl(fd,F_GETFL)|O_NONBLOCK)==0);
        FridayCacheClient client=friday_cache_client_init();client.fd=pair[0];client.role=2;client.server_pid=900;
        FridayCacheRequest request{};request.magic=FRIDAY_CACHE_MAGIC;request.version=1;request.opcode=1;request.request_id=1;request.flags=2;
        std::thread server([&]{FridayCacheRequest r{};assert(!friday_cache_wait(pair[1],POLLIN,friday_cache_now_ms()+100));assert(recv(pair[1],&r,sizeof r,0)==sizeof r);
            FridayCacheResponse p{};p.magic=FRIDAY_CACHE_MAGIC;p.version=1;p.opcode=1;p.request_id=1;p.server_pid=900;p.capabilities=31;p.sample_rate=48000;p.channels=2;p.pcm_bits=16;p.sequence=cookie;assert(send(pair[1],&p,sizeof p,0)==sizeof p);});
        FridayCachePacket reply{};int result=friday_cache_transport_until(&client,&request,&reply,friday_cache_now_ms()+100,900);
        assert(result==(cookie?0:-EPROTO));if(!result){client.instance_cookie=reply.header.sequence;assert(friday_cache_same_instance(&client,7)==(cookie==7));assert(client.sequence==0);}
        server.join();close(pair[0]);close(pair[1]);
    }
    // Darwin has no AF_UNIX SEQPACKET; a stream pair exercises the exact poll
    // backpressure/deadline primitive without claiming Android server execution.
    int full[2];assert(socketpair(AF_UNIX,SOCK_STREAM,0,full)==0);assert(fcntl(full[0],F_SETFL,fcntl(full[0],F_GETFL)|O_NONBLOCK)==0);
    char bytes[576]{};int packets=0;while(send(full[0],bytes,sizeof bytes,MSG_DONTWAIT|MSG_NOSIGNAL)>0){assert(++packets<100000);}
    assert(errno==EAGAIN||errno==EWOULDBLOCK);int64_t queued=friday_cache_now_ms();assert(friday_cache_wait(full[0],POLLOUT,queued+20)==-ETIMEDOUT);assert(friday_cache_now_ms()-queued<200);close(full[0]);close(full[1]);
    int pipefd[2];assert(pipe(pipefd)==0);int64_t before=friday_cache_now_ms();assert(friday_cache_wait(pipefd[0],POLLIN,before+20)==-ETIMEDOUT);assert(friday_cache_now_ms()-before<200);close(pipefd[0]);close(pipefd[1]);
    FridayCacheClient absent{};absent.fd=-1;absent.role=2;absent.generation=1;absent.lease=1;int16_t pcm[256];for(auto& v:pcm)v=123;
    assert(friday_cache_read128(&absent,pcm,nullptr,20)==-ENOTCONN);for(auto v:pcm)assert(!v);
}
int main(){
    assert(Sha256::matches("",0,"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"));
    assert(Sha256::matches("abc",3,"ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"));
    std::array<char,1000> data{};for(auto& c:data)c='a';assert(Sha256::matches(data.data(),data.size(),"41edece42d63e8d9bf515a9ba6932e1c20cbc9f5a5d134645adb5db1b9737ea3"));
    coreTests();vendorTests();expiryTests();cookieTests();transportTests();std::puts("strict cache: protocol, credentials, leases, stale/rebind, 100000 reads, mapped-source fakes, busy locks, owned-expiry zero lifecycle, instance-cookie CAS/borrowed lifetime/HELLO isolation, queue deadlines passed");
}
