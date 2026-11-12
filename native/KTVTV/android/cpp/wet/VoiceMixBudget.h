#pragma once
#include <algorithm>
#include <cmath>
namespace fridaywet {
// Same integer budget as the Guard. No dry samples are copied into wet output.
// The requested voice level is divided between the existing OEM dry path and
// this wet return. This is a digital/setpoint budget, not a measured speaker SPL.
inline int dryBudget(int gain,int wet){return gain<=0?0:std::max(1,std::clamp(gain,0,100)*1000/(1000+std::clamp(wet,0,360)));}
inline float wetCeiling(int gainPermille,int wet){const int gain=std::clamp(gainPermille/10,0,100);return (gain-dryBudget(gain,wet))/100.0f;}
inline float softVoiceLimit(float value,float ceiling){
    if(!std::isfinite(value)||ceiling<=0)return 0;
    const float magnitude=std::abs(value),knee=ceiling*.8f;
    if(magnitude<=knee)return value;
    const float excess=magnitude-knee,space=ceiling-knee;
    return std::copysign(knee+space*(excess/(space+excess)),value);
}
}
