/* Android audioserver/HAL-hosted DSO. Loading it in an APK does not grant access
 * to the audioserver's manager: UID, secure config and mapped image gates fail. */
#include "StrictCacheCore.h"
#include "StrictCacheConfig.h"
#include "VendorCacheSource.h"
#include "Sha256.h"
#include "InstanceCookie.h"
#include "VendorPayloadProfile.h"
#include <array>
#include <atomic>
#include <cerrno>
#include <cstdlib>
#include <cstring>
#include <dlfcn.h>
#include <elf.h>
#include <fcntl.h>
#include <link.h>
#include <limits.h>
#include <new>
#include <poll.h>
#include <pthread.h>
#include <sys/random.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>
#if !defined(__ANDROID__) || !defined(__aarch64__)
#error The platform host is pinned to the inspected Android arm64 GlobalMic layout
#endif

namespace {
using namespace fridaystrict;
constexpr const char* MappedRxSha="618a2f2d4a60b17d0d3e52167d564f8064a2129314a97efffbce5273f30c4ef8";
constexpr size_t RxBytes=614408;
uint64_t nowMs(){timespec t{};if(clock_gettime(CLOCK_MONOTONIC,&t))return 0;return uint64_t(t.tv_sec)*1000+t.tv_nsec/1000000;}
bool secure(const struct stat& s,mode_t mode,bool directory){
    return (directory?S_ISDIR(s.st_mode):S_ISREG(s.st_mode))&&configAuthority(s.st_uid,s.st_mode,s.st_nlink,directory)&&(s.st_mode&07777)==mode;
}
int config(Config& c){
    int dir=open("/data/vendor/friday-ktv",O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC);if(dir<0)return -errno;
    struct stat st{};if(fstat(dir,&st)||!secure(st,0700,true)){close(dir);return -EACCES;}
    int file=openat(dir,"cache-clients.conf",O_RDONLY|O_NOFOLLOW|O_CLOEXEC);close(dir);if(file<0)return -errno;
    if(fstat(file,&st)||!secure(st,0600,false)||st.st_size<=0||st.st_size>256){close(file);return -EACCES;}
    char text[258]{};ssize_t n=read(file,text,257);struct stat after{};bool unchanged=fstat(file,&after)==0&&after.st_ino==st.st_ino&&after.st_size==st.st_size&&after.st_mtime==st.st_mtime&&after.st_ctime==st.st_ctime;close(file);
    if(!unchanged||n!=st.st_size||n>256||std::memchr(text,0,size_t(n)))return -EACCES;
    return parseConfig(text,size_t(n),c);
}
bool writableSlot(uintptr_t slot,size_t bytes){
    int fd=open("/proc/self/maps",O_RDONLY|O_CLOEXEC);if(fd<0)return false;
    char line[192]{},chunk[1024]{};size_t used=0;bool found=false;
    auto inspect=[&]{line[used]=0;char* next=nullptr;errno=0;unsigned long long begin=strtoull(line,&next,16);if(errno||next==line||*next!='-')return;
        char* end=nullptr;unsigned long long limit=strtoull(next+1,&end,16);if(errno||end==next+1)return;while(*end==' ')end++;
        if(begin<=slot&&limit>=slot&&bytes<=limit-slot&&end[0]=='r'&&end[1]=='w'&&end[2]=='-'&&end[3]=='p')found=true;};
    for(;;){ssize_t n=read(fd,chunk,sizeof chunk);if(n<0&&errno==EINTR)continue;if(n<=0)break;
        for(ssize_t i=0;i<n;i++){if(chunk[i]=='\n'){inspect();used=0;}else if(used<sizeof line-1)line[used++]=chunk[i];}}
    if(used)inspect();close(fd);return found;
}
struct Image {char path[PATH_MAX]{};uintptr_t base=0;MemoryRange writable{};int found=0;bool valid=false;bool payload=false;};
int findPath(dl_phdr_info* info,size_t,void* context){
    const char* path=info->dlpi_name;if(!path||!*path||strnlen(path,PATH_MAX)>=PATH_MAX)return 0;
    if((std::strncmp(path,"/vendor/",8)&&std::strncmp(path,"/system/",8))||std::strstr(path,"/../")||std::strstr(path,"/./")||std::strstr(path,"//"))return 0;
    auto& image=*static_cast<Image*>(context);
    const char* name=std::strrchr(path,'/');if(!name||std::strcmp(name+1,image.payload?PayloadName:"libtlkg.hal.globalmic.so"))return 0;
    if(image.payload&&std::strcmp(path,PayloadPath))return 0;
    image.found++;std::strcpy(image.path,path);return 0;
}
int findImage(dl_phdr_info* info,size_t,void* context){
    auto& image=*static_cast<Image*>(context);if(!info->dlpi_name||std::strcmp(info->dlpi_name,image.path))return 0;
    image.found++;bool rx=false,rw=false;uintptr_t slot=uintptr_t(info->dlpi_addr)+0x9e1d8;
    for(int i=0;i<info->dlpi_phnum;i++){const auto& p=info->dlpi_phdr[i];if(p.p_type==PT_LOAD&&(p.p_flags&PF_X)){
        if(rx||p.p_flags!=(PF_R|PF_X)||p.p_offset!=0||p.p_vaddr!=0||p.p_filesz!=RxBytes||p.p_memsz!=RxBytes)return 0;rx=true;
    }
        if(p.p_type==PT_LOAD&&p.p_flags==(PF_R|PF_W)){
            MemoryRange range{uintptr_t(info->dlpi_addr+p.p_vaddr),size_t(p.p_memsz)};
            if(range.contains(slot,sizeof(uintptr_t))){if(rw)return 0;rw=true;image.writable=range;}
        }
    }
    if(!rx||!rw)return 0;image.base=uintptr_t(info->dlpi_addr);
    uint32_t instruction=0;std::memcpy(&instruction,reinterpret_cast<void*>(image.base+0x39a54),4);
    image.valid=instruction==0x1e2703e3&&Sha256::matches(reinterpret_cast<void*>(image.base),RxBytes,image.payload?PayloadRxSha:MappedRxSha);return 0;
}
struct Loaded {
    void* handle=nullptr;VendorSymbols symbols{};InstanceCookie instance{};MemoryRange rx{},writable{};uintptr_t versionSlot=0;
    ~Loaded(){instance.restore();if(handle)dlclose(handle);}
    bool open(bool payload){
        /* NOLOAD is mandatory: never create an app-local/new GlobalMic manager. */
        Image image;image.payload=payload;dl_iterate_phdr(findPath,&image);if(image.found!=1)return false;
        handle=dlopen(image.path,RTLD_NOW|RTLD_LOCAL|RTLD_NOLOAD);if(!handle)return false;
        /* Pin first, then re-enumerate and hash the mapping. This avoids hashing
         * an image that another HAL thread could unload between enumeration and
         * NOLOAD. No guessed alternate path or fresh dlopen is attempted. */
        image.found=0;dl_iterate_phdr(findImage,&image);if(image.found!=1||!image.valid)return false;
        rx={image.base,RxBytes};writable=image.writable;versionSlot=image.base+0x9e1d8;
        if(!writableSlot(versionSlot,sizeof(uintptr_t)))return false;
        auto exact=[&](const char* symbol,uintptr_t offset)->uintptr_t{
            auto address=reinterpret_cast<uintptr_t>(dlsym(handle,symbol));return address==image.base+offset?address:0;
        };
        symbols.manager=image.base+0x9dec0;
        symbols.strategyVtable=exact("_ZTV15KaraokeStrategy",0x976c8);
        symbols.singleVtable=exact("_ZTV13KaraokeSingle",0x975f8);
        symbols.cacheVtable=exact("_ZTV9CacheUnit",0x97b70);
        symbols.volumeVtable=exact("_ZTV7VolUnit",0x98000);
        if(!symbols.strategyVtable||!symbols.singleVtable||!symbols.cacheVtable||!symbols.volumeVtable)return false;
        symbols.strategyVtable+=16;symbols.singleVtable+=16;symbols.cacheVtable+=16;symbols.volumeVtable+=16;
        symbols.getData=reinterpret_cast<int(*)(void*,char*,int)>(exact("_ZN9CacheUnit7getDataEPci",0x48f1c));
        symbols.length=reinterpret_cast<unsigned(*)(void*)>(exact("loopbuffer_len",0x577ac));
        return symbols.getData&&symbols.length;
    }
    bool publishCookie(uint64_t cookie){return instance.publish(reinterpret_cast<uintptr_t*>(versionSlot),rx,writable,cookie);}
};
struct Host {
    struct Connection {int fd=-1;Peer peer{};uint64_t deadline=0;};
    Loaded loaded;VendorCacheSource* source=nullptr;Core* core=nullptr;Config cfg{};
    int listener=-1,wake[2]={-1,-1};pthread_t thread{};bool launched=false,payloadHost=false;std::atomic<bool> stopping{false};std::array<Connection,8> clients{};uint64_t nextId=0;
    ~Host(){stop();delete core;delete source;}
    void closePeer(Connection& c){if(c.fd>=0){core->disconnected(c.peer,nowMs());close(c.fd);c=Connection{};}}
    int start(bool payload=false){
        payloadHost=payload;
        if(getuid()!=1041||geteuid()!=1041)return -EACCES;
        int error=config(cfg);if(error)return error;
        if(!loaded.open(payload))return -ENODEV;
        source=new(std::nothrow)VendorCacheSource(loaded.symbols,true);if(!source)return -ENOMEM;
        if(!source->proved())return -ENODEV;
        uint64_t random[2]{};if(getrandom(random,sizeof random,GRND_NONBLOCK)!=sizeof random)return -EAGAIN;
        random[1]&=INT64_MAX;if(!random[1]||!loaded.publishCookie(random[1]))return -EACCES;
        core=new(std::nothrow)Core(cfg,*source,getpid(),random[0],random[1]);if(!core)return -ENOMEM;
        listener=socket(AF_UNIX,SOCK_SEQPACKET|SOCK_NONBLOCK|SOCK_CLOEXEC,0);if(listener<0)return -errno;
        sockaddr_un address{};address.sun_family=AF_UNIX;std::memcpy(address.sun_path+1,FRIDAY_CACHE_SOCKET,sizeof(FRIDAY_CACHE_SOCKET)-1);
        socklen_t length=offsetof(sockaddr_un,sun_path)+1+sizeof(FRIDAY_CACHE_SOCKET)-1;
        if(bind(listener,reinterpret_cast<sockaddr*>(&address),length)||listen(listener,4))return -errno;
        if(pipe2(wake,O_NONBLOCK|O_CLOEXEC))return -errno;
        int result=pthread_create(&thread,nullptr,[](void* p)->void*{static_cast<Host*>(p)->loop();return nullptr;},this);
        if(result)return -result;launched=true;return 0;
    }
    void acceptPeer(){
        int fd=accept4(listener,nullptr,nullptr,SOCK_NONBLOCK|SOCK_CLOEXEC);if(fd<0)return;
        struct {int pid;unsigned uid,gid;} credential{};socklen_t size=sizeof credential;
        if(getsockopt(fd,SOL_SOCKET,SO_PEERCRED,&credential,&size)||size!=sizeof credential||credential.pid<=0
            ||(credential.uid!=cfg.ownerUid&&credential.uid!=cfg.readerUid)){close(fd);return;}
        int perUid=0;Connection* free=nullptr;for(auto& c:clients){if(c.fd<0)free=&c;else perUid+=c.peer.uid==credential.uid;}
        if(!free||perUid>=2){close(fd);return;}
        uint64_t now=nowMs();if(!now){close(fd);return;}
        free->fd=fd;free->peer.id=++nextId;free->peer.uid=credential.uid;free->peer.pid=credential.pid;free->deadline=now+250;
    }
    void receive(Connection& c,uint64_t now){
        FridayCacheRequest request{};iovec vector{&request,sizeof request};msghdr message{};message.msg_iov=&vector;message.msg_iovlen=1;
        ssize_t bytes=recvmsg(c.fd,&message,MSG_DONTWAIT);
        if(bytes<0&&(errno==EAGAIN||errno==EWOULDBLOCK||errno==EINTR))return;
        if(!friday_cache_request_size_valid(size_t(bytes),message.msg_flags&(MSG_TRUNC|MSG_CTRUNC))){closePeer(c);return;}
        FridayCachePacket packet{};size_t count=core->handle(c.peer,request,now,packet);
        /* One nonblocking packet send only. A stalled peer cannot hold PCM or
         * block lease expiry, other clients, source revocation or host stop. */
        ssize_t sent=send(c.fd,&packet,count,MSG_DONTWAIT|MSG_NOSIGNAL);bool malformed=packet.header.status==-EPROTO||packet.header.status==-EACCES;
        friday_cache_wipe(&packet,sizeof packet);friday_cache_wipe(&request,sizeof request);
        if(sent!=ssize_t(count)||malformed)closePeer(c);
    }
    void loop(){
        while(!stopping.load()){
            std::array<pollfd,10> fds{};fds[0]={wake[0],POLLIN,0};fds[1]={listener,POLLIN,0};
            for(size_t i=0;i<clients.size();i++)fds[i+2]={clients[i].fd,POLLIN,0};
            int count=poll(fds.data(),fds.size(),10);uint64_t now=nowMs();if(!now)break;core->maintenance(now);
            if(count<0){if(errno==EINTR)continue;break;}if(fds[0].revents)break;
            if(fds[1].revents&POLLIN)acceptPeer();
            for(size_t i=0;i<clients.size();i++){auto& c=clients[i];if(c.fd<0)continue;
                if(!c.peer.role&&now>=c.deadline){closePeer(c);continue;}
                short events=fds[i+2].revents;if(events&(POLLERR|POLLHUP|POLLNVAL)){closePeer(c);continue;}
                if(events&POLLIN)receive(c,now);
            }
        }
        for(auto& c:clients)closePeer(c);
        /* Only this maintenance thread retries expiry writes. Shutdown remains
         * bounded even when a vendor lock stays busy or the clock fails. */
        for(int n=0;n<10&&source->pending();n++){(void)poll(nullptr,0,10);uint64_t now=nowMs();if(!now)break;source->maintenance(now);}
        source->abandonPending();
    }
    void stop(){
        stopping.store(true);if(launched){char b=1;(void)write(wake[1],&b,1);pthread_join(thread,nullptr);launched=false;}
        for(auto& c:clients)if(c.fd>=0){if(core)core->disconnected(c.peer,nowMs());close(c.fd);c.fd=-1;}
        if(listener>=0){close(listener);listener=-1;}for(int& fd:wake)if(fd>=0){close(fd);fd=-1;}
    }
};
pthread_mutex_t lifecycle=PTHREAD_MUTEX_INITIALIZER;Host* host=nullptr;
}
extern "C" __attribute__((visibility("default"))) int friday_strict_cache_start(){
    if(getuid()!=1041||geteuid()!=1041)return -EACCES;
    pthread_mutex_lock(&lifecycle);int result=0;if(!host){Host* candidate=new(std::nothrow)Host;
        if(!candidate)result=-ENOMEM;else {result=candidate->start();if(result)delete candidate;else host=candidate;}}
    pthread_mutex_unlock(&lifecycle);return result;
}
extern "C" __attribute__((visibility("default"))) void friday_strict_cache_stop(){
    pthread_mutex_lock(&lifecycle);delete host;host=nullptr;pthread_mutex_unlock(&lifecycle);
}
#if defined(FRIDAY_SYSTEM_MODULE_SHIM)
// Linked only into the firmware module. It is not exported by the shim's map
// and cannot accept a caller-selected path, image, UID or memory address.
extern "C" int friday_strict_cache_start_payload(){
    if(getuid()!=1041||geteuid()!=1041)return -EACCES;
    pthread_mutex_lock(&lifecycle);int result=0;
    if(host&&!host->payloadHost)result=-EBUSY;
    else if(!host){Host* candidate=new(std::nothrow)Host;
        if(!candidate)result=-ENOMEM;else{result=candidate->start(true);if(result)delete candidate;else host=candidate;}}
    pthread_mutex_unlock(&lifecycle);return result;
}
#else
__attribute__((constructor)) static void begin(){(void)friday_strict_cache_start();}
__attribute__((destructor)) static void end(){friday_strict_cache_stop();}
#endif
