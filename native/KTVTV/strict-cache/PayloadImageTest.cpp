// Read ELF bytes as data, emulate relocation in owned RAM, never dlopen/execute.
#include "PayloadBinding.h"
#include "VendorPayloadProfile.h"
#include "Sha256.h"
#include <cassert>
#include <fstream>
#include <iostream>
#include <iterator>
#include <vector>
using namespace fridaystrict;
int main(int argc,char** argv){
    assert(argc==2);std::ifstream input(argv[1],std::ios::binary);
    std::vector<unsigned char> file((std::istreambuf_iterator<char>(input)),{});
    assert(file.size()==PayloadBytes&&Sha256::matches(file.data(),file.size(),PayloadSha));
    assert(Sha256::matches(file.data(),PayloadRxBytes,PayloadRxSha));
    // The two PT_LOAD ranges have different file and memory offsets.
    std::vector<uint64_t> storage((0xa0000+7)/8);auto* bytes=reinterpret_cast<unsigned char*>(storage.data());
    uintptr_t base=uintptr_t(bytes);std::memcpy(bytes,file.data(),PayloadRxBytes);
    std::memcpy(bytes+0x97378,file.data()+0x96378,0x6b48);
    MemoryRange rw{base+0x97378,0x79f8},immutable{base+0x97378,0x5c88};
    auto* symbols=reinterpret_cast<const PayloadSymbol*>(bytes+0x3418);constexpr size_t symbolCount=(0xdab0-0x3418)/24;
    auto* rela=reinterpret_cast<const PayloadRela*>(bytes+0x1e308);constexpr size_t relaCount=49656/24;
    auto* plt=reinterpret_cast<const PayloadRela*>(bytes+0x2a500);constexpr size_t pltCount=16680/24;
    auto relocate=[&](const PayloadRela* entries,size_t count){
        for(size_t i=0;i<count;i++){
            const auto& r=entries[i];const auto type=uint32_t(r.info),index=uint32_t(r.info>>32);uintptr_t expected=0;
            if(type==1027){assert(!index&&addSigned(base,r.addend,expected));}
            else{
                assert(index<symbolCount);const auto& s=symbols[index];
                if(s.section==0)expected=0x123456780000ULL+index*16;
                else assert(addSigned(s.section==0xfff1?s.value:base+s.value,r.addend,expected));
            }
            assert(rw.contains(base+r.offset,8));std::memcpy(bytes+r.offset,&expected,8);
        }
    };
    relocate(rela,relaCount);relocate(plt,pltCount);
    auto check=[&]{BindingProof p;bool ok=verifyPayloadBindings(base,rw,rela,relaCount,symbols,symbolCount,p,immutable)
        &&verifyPayloadBindings(base,rw,plt,pltCount,symbols,symbolCount,p,immutable);
        return std::pair<bool,BindingProof>{ok,p};};
    const auto result=check();assert(result.first&&result.second.relative==921&&result.second.mutableSkipped==75);
    assert(verifyPayloadImageBindings(base,{base,PayloadRxBytes},rw));
    auto* dynamic=reinterpret_cast<PayloadDynamicEntry*>(bytes+PayloadDynamic);
    // Exercise the exact production dynamic-table parser, including refusal of
    // a foreign table pointer and missing symbolic/local resolution request.
    for(size_t i=0;i<PayloadDynamicBytes/16;i++){
        if(dynamic[i].tag==6){const auto original=dynamic[i].value;dynamic[i].value=UINT64_MAX;
            assert(!verifyPayloadImageBindings(base,{base,PayloadRxBytes},rw));dynamic[i].value=original;}
        if(dynamic[i].tag==30){const auto original=dynamic[i].value;dynamic[i].value=8;
            assert(!verifyPayloadImageBindings(base,{base,PayloadRxBytes},rw));dynamic[i].value=original;}
    }
    assert(verifyPayloadImageBindings(base,{base,PayloadRxBytes},rw));
    // C++/DSP constructors may change unrelated mutable data. The source
    // method pointers and every immutable self binding must stay pinned.
    auto* arbitrary=reinterpret_cast<uintptr_t*>(bytes+0x9d3b8);*arbitrary^=0xabcdef;assert(check().first);
    auto* start=reinterpret_cast<uintptr_t*>(bytes+0x9d090);const auto saved=*start;*start+=0x100000;
    assert(!check().first);*start=saved;assert(check().first);
    auto* slot=reinterpret_cast<uintptr_t*>(bytes+plt[1].offset);const auto original=*slot;
    // Choose an actual self-defined PLT function instead of an external import.
    for(size_t i=0;i<pltCount;i++)if(symbols[plt[i].info>>32].section){slot=reinterpret_cast<uintptr_t*>(bytes+plt[i].offset);break;}
    const auto own=*slot;*slot+=0x800000;assert(!check().first);*slot=own;assert(check().first);(void)original;
    std::cout<<"payload data-only relocation proof: relative="<<result.second.relative<<" self="<<result.second.self
        <<" external="<<result.second.external<<" intentionally mutable="<<result.second.mutableSkipped<<"; foreign table/PLT refused\n";
}
