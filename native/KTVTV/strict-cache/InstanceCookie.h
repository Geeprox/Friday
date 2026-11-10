#ifndef FRIDAY_STRICT_CACHE_INSTANCE_COOKIE_H
#define FRIDAY_STRICT_CACHE_INSTANCE_COOKIE_H
#include <cstdint>
#include <cstdlib>
#include <cstring>
namespace fridaystrict {
struct MemoryRange {
    uintptr_t begin=0;size_t size=0;
    bool contains(uintptr_t address,size_t bytes)const{return bytes<=size&&address>=begin&&address-begin<=size-bytes;}
};
class InstanceCookie {
    uintptr_t* slot_=nullptr;uintptr_t original_=0;char* text_=nullptr;uint64_t cookie_=0;
public:
    ~InstanceCookie(){restore();}
    bool publish(uintptr_t* slot,MemoryRange verifiedRx,MemoryRange writableData,uint64_t cookie){
        if(slot_||!cookie||!slot||(uintptr_t(slot)&(alignof(uintptr_t)-1))||!writableData.contains(uintptr_t(slot),sizeof *slot))return false;
        uintptr_t original=__atomic_load_n(slot,__ATOMIC_ACQUIRE);
        if(!verifiedRx.contains(original,6)||std::memcmp(reinterpret_cast<void*>(original),"1.1.3",6))return false;
        char* text=static_cast<char*>(std::malloc(30));if(!text)return false;
        std::memcpy(text,"1.1.3+friday.",13);static constexpr char hex[]="0123456789abcdef";
        for(int i=0;i<16;i++)text[13+i]=hex[(cookie>>(60-4*i))&15];text[29]=0;
        uintptr_t expected=original;
        if(!__atomic_compare_exchange_n(slot,&expected,uintptr_t(text),false,__ATOMIC_ACQ_REL,__ATOMIC_ACQUIRE)){std::free(text);return false;}
        slot_=slot;original_=original;text_=text;cookie_=cookie;return true;
    }
    void restore(){
        if(slot_){uintptr_t expected=uintptr_t(text_);(void)__atomic_compare_exchange_n(slot_,&expected,original_,false,__ATOMIC_ACQ_REL,__ATOMIC_ACQUIRE);slot_=nullptr;cookie_=0;}
        /* Intentionally never free a successfully published string: a vendor
         * getter may have borrowed its pointer immediately before restoration.
         * malloc storage survives this DSO's unload for the process lifetime. */
    }
    const char* text()const{return text_;}
    uint64_t cookie()const{return cookie_;}
};
}
#endif
