#include "SystemModuleCore.h"
#include "PayloadBinding.h"
#include <cassert>
#include <climits>
#include <iostream>
#include <mutex>
#include <thread>
#include <vector>
using namespace fridaystrict;
struct Fake final:ModuleEnvironment {
    int loads=0,starts=0,closes=0;bool loaded=true,bridge=true,hole=false;
    bool loadAndVerify(ModuleOps& ops)override{++loads;for(size_t i=0;i<ops.size();i++)ops[i]=0x1000+8*i;if(hole)ops[5]=0;return loaded;}
    bool startBridge()override{++starts;return bridge;}
    void abandon()override{++closes;}
};
int main(){
    static_assert(sizeof(ModuleOps)==88,"original full eleven-slot ABI");
    for(int failure=0;failure<3;failure++){
        SystemModuleCore core;Fake env;env.loaded=failure!=0;env.bridge=failure!=1;env.hole=failure==2;
        ModuleOps output;output.fill(0xaaaaaaaa);const auto before=output;
        assert(core.open(output.data(),env)==0&&output==before&&env.closes==1);
        assert(env.starts==(failure==1?1:0));
        // A later successful-looking environment cannot revive a failed module.
        env.loaded=env.bridge=true;env.hole=false;
        assert(core.open(output.data(),env)==0&&env.loads==1&&output==before);
    }
    SystemModuleCore core;Fake env;ModuleOps first{},again{};
    assert(core.open(nullptr,env)==0&&env.loads==0);
    alignas(8) char unaligned[96]{};assert(core.open(unaligned+1,env)==0&&env.loads==0);
    assert(core.open(first.data(),env)==1&&env.loads==1&&env.starts==1);
    assert(core.open(again.data(),env)==1&&again==first&&env.loads==1&&env.starts==1);
    std::mutex mutex;std::vector<std::thread> threads;
    for(int i=0;i<8;i++)threads.emplace_back([&]{for(int n=0;n<1000;n++){ModuleOps target{};std::lock_guard<std::mutex> lock(mutex);assert(core.open(target.data(),env)==1&&target==first);}});
    for(auto& t:threads)t.join();assert(env.loads==1&&env.starts==1);

    // Relocated pointers must actually point into the same payload, even when
    // another image supplied a same-named symbol to the loader.
    alignas(8) std::array<uintptr_t,8> words{};uintptr_t base=uintptr_t(words.data());
    MemoryRange rw{base,sizeof words};PayloadSymbol symbols[4]{};
    symbols[1].section=1;symbols[1].value=16;symbols[1].info=2;
    symbols[2].section=0;symbols[3].section=0xfff1;symbols[3].value=1234;
    PayloadRela relative{0,1027,32},self{8,(uint64_t(1)<<32)|1026,0},absolute{16,(uint64_t(3)<<32)|257,-1};
    PayloadRela external{24,(uint64_t(2)<<32)|1025,0};BindingProof proof;
    words[0]=base+32;words[1]=base+16;words[2]=1233;words[3]=0xdeadbeef;
    PayloadRela all[]{relative,self,absolute,external};
    assert(verifyPayloadBindings(base,rw,all,4,symbols,4,proof));
    assert(proof.relative==1&&proof.self==2&&proof.external==1);
    words[1]+=4096;proof={};assert(!verifyPayloadBindings(base,rw,all,4,symbols,4,proof));words[1]=base+16;
    words[0]++;proof={};assert(!verifyPayloadBindings(base,rw,all,4,symbols,4,proof));words[0]--;
    auto invalid=self;invalid.offset=1;assert(!verifyPayloadBindings(base,rw,&invalid,1,symbols,4,proof));
    invalid=self;invalid.offset=sizeof words;assert(!verifyPayloadBindings(base,rw,&invalid,1,symbols,4,proof));
    invalid=self;invalid.info=(uint64_t(4)<<32)|1026;assert(!verifyPayloadBindings(base,rw,&invalid,1,symbols,4,proof));
    invalid=self;invalid.info=(uint64_t(1)<<32)|1032;assert(!verifyPayloadBindings(base,rw,&invalid,1,symbols,4,proof));
    symbols[1].info=10;assert(!verifyPayloadBindings(base,rw,&self,1,symbols,4,proof));symbols[1].info=2;
    uintptr_t value=0;assert(!addSigned(UINTPTR_MAX,1,value));assert(!addSigned(0,-1,value));
    assert(!addSigned(0,INT64_MIN,value));assert(addSigned(42,-42,value)&&value==0);
    std::cout<<"system module: full-slot publish, terminal failure, serialized reuse, self relocation and boundary tests passed\n";
}
