#ifndef FRIDAY_SYSTEM_MODULE_CORE_H
#define FRIDAY_SYSTEM_MODULE_CORE_H
#include <array>
#include <cstdint>
#include <cstring>
namespace fridaystrict {
using ModuleOps=std::array<uintptr_t,11>;
struct ModuleEnvironment {
    virtual ~ModuleEnvironment()=default;
    virtual bool loadAndVerify(ModuleOps&)=0;
    virtual bool startBridge()=0;
    virtual void abandon()=0;
};
// Serialized by the module's process-local mutex. A failed first open is
// terminal for this loaded module: no partial table and no hidden retry/START.
class SystemModuleCore {
    enum State{New,Ready,Failed};State state_=New;ModuleOps ops_{};
public:
    int open(void* destination,ModuleEnvironment& environment){
        if(!destination||reinterpret_cast<uintptr_t>(destination)%alignof(uintptr_t))return 0;
        if(state_==Failed)return 0;
        if(state_==New){
            ModuleOps candidate{};
            if(!environment.loadAndVerify(candidate)){
                state_=Failed;environment.abandon();return 0;
            }
            for(auto value:candidate)if(!value){state_=Failed;environment.abandon();return 0;}
            if(!environment.startBridge()){state_=Failed;environment.abandon();return 0;}
            ops_=candidate;state_=Ready;
        }
        // USB's actual ABI treats ANY nonzero return as success. Never return
        // -errno here, and publish all eleven slots only after every gate passes.
        std::memcpy(destination,ops_.data(),sizeof ops_);return 1;
    }
};
}
#endif
