#include "../android/cpp/DirectVoiceMonitor.h"
#include <cassert>
#include <cstdlib>

int main(){
    for(int value=-32768;value<=32767;value++){
        auto sample=directVoiceSample(static_cast<int16_t>(value),static_cast<int16_t>(value));
        assert(std::abs(static_cast<int>(sample))<=2621);
        // Exhaustive domain check: the safety ceiling no longer changes any
        // equal-channel input sample; fixed gain has no clipped upper segment.
        assert(sample==value*8/100);
    }
    assert(directVoiceSample(16000,16000)==1280);
    assert(directVoiceSample(16000,-16000)==0);
    assert(directVoiceSample(-32768,-32768)==-2621);
    assert(directVoiceSample(32767,32767)==2621);
    assert(!directVoiceMayPlay(false,false,10,20));
    assert(!directVoiceMayPlay(true,true,10,20));
    assert(!directVoiceMayPlay(true,false,20,20));
    assert(!directVoiceMayPlay(true,false,21,20));
    assert(!directVoiceMayPlay(true,false,0,0));
    assert(directVoiceMayPlay(true,false,19,20));
}
