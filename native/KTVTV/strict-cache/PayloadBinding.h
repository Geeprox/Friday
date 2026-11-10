#ifndef FRIDAY_PAYLOAD_BINDING_H
#define FRIDAY_PAYLOAD_BINDING_H
#include "InstanceCookie.h"
#include "VendorPayloadProfile.h"
#include <cstdint>
#include <cstring>
#include <limits>
namespace fridaystrict {
struct PayloadRela {uint64_t offset,info;int64_t addend;};
struct PayloadSymbol {uint32_t name;uint8_t info,other;uint16_t section;uint64_t value,size;};
static_assert(sizeof(PayloadRela)==24&&sizeof(PayloadSymbol)==24,"ELF64 binding ABI");
struct BindingProof {size_t relative=0,self=0,external=0,mutableSkipped=0;};
inline bool fixedPayloadSlot(uint64_t offset){
    // Descriptor, immutable source method table, version and dependency paths.
    constexpr uint64_t slots[]={0x9d000,0x9d008,0x9d010,0x9d018,0x9d028,0x9d070,0x9d078,0x9d358,
        0x9d090,0x9d0b8,0x9d0e0,0x9d108,0x9d130,0x9d168,0x9d190,0x9d1b8,0x9d1e0,0x9d208,0x9d338};
    for(auto slot:slots)if(offset==slot)return true;return false;
}
inline bool addSigned(uintptr_t value,int64_t addend,uintptr_t& result){
    if(addend>=0){auto n=uint64_t(addend);if(n>UINTPTR_MAX-value)return false;result=value+uintptr_t(n);}
    else{auto n=uint64_t(-(addend+1))+1;if(n>value)return false;result=value-uintptr_t(n);}
    return true;
}
// Run after relocation/constructors, before publishing ops or the host cookie.
// No stores, no function calls into payload, and no access outside the pinned
// PT_LOAD ranges. Undefined imports retain their platform dependency semantics.
inline bool verifyPayloadBindings(uintptr_t base,MemoryRange writable,
    const PayloadRela* relocations,size_t count,const PayloadSymbol* symbols,size_t symbolCount,BindingProof& proof,
    MemoryRange immutable={}){
    if(!base||!relocations||!symbols||count>4096||symbolCount>8192)return false;
    for(size_t i=0;i<count;i++){
        const auto& r=relocations[i];const uint32_t type=uint32_t(r.info),symbol=uint32_t(r.info>>32);
        if(r.offset>UINTPTR_MAX-base)return false;
        const uintptr_t at=base+uintptr_t(r.offset);
        if((at&7)||!writable.contains(at,8))return false;
        if(immutable.size&&!immutable.contains(at,8)){
            if(type==1025||type==1026)return false;
            if(!fixedPayloadSlot(r.offset)){
                if(type!=257&&type!=1027)return false;
                ++proof.mutableSkipped;continue;
            }
        }
        uintptr_t expected=0;
        if(type==1027){
            if(symbol||!addSigned(base,r.addend,expected))return false;
            ++proof.relative;
        }else if(type==257||type==1025||type==1026){
            if(!symbol||symbol>=symbolCount)return false;
            const auto& s=symbols[symbol];
            if(s.section==0){++proof.external;continue;}
            if((s.info&15)==10||(s.info&15)==6)return false; // IFUNC/TLS not in this profile.
            if(s.section>=0xff00&&s.section!=0xfff1)return false;
            uintptr_t origin=uintptr_t(s.value);
            if(s.section!=0xfff1){if(s.value>UINTPTR_MAX-base)return false;origin+=base;}
            if(!addSigned(origin,r.addend,expected))return false;
            ++proof.self;
        }else return false;
        uintptr_t actual=0;std::memcpy(&actual,reinterpret_cast<const void*>(at),8);
        if(actual!=expected)return false;
    }
    return true;
}
struct PayloadDynamicEntry {int64_t tag;uint64_t value;};
static_assert(sizeof(PayloadDynamicEntry)==16,"ELF64 dynamic ABI");
inline bool verifyPayloadImageBindings(uintptr_t base,MemoryRange rx,MemoryRange rw){
    if(!rw.contains(base+PayloadDynamic,PayloadDynamicBytes))return false;
    auto* dynamic=reinterpret_cast<const PayloadDynamicEntry*>(base+PayloadDynamic);
    uint64_t symbols=0,symbolBytes=0,rela=0,relaBytes=0,plt=0,pltBytes=0;bool symbolic=false,now=false,terminated=false;
    for(size_t i=0;i<PayloadDynamicBytes/sizeof(PayloadDynamicEntry);i++){
        const auto& d=dynamic[i];if(d.tag==0){terminated=true;break;}
        switch(d.tag){
            case 6:symbols=d.value;break;
            case 5:if(d.value<symbols)return false;symbolBytes=d.value;break;
            case 7:rela=d.value;break;
            case 8:relaBytes=d.value;break;
            case 23:plt=d.value;break;
            case 2:pltBytes=d.value;break;
            case 30:symbolic=(d.value&2)!=0;now=(d.value&8)!=0;break;
            case 11:if(d.value!=sizeof(PayloadSymbol))return false;break;
            case 9:if(d.value!=sizeof(PayloadRela))return false;break;
            case 20:if(d.value!=7)return false;break;
            default:break;
        }
    }
    if(!terminated||!symbolic||!now||!symbols||symbolBytes<=symbols)return false;
    symbolBytes-=symbols;
    if(symbolBytes%24||relaBytes%24||pltBytes%24||!relaBytes||!pltBytes)return false;
    auto range=[&](uint64_t offset,uint64_t bytes){return offset<=UINTPTR_MAX-base&&rx.contains(base+offset,size_t(bytes));};
    if(!range(symbols,symbolBytes)||!range(rela,relaBytes)||!range(plt,pltBytes))return false;
    const auto* table=reinterpret_cast<const PayloadSymbol*>(base+symbols);BindingProof proof;
    const MemoryRange immutable{base+0x97378,0x5c88};
    if(!verifyPayloadBindings(base,rw,reinterpret_cast<const PayloadRela*>(base+rela),relaBytes/24,table,symbolBytes/24,proof,immutable)
        ||!verifyPayloadBindings(base,rw,reinterpret_cast<const PayloadRela*>(base+plt),pltBytes/24,table,symbolBytes/24,proof,immutable))return false;
    // 75 non-RELRO DSP/config/C++ data pointers are intentionally mutable.
    // Their initializer values are not a valid post-constructor invariant.
    return proof.relative==921&&proof.self>600&&proof.mutableSkipped==75;
}
}
#endif
