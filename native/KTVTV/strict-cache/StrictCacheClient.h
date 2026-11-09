#ifndef FRIDAY_STRICT_CACHE_CLIENT_H
#define FRIDAY_STRICT_CACHE_CLIENT_H
/* Header-only C API. AF_UNIX SOCK_SEQPACKET is the only transport. No audio
 * constructors, device opens, library loads, file/network requests or fallback. */
#include "StrictCacheProtocol.h"
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>
#ifdef __cplusplus
extern "C" {
#endif
typedef struct FridayCacheClient {
    int fd,server_pid,server_uid,role;uint32_t capabilities,rate,channels,format;
    uint64_t generation,lease,request_id,sequence,instance_cookie;int card,device;
} FridayCacheClient;
static inline FridayCacheClient friday_cache_client_init(void){FridayCacheClient c;memset(&c,0,sizeof c);c.fd=-1;return c;}
static inline int friday_cache_same_instance(const FridayCacheClient* c,uint64_t expected_cookie){
    return c&&expected_cookie&&c->instance_cookie==expected_cookie;
}
static inline int64_t friday_cache_now_ms(void){struct timespec t;if(clock_gettime(CLOCK_MONOTONIC,&t))return -1;return (int64_t)t.tv_sec*1000+t.tv_nsec/1000000;}
static inline int friday_cache_deadline(int timeout_ms,int64_t* deadline){
    int64_t now=friday_cache_now_ms();if(now<0||now>INT64_MAX-250)return -EIO;if(timeout_ms<1||timeout_ms>250)return -EINVAL;*deadline=now+timeout_ms;return 0;
}
static inline int friday_cache_remaining(int64_t deadline,int64_t now){
    if(now<0)return -EIO;if(deadline<=now)return -ETIMEDOUT;int64_t left=deadline-now;return left>250?250:(int)left;
}
static inline int friday_cache_wait(int fd,short events,int64_t deadline){
    for(;;){int left=friday_cache_remaining(deadline,friday_cache_now_ms());if(left<0)return left;
        struct pollfd p={fd,events,0};int n=poll(&p,1,left);if(n<0){if(errno==EINTR)continue;return -errno;}
        if(!n)return -ETIMEDOUT;if(p.revents&(POLLERR|POLLHUP|POLLNVAL))return -ECONNRESET;if(p.revents&events)return 0;}
}
static inline void friday_cache_close(FridayCacheClient* c){if(!c)return;if(c->fd>=0&&(c->role==1||c->role==2))close(c->fd);memset(c,0,sizeof *c);c->fd=-1;}
static inline int friday_cache_peer_values(unsigned uid,int peer_pid,int expected_pid){
    return uid==FRIDAY_CACHE_SERVER_UID&&peer_pid>0&&(!expected_pid||peer_pid==expected_pid)?0:-EACCES;
}
static inline int friday_cache_peer(int fd,int expected_pid,int* pid){
#if defined(__linux__)
    struct {int pid;unsigned int uid,gid;} peer;socklen_t size=sizeof peer;
    if(getsockopt(fd,SOL_SOCKET,SO_PEERCRED,&peer,&size)||size!=sizeof peer)return -EACCES;
    if(friday_cache_peer_values(peer.uid,peer.pid,expected_pid))return -EACCES;
    *pid=peer.pid;return 0;
#else
    (void)fd;(void)expected_pid;(void)pid;return -ENOTSUP;
#endif
}
static inline FridayCacheRequest friday_cache_request(FridayCacheClient* c,int op){
    FridayCacheRequest r;memset(&r,0,sizeof r);r.magic=FRIDAY_CACHE_MAGIC;r.version=1;r.opcode=(uint16_t)op;r.request_id=++c->request_id;
    r.generation=c->generation;r.lease=c->lease;
    if(c->role==FRIDAY_CACHE_OWNER){r.owner_pid=getpid();r.card=c->card;r.device=c->device;}
    else r.reader_pid=getpid();return r;
}
/* Transport helper takes an ALREADY SO_PEERCRED-verified peer PID. Public RPC
 * entry always performs that kernel check; tests may exercise packet I/O on
 * socketpair with a synthetic credential separately. */
static inline int friday_cache_transport_until(FridayCacheClient* c,FridayCacheRequest* r,FridayCachePacket* reply,int64_t deadline,int peer_pid){
    memset(reply,0,sizeof *reply);int result=0;
    for(;;){int left=friday_cache_remaining(deadline,friday_cache_now_ms());if(left<0)return left;ssize_t n=send(c->fd,r,sizeof *r,MSG_DONTWAIT|MSG_NOSIGNAL);if(n==(ssize_t)sizeof *r)break;
        if(n>=0)return -EPROTO;if(errno==EINTR)continue;if(errno!=EAGAIN&&errno!=EWOULDBLOCK)return -errno;
        result=friday_cache_wait(c->fd,POLLOUT,deadline);if(result)return result;}
    for(;;){int left=friday_cache_remaining(deadline,friday_cache_now_ms());if(left<0)return left;struct iovec v={reply,sizeof *reply};struct msghdr m;memset(&m,0,sizeof m);m.msg_iov=&v;m.msg_iovlen=1;
        ssize_t n=recvmsg(c->fd,&m,MSG_DONTWAIT);if(n>=0){
            FridayCacheResponse* h=&reply->header;
            if((m.msg_flags&(MSG_TRUNC|MSG_CTRUNC))||n<(ssize_t)sizeof *h||h->magic!=FRIDAY_CACHE_MAGIC||h->version!=1
                ||h->opcode!=r->opcode||h->request_id!=r->request_id||h->server_pid!=peer_pid||h->reserved
                ||!friday_cache_capabilities_valid(h->capabilities)||(c->capabilities&&h->capabilities!=c->capabilities)||h->sample_rate!=48000||h->channels!=2||h->pcm_bits!=16
                ||h->status>0||h->status < -4095||h->frames!=(!h->status&&r->opcode==FRIDAY_CACHE_READ?128:0)
                ||n!=(ssize_t)(sizeof *h+(h->frames?FRIDAY_CACHE_PCM_BYTES:!h->status&&r->opcode==FRIDAY_CACHE_INLINE_STATUS?sizeof(FridayInlineMetrics):0)))return -EPROTO;
            if(!h->status&&r->opcode==FRIDAY_CACHE_READ&&(h->generation!=c->generation||h->lease!=c->lease||h->sequence<=c->sequence))return -ESTALE;
            if(!h->status&&r->opcode==FRIDAY_CACHE_HEARTBEAT&&h->generation!=c->generation)return -ESTALE;
            if(!h->status&&r->opcode>=FRIDAY_CACHE_INLINE_CONFIGURE&&h->generation!=c->generation)return -ESTALE;
            if(!h->status&&r->opcode==FRIDAY_CACHE_HELLO&&(h->generation||h->lease||!h->sequence))return -EPROTO;
            if(!h->status&&r->opcode!=FRIDAY_CACHE_HELLO&&r->opcode!=FRIDAY_CACHE_END&&(!h->generation||h->lease!=r->lease))return -EPROTO;
            if(!h->status&&r->opcode==FRIDAY_CACHE_INLINE_STATUS){
                FridayInlineMetrics* m=&reply->inline_metrics;
                if(h->capabilities!=FRIDAY_CACHE_INLINE_CAPABILITIES||m->requested_revision>0xfffff||m->applied_revision>0xfffff
                    ||m->profile<0||m->profile>6||m->wet<0||m->wet>360||m->monitor_gain<0||m->monitor_gain>1000
                    ||m->peak>32768||(m->flags&~UINT32_C(511))||m->fault<0||m->fault>31)return -EPROTO;
            }
            return h->status;
        }
        if(errno==EINTR)continue;if(errno!=EAGAIN&&errno!=EWOULDBLOCK)return -errno;
        result=friday_cache_wait(c->fd,POLLIN,deadline);if(result)return result;
    }
}
static inline int friday_cache_rpc_until(FridayCacheClient* c,FridayCacheRequest* r,FridayCachePacket* reply,int64_t deadline){
    memset(reply,0,sizeof *reply);if(!c||c->fd<0)return -ENOTCONN;int peer_pid=0,result=friday_cache_peer(c->fd,c->server_pid,&peer_pid);
    return result?result:friday_cache_transport_until(c,r,reply,deadline,peer_pid);
}
static inline int friday_cache_rpc(FridayCacheClient* c,FridayCacheRequest* r,FridayCachePacket* reply,int timeout_ms){
    int64_t deadline;int result=friday_cache_deadline(timeout_ms,&deadline);if(!result)result=friday_cache_rpc_until(c,r,reply,deadline);
    if(result&&result!=-EAGAIN&&result!=-ESTALE&&result!=-EBUSY)friday_cache_close(c);return result;
}
static inline int friday_cache_connect(FridayCacheClient* c,int role,int timeout_ms){
    if(!c)return -EINVAL;memset(c,0,sizeof *c);c->fd=-1;if(role!=1&&role!=2)return -EINVAL;
    int64_t deadline;int result=friday_cache_deadline(timeout_ms,&deadline);if(result)return result;
#if defined(__linux__)
    int fd=socket(AF_UNIX,SOCK_SEQPACKET|SOCK_NONBLOCK|SOCK_CLOEXEC,0);if(fd<0)return -errno;c->fd=fd;c->role=role;
    struct sockaddr_un a;memset(&a,0,sizeof a);a.sun_family=AF_UNIX;memcpy(a.sun_path+1,FRIDAY_CACHE_SOCKET,sizeof(FRIDAY_CACHE_SOCKET)-1);
    socklen_t size=(socklen_t)(offsetof(struct sockaddr_un,sun_path)+1+sizeof(FRIDAY_CACHE_SOCKET)-1);
    if(connect(fd,(struct sockaddr*)&a,size)){if(errno!=EINPROGRESS){result=-errno;friday_cache_close(c);return result;}
        result=friday_cache_wait(fd,POLLOUT,deadline);int error=0;socklen_t length=sizeof error;
        if(!result&&(getsockopt(fd,SOL_SOCKET,SO_ERROR,&error,&length)||error))result=error?-error:-errno;}
    if(!result)result=friday_cache_peer(fd,0,&c->server_pid);
    FridayCacheRequest r;memset(&r,0,sizeof r);r.magic=FRIDAY_CACHE_MAGIC;r.version=1;r.opcode=FRIDAY_CACHE_HELLO;r.request_id=++c->request_id;r.flags=(uint32_t)role;
    FridayCachePacket reply;if(!result)result=friday_cache_rpc_until(c,&r,&reply,deadline);
    if(result){friday_cache_wipe(&reply,sizeof reply);friday_cache_close(c);return result;}c->server_uid=FRIDAY_CACHE_SERVER_UID;c->capabilities=reply.header.capabilities;c->rate=reply.header.sample_rate;c->channels=reply.header.channels;c->format=1;c->instance_cookie=reply.header.sequence;friday_cache_wipe(&reply,sizeof reply);return 0;
#else
    (void)deadline;return -ENOTSUP;
#endif
}
static inline int friday_cache_begin(FridayCacheClient* c,uint64_t lease,int card,int device,uint64_t* generation,int timeout_ms){
    if(!c||c->role!=1||!lease||card<0||device<0||card>31||device>31)return -EINVAL;
    c->card=card;c->device=device;FridayCacheRequest r=friday_cache_request(c,FRIDAY_CACHE_BEGIN);r.generation=0;r.lease=lease;
    FridayCachePacket reply;int result=friday_cache_rpc(c,&r,&reply,timeout_ms);if(!result){c->lease=lease;c->generation=reply.header.generation;if(generation)*generation=c->generation;}friday_cache_wipe(&reply,sizeof reply);return result;
}
static inline int friday_cache_retry_pause(int64_t deadline){
    int64_t now=friday_cache_now_ms();int left=friday_cache_remaining(deadline,now);if(left<0)return left;
    const int64_t wake=now+1;
    for(;;){now=friday_cache_now_ms();left=friday_cache_remaining(deadline,now);if(left<0)return left;if(now>=wake)return 0;
        if(poll(NULL,0,1)<0&&errno!=EINTR)return -errno;}
}
/* Only initialization metadata retries. Each attempt keeps the original
 * monotonic deadline and a new request ID; no source READ is delayed here. */
static inline int friday_cache_initialise(FridayCacheClient* c,int op,uint64_t lease,int reader_pid,int timeout_ms){
    int64_t deadline;int result=friday_cache_deadline(timeout_ms,&deadline);if(result)return result;
    for(;;){FridayCacheRequest r=friday_cache_request(c,op);
        if(op==FRIDAY_CACHE_ATTACH){r.generation=0;r.lease=lease;}else r.reader_pid=reader_pid;
        FridayCachePacket reply;result=friday_cache_rpc_until(c,&r,&reply,deadline);
        if(!result){c->generation=reply.header.generation;c->lease=reply.header.lease;}
        friday_cache_wipe(&reply,sizeof reply);
        if(result!=-EAGAIN)break;
        result=friday_cache_retry_pause(deadline);if(result)break;
    }
    if(result&&result!=-ESTALE&&result!=-EBUSY)friday_cache_close(c);return result;
}
static inline int friday_cache_bind_reader(FridayCacheClient* c,int reader_pid,int timeout_ms){
    if(!c||c->role!=1||reader_pid<0)return -EINVAL;return friday_cache_initialise(c,FRIDAY_CACHE_BIND,c->lease,reader_pid,timeout_ms);
}
static inline int friday_cache_heartbeat(FridayCacheClient* c,int timeout_ms){
    if(!c||c->role!=1)return -EINVAL;FridayCacheRequest r=friday_cache_request(c,FRIDAY_CACHE_HEARTBEAT);FridayCachePacket reply;
    int result=friday_cache_rpc(c,&r,&reply,timeout_ms);friday_cache_wipe(&reply,sizeof reply);return result;
}
/* END is best effort, nonblocking and waits for no response. Close immediately
 * afterwards: owner disconnect independently revokes even if the queue was full. */
static inline int friday_cache_end(FridayCacheClient* c,int timeout_ms){
    (void)timeout_ms;if(!c||c->fd<0||c->role!=1)return -EINVAL;FridayCacheRequest r=friday_cache_request(c,FRIDAY_CACHE_END);
    ssize_t n=send(c->fd,&r,sizeof r,MSG_DONTWAIT|MSG_NOSIGNAL);int result=n==(ssize_t)sizeof r?0:-(n<0?errno:EPROTO);friday_cache_close(c);return result;
}
static inline int friday_cache_attach(FridayCacheClient* c,uint64_t lease,int timeout_ms){
    if(!c||c->role!=2||!lease)return -EINVAL;return friday_cache_initialise(c,FRIDAY_CACHE_ATTACH,lease,0,timeout_ms);
}
static inline int friday_cache_read128(FridayCacheClient* c,int16_t stereo[256],uint64_t* sequence,int timeout_ms){
    if(!stereo)return -EINVAL;friday_cache_wipe(stereo,512);if(sequence)*sequence=0;
    if(!c||c->role!=2||!c->generation||!c->lease)return -ESTALE;
    FridayCacheRequest r=friday_cache_request(c,FRIDAY_CACHE_READ);r.frames=128;FridayCachePacket reply;
    int result=friday_cache_rpc(c,&r,&reply,timeout_ms);if(!result){memcpy(stereo,reply.pcm,512);c->sequence=reply.header.sequence;if(sequence)*sequence=c->sequence;}
    friday_cache_wipe(&reply,sizeof reply);return result;
}
static inline int friday_cache_inline_configure(FridayCacheClient* c,int profile,int wet,int gain,int muted,int bypass,int timeout_ms){
    if(!c||c->role!=FRIDAY_CACHE_READER||c->capabilities!=FRIDAY_CACHE_INLINE_CAPABILITIES||!c->generation||!c->lease
        ||profile<0||profile>6||wet<0||wet>360||gain<0||gain>1000||(muted!=0&&muted!=1)||(bypass!=0&&bypass!=1))return -EINVAL;
    FridayCacheRequest r=friday_cache_request(c,FRIDAY_CACHE_INLINE_CONFIGURE);
    r.flags=(uint32_t)profile|((uint32_t)wet<<3)|((uint32_t)gain<<12)|((uint32_t)muted<<22)|((uint32_t)bypass<<23);
    FridayCachePacket reply;int result=friday_cache_rpc(c,&r,&reply,timeout_ms);friday_cache_wipe(&reply,sizeof reply);return result;
}
static inline int friday_cache_inline_status(FridayCacheClient* c,FridayInlineMetrics* metrics,int timeout_ms){
    if(!metrics)return -EINVAL;memset(metrics,0,sizeof *metrics);
    if(!c||c->capabilities!=FRIDAY_CACHE_INLINE_CAPABILITIES||!c->generation||!c->lease)return -EINVAL;
    FridayCacheRequest r=friday_cache_request(c,FRIDAY_CACHE_INLINE_STATUS);FridayCachePacket reply;
    int result=friday_cache_rpc(c,&r,&reply,timeout_ms);if(!result)memcpy(metrics,&reply.inline_metrics,sizeof *metrics);
    friday_cache_wipe(&reply,sizeof reply);return result;
}
static inline int friday_cache_inline_mute(FridayCacheClient* c,int muted,int timeout_ms){
    if(!c||c->role!=FRIDAY_CACHE_OWNER||c->capabilities!=FRIDAY_CACHE_INLINE_CAPABILITIES||!c->generation||!c->lease||(muted!=0&&muted!=1))return -EINVAL;
    FridayCacheRequest r=friday_cache_request(c,FRIDAY_CACHE_INLINE_MUTE);r.flags=(uint32_t)muted;FridayCachePacket reply;
    int result=friday_cache_rpc(c,&r,&reply,timeout_ms);friday_cache_wipe(&reply,sizeof reply);return result;
}
static inline int friday_cache_inline_notch_until(FridayCacheClient* c,int slot,int hz,int cut,int64_t deadline){
    if(!c||c->role!=FRIDAY_CACHE_OWNER||c->capabilities!=FRIDAY_CACHE_INLINE_CAPABILITIES||!c->generation||!c->lease
        ||slot<0||slot>7||hz<188||hz>6492||cut<0||cut>12)return -EINVAL;
    FridayCacheRequest r=friday_cache_request(c,FRIDAY_CACHE_INLINE_NOTCH);r.flags=(uint32_t)slot|((uint32_t)hz<<3)|((uint32_t)cut<<16);
    FridayCachePacket reply;int result=friday_cache_rpc_until(c,&r,&reply,deadline);
    friday_cache_wipe(&reply,sizeof reply);if(result&&result!=-EAGAIN&&result!=-ESTALE&&result!=-EBUSY)friday_cache_close(c);return result;
}
#ifdef __cplusplus
}
#endif
#endif
