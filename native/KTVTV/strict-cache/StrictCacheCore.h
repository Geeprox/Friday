#ifndef FRIDAY_STRICT_CACHE_CORE_H
#define FRIDAY_STRICT_CACHE_CORE_H
#include "StrictCacheProtocol.h"
#include <cerrno>
#include <cstdint>
#include <cstring>
namespace fridaystrict {
struct Config {uint32_t ownerUid=0,readerUid=0;};
struct Peer {uint64_t id=0,lastRequest=0,attached=0;uint32_t uid=0;int32_t pid=0;int role=0;};
/* All calls are serialized by the host. Implementations must never start/open a
 * source, wait for PCM, or retain PCM. reset only forgets the binding; the
 * revokeOwned hook may implement only identity-checked one-way safety zero. */
struct Source {
    virtual ~Source()=default;
    virtual bool proved()const=0;
    virtual uint32_t capabilities()const{return FRIDAY_CACHE_CAPABILITIES;}
    virtual int prepareOwner(int,int){return 0;}
    virtual int bind(int card,int device)=0;
    virtual int check()=0;
    virtual int read128(int16_t* stereo)=0;
    virtual void reset()=0;
    virtual void revokeOwned(uint64_t){reset();}
    virtual void rebindForOwner(uint64_t now){revokeOwned(now);}
    virtual void maintenance(uint64_t){}
    virtual int configureInline(int,int,int,bool,bool){return -ENOTSUP;}
    virtual int muteInline(bool){return -ENOTSUP;}
    virtual int notchInline(int,int,int){return -ENOTSUP;}
    virtual int inlineStatus(FridayInlineMetrics&){return -ENOTSUP;}
    virtual void readerLost(){}
};
class Core {
    Config config_;Source& source_;int32_t pid_;uint64_t cookie_;
    uint64_t counter_,generation_=0,lease_=0,owner_=0,reader_=0,lastBeat_=0,sequence_=0,readerFrames_=0,lastRead_=0;
    int32_t ownerPid_=0,readerPid_=0,card_=0,device_=0;bool bound_=false;
    void revoke(uint64_t now){if(bound_)source_.revokeOwned(now);else source_.reset();owner_=reader_=generation_=lease_=lastBeat_=readerFrames_=lastRead_=0;ownerPid_=readerPid_=0;bound_=false;}
    uint64_t next(){if(++counter_==0)++counter_;return counter_;}
    bool owner(const Peer& p,const FridayCacheRequest& r)const {
        return p.role==FRIDAY_CACHE_OWNER && owner_==p.id && ownerPid_==p.pid
            &&r.owner_pid==p.pid&&r.generation==generation_&&r.lease==lease_&&r.card==card_&&r.device==device_;
    }
    bool reader(const Peer& p,const FridayCacheRequest& r)const{
        return p.role==FRIDAY_CACHE_READER&&owner_&&bound_&&reader_==p.id&&readerPid_==p.pid&&p.attached==generation_
            &&!r.owner_pid&&!r.card&&!r.device&&r.reader_pid==p.pid&&r.generation==generation_&&r.lease==lease_;
    }
public:
    Core(Config c,Source& s,int32_t serverPid,uint64_t seed,uint64_t instanceCookie):config_(c),source_(s),pid_(serverPid),cookie_(instanceCookie),counter_(seed){}
    void expire(uint64_t now){if(owner_&&(now<lastBeat_||now-lastBeat_>=FRIDAY_CACHE_OWNER_TTL_MS))revoke(now);}
    void disconnected(const Peer& p,uint64_t now=0){if(owner_==p.id)revoke(now);else if(reader_==p.id){reader_=readerFrames_=lastRead_=0;source_.readerLost();}}
    void maintenance(uint64_t now){expire(now);source_.maintenance(now);}
    uint64_t generation()const{return generation_;}
    size_t handle(Peer& p,const FridayCacheRequest& r,uint64_t now,FridayCachePacket& packet){
        std::memset(&packet,0,sizeof packet);auto& h=packet.header;
        h.magic=FRIDAY_CACHE_MAGIC;h.version=1;h.opcode=r.opcode;h.request_id=r.request_id;h.server_pid=pid_;
        h.capabilities=source_.proved()&&cookie_?source_.capabilities():0;h.sample_rate=48000;h.channels=2;h.pcm_bits=16;
        expire(now);
        int status=0;bool sourceFault=false;
        if(r.magic!=FRIDAY_CACHE_MAGIC||r.version!=1||r.reserved||!r.request_id||r.request_id<=p.lastRequest
                ||r.opcode<FRIDAY_CACHE_HELLO||r.opcode>FRIDAY_CACHE_INLINE_NOTCH)status=-EPROTO;
        else p.lastRequest=r.request_id;
        if(!status&&(!source_.proved()||!cookie_))status=-EACCES;
        if(!status&&r.opcode==FRIDAY_CACHE_HELLO){
            if(p.role||r.generation||r.lease||r.owner_pid||r.card||r.device||r.reader_pid||r.frames
                    ||(r.flags!=FRIDAY_CACHE_OWNER&&r.flags!=FRIDAY_CACHE_READER))status=-EPROTO;
            else if((r.flags==FRIDAY_CACHE_OWNER&&p.uid!=config_.ownerUid)
                    ||(r.flags==FRIDAY_CACHE_READER&&p.uid!=config_.readerUid)||p.pid<=0)status=-EACCES;
            else p.role=int(r.flags);
        }else if(!status){
            const bool inlineFlags=r.opcode==FRIDAY_CACHE_INLINE_CONFIGURE||r.opcode==FRIDAY_CACHE_INLINE_MUTE||r.opcode==FRIDAY_CACHE_INLINE_NOTCH;
            if(!p.role||(!inlineFlags&&r.flags)||r.frames!=(r.opcode==FRIDAY_CACHE_READ?FRIDAY_CACHE_FRAMES:0))status=-EPROTO;
            else if(r.opcode==FRIDAY_CACHE_BEGIN){
                if(p.role!=FRIDAY_CACHE_OWNER||r.owner_pid!=p.pid||r.generation||!r.lease||r.reader_pid||r.card<0||r.device<0||r.card>31||r.device>31)status=-EACCES;
                else if(owner_&&owner_!=p.id)status=-EBUSY;
                else if((status=source_.prepareOwner(r.card,r.device))==0){revoke(now);owner_=p.id;ownerPid_=p.pid;lease_=r.lease;generation_=next();card_=r.card;device_=r.device;lastBeat_=now;}
            }else if(r.opcode==FRIDAY_CACHE_BIND||r.opcode==FRIDAY_CACHE_HEARTBEAT||r.opcode==FRIDAY_CACHE_END){
                if(!owner(p,r))status=-ESTALE;
                else if(r.opcode==FRIDAY_CACHE_END){if(r.reader_pid)status=-EPROTO;else revoke(now);}
                else if(r.opcode==FRIDAY_CACHE_BIND){
                    if(r.reader_pid<0)status=-EPROTO;
                    else {reader_=readerFrames_=lastRead_=0;readerPid_=0;if(bound_)source_.rebindForOwner(now);else source_.reset();bound_=false;status=source_.bind(card_,device_);sourceFault=status==-ESTALE;
                        if(!status){readerPid_=r.reader_pid;bound_=true;generation_=next();lastBeat_=now;}}
                }else if(r.reader_pid)status=-EPROTO;
                else {status=bound_?source_.check():0;
                    // This proves owner liveness, not PCM readiness. A busy
                    // trylock must not expire an authenticated current owner;
                    // READ still reports EAGAIN with zero frames/sequence.
                    if(status==-EAGAIN)status=0;
                    sourceFault=status==-ESTALE;if(!status)lastBeat_=now;}
            }else if(r.opcode==FRIDAY_CACHE_ATTACH){
                if(p.role!=FRIDAY_CACHE_READER||r.owner_pid||r.card||r.device||r.frames||r.generation||r.reader_pid!=p.pid)status=-EPROTO;
                else if(!owner_||!bound_||r.lease!=lease_)status=-ESTALE;
                else if((reader_&&reader_!=p.id)||(readerPid_&&readerPid_!=p.pid))status=-EBUSY;
                else {status=source_.check();sourceFault=status==-ESTALE;if(!status){reader_=p.id;readerPid_=p.pid;p.attached=generation_;readerFrames_=lastRead_=0;}}
            }else if(r.opcode==FRIDAY_CACHE_READ){
                if(p.role!=FRIDAY_CACHE_READER||r.owner_pid||r.card||r.device||r.reader_pid!=p.pid)status=-EPROTO;
                else if(!owner_||!bound_||reader_!=p.id||readerPid_!=p.pid||p.attached!=generation_
                        ||r.generation!=generation_||r.lease!=lease_)status=-ESTALE;
                else {status=source_.read128(packet.pcm);sourceFault=status==-ESTALE;if(!status){h.frames=128;h.sequence=++sequence_;lastRead_=now;if(readerFrames_<=UINT64_MAX-128)readerFrames_+=128;}}
            }else if(r.opcode>=FRIDAY_CACHE_INLINE_CONFIGURE){
                if(h.capabilities!=FRIDAY_CACHE_INLINE_CAPABILITIES)status=-ENOTSUP;
                else if(r.opcode==FRIDAY_CACHE_INLINE_CONFIGURE){
                    const int profile=r.flags&7,wet=(r.flags>>3)&511,gain=(r.flags>>12)&1023;
                    if(!reader(p,r))status=-ESTALE;
                    else if((r.flags>>24)||profile>6||wet>360||gain>1000)status=-EINVAL;
                    else {status=source_.configureInline(profile,wet,gain,(r.flags>>22)&1,(r.flags>>23)&1);sourceFault=status==-ESTALE;}
                }else if(r.opcode==FRIDAY_CACHE_INLINE_MUTE){
                    if(!owner(p,r)||!bound_||r.reader_pid)status=-ESTALE;
                    else if(r.flags>1)status=-EINVAL;
                    else if(!r.flags&&(!reader_||readerPid_<=0||readerFrames_<2048||!lastRead_||now<lastRead_||now-lastRead_>250))status=-ESTALE;
                    else {status=source_.muteInline(r.flags!=0);sourceFault=status==-ESTALE;}
                }else if(r.opcode==FRIDAY_CACHE_INLINE_NOTCH){
                    const int slot=r.flags&7,hz=(r.flags>>3)&8191,cut=(r.flags>>16)&15;
                    if(!owner(p,r)||!bound_||r.reader_pid)status=-ESTALE;
                    else if((r.flags>>20)||hz<188||hz>6492||cut>12)status=-EINVAL;
                    else {status=source_.notchInline(slot,hz,cut);sourceFault=status==-ESTALE;}
                }else if(r.opcode==FRIDAY_CACHE_INLINE_STATUS){
                    if((!(owner(p,r)&&!r.reader_pid)&&!reader(p,r))||!bound_)status=-ESTALE;
                    else {status=source_.inlineStatus(packet.inline_metrics);sourceFault=status==-ESTALE;}
                }
            }else status=-EPROTO;
        }
        if(sourceFault)revoke(now);
        h.status=status;
        if(!status&&r.opcode==FRIDAY_CACHE_HELLO)h.sequence=cookie_;
        /* HELLO reveals capabilities only, and never leases or reserves ownership. */
        if(!status&&p.role&&r.opcode!=FRIDAY_CACHE_HELLO){h.generation=generation_;h.lease=lease_;}
        if(status){h.frames=0;friday_cache_wipe(packet.pcm,sizeof packet.pcm);}
        return sizeof h+(h.frames?sizeof packet.pcm:!status&&r.opcode==FRIDAY_CACHE_INLINE_STATUS?sizeof packet.inline_metrics:0);
    }
};
}
#endif
