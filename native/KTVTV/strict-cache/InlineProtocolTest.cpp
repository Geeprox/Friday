#include "StrictCacheCore.h"
#include "StrictCacheClient.h"
#include "../android/cpp/wet/InlineVoiceKernel.h"
#include <array>
#include <cassert>
#include <iostream>
using namespace fridaystrict;
struct InlineSource final:Source {
    fridaywet::InlineVoiceKernel kernel;bool busy=false;int lost=0,prepared=0,revoked=0,inlineError=0,inlineCalls=0;
    bool proved()const override{return true;}
    uint32_t capabilities()const override{return 63;}
    int prepareOwner(int,int)override{if(busy)return -EBUSY;++prepared;return 0;}
    int bind(int,int)override{return 0;}
    int check()override{return 0;}
    int read128(int16_t* p)override{for(int n=0;n<256;n++)p[n]=int16_t(n);return 0;}
    void reset()override{kernel.ownerMute(true);}
    void revokeOwned(uint64_t)override{++revoked;reset();}
    void rebindForOwner(uint64_t)override{kernel.ownerMute(true);}
    void readerLost()override{++lost;kernel.ownerMute(true);}
    int configureInline(int p,int w,int g,bool m,bool b)override{++inlineCalls;if(inlineError)return inlineError;return kernel.configure(p,w,g,m,b)?0:-EINVAL;}
    int muteInline(bool muted)override{++inlineCalls;if(inlineError)return inlineError;kernel.ownerMute(muted);return 0;}
    int notchInline(int s,int hz,int cut)override{++inlineCalls;if(inlineError)return inlineError;return kernel.notch(s,hz,cut)?0:-EINVAL;}
    int inlineStatus(FridayInlineMetrics& out)override{
        ++inlineCalls;if(inlineError)return inlineError;
        const auto s=kernel.snapshot();out={s.requestedRevision,s.appliedRevision,s.callbacks,s.frames,s.profile,s.wet,s.monitorGain,s.peak,uint32_t(77|(s.muted?16:0)|(s.bypass?32:0)),0};return 0;
    }
};
void inlineSourceFaults(){
    for(int opcode:{8,9,10,11}){
        InlineSource source;Core core({10001,10002},source,900,10,123);Peer owner{1,0,0,10001,100},reader{2,0,0,10002,200};
        FridayCachePacket packet{};uint64_t now=1;
        auto request=[&](Peer& peer,int op){FridayCacheRequest r{};r.magic=FRIDAY_CACHE_MAGIC;r.version=1;r.opcode=uint16_t(op);r.request_id=peer.lastRequest+1;
            if(op==1){r.flags=peer.uid==10001?1:2;return r;}
            r.lease=77;r.generation=(op==2||op==7)?0:core.generation();
            if(peer.role==1){r.owner_pid=peer.pid;r.card=3;r.device=1;}else r.reader_pid=peer.pid;
            if(op==8)r.flags=700<<12;else if(op==10)r.flags=1;else if(op==11)r.flags=(1000<<3)|(6<<16);return r;};
        auto send=[&](Peer& peer,FridayCacheRequest r){return core.handle(peer,r,now,packet);};
        send(owner,request(owner,1));send(reader,request(reader,1));send(owner,request(owner,2));send(owner,request(owner,3));send(reader,request(reader,7));
        send(reader,request(reader,8));assert(packet.header.status==0);
        for(int n=0;n<16;n++){auto r=request(reader,6);r.frames=128;send(reader,r);assert(packet.header.status==0);}
        auto open=request(owner,10);open.flags=0;send(owner,open);assert(packet.header.status==0);
        std::array<int16_t,256> pcm{};for(int n=0;n<8;n++){pcm.fill(6000);source.kernel.process(pcm.data(),128);}
        assert(pcm[0]!=0);
        auto& peer=opcode==8?reader:owner;
        const auto generation=core.generation();
        source.inlineError=-ESTALE;
        auto old=request(peer,opcode);old.generation--;
        const auto calls=source.inlineCalls;send(peer,old);
        assert(packet.header.status==-ESTALE&&core.generation()==generation&&source.revoked==0&&source.inlineCalls==calls);
        source.inlineError=-EAGAIN;send(peer,request(peer,opcode));
        assert(packet.header.status==-EAGAIN&&core.generation()==generation&&source.revoked==0);
        source.inlineError=-ESTALE;send(peer,request(peer,opcode));
        assert(packet.header.status==-ESTALE&&core.generation()==0&&source.revoked==1&&packet.header.frames==0);
        for(auto sample:packet.pcm)assert(sample==0);
        pcm.fill(6000);source.kernel.process(pcm.data(),128);for(auto sample:pcm)assert(sample==0);
    }
}
int main(){
    inlineSourceFaults();
    InlineSource source;Core core({10001,10002},source,900,10,123);Peer owner{1,0,0,10001,100},reader{2,0,0,10002,200};
    FridayCachePacket packet{};uint64_t now=1;
    auto request=[&](Peer& peer,int operation){FridayCacheRequest r{};r.magic=FRIDAY_CACHE_MAGIC;r.version=1;r.opcode=uint16_t(operation);r.request_id=peer.lastRequest+1;
        if(operation==1){r.flags=peer.uid==10001?1:2;return r;}
        r.lease=77;r.generation=core.generation();if(operation==2||operation==7)r.generation=0;
        if(peer.role==1){r.owner_pid=peer.pid;r.card=3;r.device=1;}else r.reader_pid=peer.pid;return r;};
    auto send=[&](Peer& peer,FridayCacheRequest r){return core.handle(peer,r,now,packet);};
    send(owner,request(owner,1));assert(packet.header.status==0&&packet.header.capabilities==63);
    send(reader,request(reader,1));assert(packet.header.status==0);
    source.busy=true;send(owner,request(owner,2));assert(packet.header.status==-EBUSY&&core.generation()==0&&source.prepared==0);
    source.busy=false;send(owner,request(owner,2));assert(packet.header.status==0&&source.prepared==1);
    send(owner,request(owner,3));assert(packet.header.status==0);
    auto config=request(reader,8);config.flags=1|(250<<3)|(700<<12);
    send(reader,config);assert(packet.header.status==-ESTALE);
    send(reader,request(reader,7));assert(packet.header.status==0);
    auto warm=[&]{for(int i=0;i<16;i++){auto r=request(reader,6);r.frames=128;send(reader,r);assert(packet.header.status==0);}};
    config=request(reader,8);config.flags=1|(250<<3)|(700<<12);send(reader,config);assert(packet.header.status==0);
    std::array<int16_t,256> pcm{};pcm.fill(6000);source.kernel.process(pcm.data(),128);for(auto n:pcm)assert(n==0);
    const auto bytes=send(reader,request(reader,9));assert(bytes==64+56&&packet.header.frames==0&&packet.inline_metrics.profile==1);
    assert(packet.inline_metrics.flags&16); // Configure cannot open owner gate.
    auto readerMute=request(reader,10);readerMute.flags=0;send(reader,readerMute);assert(packet.header.status==-ESTALE);
    auto ownerConfig=request(owner,8);ownerConfig.flags=1;send(owner,ownerConfig);assert(packet.header.status==-ESTALE);
    auto cold=request(owner,10);cold.flags=0;send(owner,cold);assert(packet.header.status==-ESTALE);
    warm();
    now+=251;
    auto aged=request(owner,10);aged.flags=0;send(owner,aged);assert(packet.header.status==-ESTALE);
    warm();
    auto open=request(owner,10);open.flags=0;send(owner,open);assert(packet.header.status==0);
    for(int n=0;n<6;n++){pcm.fill(6000);source.kernel.process(pcm.data(),128);}
    bool audible=false;for(auto n:pcm)audible|=n!=0;assert(audible);
    send(reader,request(reader,9));assert(packet.inline_metrics.applied_revision==packet.inline_metrics.requested_revision);
    auto notch=request(owner,11);notch.flags=1000<<3|6<<16;send(owner,notch);assert(packet.header.status==0);
    auto forbidden=request(reader,11);forbidden.flags=notch.flags;send(reader,forbidden);assert(packet.header.status==-ESTALE);
    for(uint32_t invalid:{7U,(361U<<3),(1001U<<12),(1U<<31)}){
        auto bad=request(reader,8);bad.flags=invalid;send(reader,bad);assert(packet.header.status==-EINVAL);
    }
    const auto generation=core.generation();auto stale=request(owner,10);stale.generation--;stale.flags=0;send(owner,stale);assert(packet.header.status==-ESTALE&&core.generation()==generation);
    core.disconnected(reader);assert(source.lost==1);pcm.fill(6000);source.kernel.process(pcm.data(),128);for(auto n:pcm)assert(n==0);
    auto missing=request(owner,10);missing.flags=0;send(owner,missing);assert(packet.header.status==-ESTALE);
    send(owner,request(owner,3));assert(packet.header.status==0&&core.generation()!=generation);
    send(reader,request(reader,7));assert(packet.header.status==0);
    warm();
    config=request(reader,8);config.flags=0|(700<<12);send(reader,config);assert(packet.header.status==0);
    pcm.fill(6000);source.kernel.process(pcm.data(),128);for(auto n:pcm)assert(n==0);
    open=request(owner,10);open.flags=0;send(owner,open);assert(packet.header.status==0);
    now+=750;send(reader,request(reader,9));assert(packet.header.status==-ESTALE&&core.generation()==0);
    pcm.fill(6000);source.kernel.process(pcm.data(),128);for(auto n:pcm)assert(n==0);
    std::cout<<"inline wire/core + real kernel: authorization, source-fault immediate revoke, busy/stale request isolation, closed payload, callback ACK, mute, rebind and expiry passed\n";
}
