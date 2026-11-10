#include "../android/cpp/wet/VoiceMixBudget.h"
#include "../android/cpp/wet/WetDsp.h"
#include "../android/cpp/wet/InputBacklogPolicy.h"
#include <cassert>
#include <cmath>
int main(){
    using namespace fridaywet;
    for(int gain=0;gain<=100;gain++)for(const auto& preset:Presets){
        const int wet=int(std::lround(preset.defaultWet*1000));
        const int dry=dryBudget(gain,wet);const float ceiling=wetCeiling(gain*10,wet);
        assert(dry>=0&&dry<=gain&&ceiling>=0&&std::isfinite(ceiling));
        assert(std::abs(dry/100.0f+ceiling-gain/100.0f)<.00001f);
        assert(wet>0||ceiling==0);
    }
    for(float ceiling:{0.f,.05f,.25f})for(int i=-10000;i<=10000;i++){
        const float sample=i/1000.f,out=softVoiceLimit(sample,ceiling);
        assert(std::isfinite(out)&&std::abs(out)<=ceiling+.000001f);
        if(std::abs(sample)<=ceiling*.8f)assert(out==sample);
    }
    assert(softVoiceLimit(NAN,.2f)==0&&softVoiceLimit(INFINITY,.2f)==0);
    // All permitted monitor/wet values, including fractional monitor levels
    // and wet above the Guard's reserved 360-permille maximum, stay conservative.
    for(int gain=0;gain<=1000;gain++)for(int wet=0;wet<=1000;wet++){
        const float ceiling=wetCeiling(gain,wet);const int dry=dryBudget(gain/10,wet);
        assert(ceiling>=0&&std::isfinite(ceiling));assert(dry/100.f+ceiling<=gain/1000.f+.000001f);
        for(float sample:{-100.f,-.9f,.9f,100.f}){
            const float out=softVoiceLimit(sample,ceiling);
            assert(std::isfinite(out)&&std::abs(out)<=ceiling+.000001f);
        }
    }
    assert(dryBudget(70,320)==53&&dryBudget(100,360)==73);
    assert(backlogPlan(-1).discardBlocks==0&&!backlogPlan(-1).failed);
    assert(backlogPlan(480).discardBlocks==0);
    assert(backlogPlan(481).discardBlocks==1);
    assert(backlogPlan(1600).discardBlocks==8&&!backlogPlan(4800).failed);
    assert(backlogPlan(4801).failed&&backlogPlan(4801).discardBlocks==0);
    // A delayed 32 ms observer drains a bounded amount, consumes the current
    // block, then reaches its 10 ms target instead of preserving a long FIFO.
    int queued=1600;const auto plan=backlogPlan(queued);
    for(int i=0;i<plan.discardBlocks;i++)queued-=128;
    queued-=128;assert(queued<=480);
}
