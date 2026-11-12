/* Exact-build firmware module shim. Loaded by the existing USB HAL's TMIS
 * entry point, in that HAL's system process. No PCM open/START/setter here. */
#include "SystemModuleCore.h"
#include "VendorPayloadProfile.h"
#include "PayloadBinding.h"
#include "InlineEffectHook.h"
#include "Sha256.h"
#include <android/dlext.h>
#include <dlfcn.h>
#include <elf.h>
#include <fcntl.h>
#include <link.h>
#include <new>
#include <pthread.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>
#if !defined(__ANDROID__) || !defined(__aarch64__)
#error The system module is pinned to the inspected Android arm64 HAL ABI.
#endif
extern "C" int friday_strict_cache_start_payload();
extern "C" void friday_strict_cache_stop();
namespace {
using namespace fridaystrict;
struct Mapping {uintptr_t base=0,self=0;MemoryRange rx{},rw{};int found=0,foreign=0;};
int mapping(dl_phdr_info* info,size_t,void* context){
    auto& out=*static_cast<Mapping*>(context);const char* path=info->dlpi_name;
    if(!path||!*path)return 0;
    const char* leaf=std::strrchr(path,'/');leaf=leaf?leaf+1:path;
    if(!std::strcmp(leaf,"libtlkg.hal.globalmic.so")&&(std::strcmp(path,ModulePath)||uintptr_t(info->dlpi_addr)!=out.self))out.foreign++;
    if(std::strcmp(leaf,PayloadName))return 0;
    if(std::strcmp(path,PayloadPath)){out.foreign++;return 0;}
    out.found++;out.base=uintptr_t(info->dlpi_addr);
    int executable=0,writable=0;
    for(int i=0;i<info->dlpi_phnum;i++){
        const auto& p=info->dlpi_phdr[i];if(p.p_type!=PT_LOAD)continue;
        if(p.p_flags&PF_X){
            ++executable;
            if(p.p_flags!=(PF_R|PF_X)||p.p_vaddr||p.p_offset||p.p_filesz!=PayloadRxBytes||p.p_memsz!=PayloadRxBytes){out.foreign++;continue;}
            out.rx={out.base,PayloadRxBytes};
        }
        if(p.p_flags==(PF_R|PF_W)){++writable;out.rw={out.base+uintptr_t(p.p_vaddr),size_t(p.p_memsz)};}
    }
    if(executable!=1||writable!=1)out.foreign++;
    return 0;
}
bool stable(const struct stat& a,const struct stat& b){return a.st_dev==b.st_dev&&a.st_ino==b.st_ino&&a.st_size==b.st_size&&a.st_mtim.tv_sec==b.st_mtim.tv_sec&&a.st_mtim.tv_nsec==b.st_mtim.tv_nsec&&a.st_ctim.tv_sec==b.st_ctim.tv_sec&&a.st_ctim.tv_nsec==b.st_ctim.tv_nsec;}
bool directory(const struct stat& s){return S_ISDIR(s.st_mode)&&s.st_uid==0&&!(s.st_mode&0022);}
bool file(const struct stat& s){return S_ISREG(s.st_mode)&&s.st_uid==0&&s.st_nlink==1&&!(s.st_mode&0022)&&s.st_size==PayloadBytes;}

class ProductionEnvironment final:public ModuleEnvironment {
    void* handle_=nullptr;int file_=-1;
public:
    bool loadAndVerify(ModuleOps& ops)override{
        if(getuid()!=1041||geteuid()!=1041)return false;
        Dl_info self{};if(!dladdr(reinterpret_cast<void*>(&friday_strict_cache_start_payload),&self)||!self.dli_fname||std::strcmp(self.dli_fname,ModulePath))return false;
        // Fix the directory and pin the actual inode through android_dlopen_ext.
        int root=open("/vendor",O_RDONLY|O_DIRECTORY|O_CLOEXEC|O_NOFOLLOW);if(root<0)return false;
        struct stat st{};if(fstat(root,&st)||!directory(st)){close(root);return false;}
        int lib=openat(root,"lib64",O_RDONLY|O_DIRECTORY|O_CLOEXEC|O_NOFOLLOW);close(root);if(lib<0)return false;
        if(fstat(lib,&st)||!directory(st)){close(lib);return false;}
        file_=openat(lib,PayloadName,O_RDONLY|O_CLOEXEC|O_NOFOLLOW);close(lib);if(file_<0)return false;
        if(fstat(file_,&st)||!file(st))return false;
        void* bytes=mmap(nullptr,PayloadBytes,PROT_READ,MAP_PRIVATE,file_,0);if(bytes==MAP_FAILED)return false;
        bool exact=Sha256::matches(bytes,PayloadBytes,PayloadSha);munmap(bytes,PayloadBytes);if(!exact)return false;
        android_dlextinfo options{};options.flags=ANDROID_DLEXT_USE_LIBRARY_FD;options.library_fd=file_;
        handle_=android_dlopen_ext(PayloadPath,RTLD_NOW|RTLD_LOCAL,&options);if(!handle_)return false;
        struct stat after{};if(fstat(file_,&after)||!stable(st,after))return false;
        Mapping image;image.self=uintptr_t(self.dli_fbase);dl_iterate_phdr(mapping,&image);
        if(image.found!=1||image.foreign||!image.base||!Sha256::matches(reinterpret_cast<const void*>(image.base),PayloadRxBytes,PayloadRxSha)||!verifyPayloadImageBindings(image.base,image.rx,image.rw))return false;
        auto* descriptor=reinterpret_cast<uintptr_t*>(dlsym(handle_,"TMIS"));
        if(uintptr_t(descriptor)!=image.base+PayloadDescriptor||!image.rw.contains(uintptr_t(descriptor),24)
            ||descriptor[0]!=image.base+0x7ab40||descriptor[1]!=image.base+0x7dd82||descriptor[2]!=image.base+0x9d000)return false;
        uintptr_t filler=0;std::memcpy(&filler,reinterpret_cast<const void*>(descriptor[2]),8);
        if(filler!=image.base+0x31358)return false;
        auto symbol=dlsym(handle_,"_Z20tlkg_moudle_open_jniP12_HalPatchOps");if(uintptr_t(symbol)!=image.base+PayloadOpen)return false;
        if(reinterpret_cast<int(*)(void*)>(symbol)(ops.data())!=1)return false;
        for(size_t i=0;i<ops.size();i++)if(ops[i]!=image.base+PayloadOps[i])return false;
        if(!inlinefx::install(image.base,image.rw,{image.base+0x97378,0x5c88}))return false;
        // The filler writes a table only. START/setters remain with the normal
        // Guard lease and protection protocol; no positive gain occurs here.
        return true;
    }
    bool startBridge()override{return friday_strict_cache_start_payload()==0;}
    void abandon()override{
        friday_strict_cache_stop();
        inlinefx::shutdown();
        if(handle_){dlclose(handle_);handle_=nullptr;}
        inlinefx::finishAfterPayloadStop();
        if(file_>=0){close(file_);file_=-1;}
    }
};
pthread_mutex_t mutex=PTHREAD_MUTEX_INITIALIZER;
// The explicit module destructor owns teardown. Keep the environment alive
// throughout it instead of registering a competing C++ exit destructor.
alignas(ProductionEnvironment) unsigned char environmentStorage[sizeof(ProductionEnvironment)];
ProductionEnvironment& environment=*new(environmentStorage) ProductionEnvironment;
SystemModuleCore module;
int moduleOpen(void* ops){pthread_mutex_lock(&mutex);int result=module.open(ops,environment);pthread_mutex_unlock(&mutex);return result;}
struct Methods {int(*open)(void*);};
struct Descriptor {const char* name;const char* reserved;const Methods* methods;};
static_assert(sizeof(Descriptor)==24&&sizeof(Methods)==8,"TMIS is not Android hw_module_t");
const Methods methods{moduleOpen};
}
extern "C" {
__attribute__((visibility("default"))) Descriptor TMIS{"tlkg global mic","",&methods};
__attribute__((visibility("default"))) int moduleOpenJni(void* ops) __asm__("_Z20tlkg_moudle_open_jniP12_HalPatchOps");
int moduleOpenJni(void* ops){return moduleOpen(ops);}
}
__attribute__((destructor)) static void moduleClose(){pthread_mutex_lock(&mutex);environment.abandon();pthread_mutex_unlock(&mutex);}
