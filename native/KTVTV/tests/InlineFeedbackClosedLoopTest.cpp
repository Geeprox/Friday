#include "../android/cpp/wet/FeedbackDetector.h"
#include "../android/cpp/wet/InlineVoiceKernel.h"
#include <array>
#include <cassert>
#include <cmath>
#include <iostream>
#include <memory>

// This deterministic causal plant is not a measured room, vendor actuator, or
// latency acceptance. Unlike the older surrogate-EQ test, every protection
// notch here executes the actual production InlineVoiceKernel at 48 kHz.
using fridaywet::InlineVoiceKernel;
using fridaywet::feedback::Detector;
constexpr double Pi=3.1415926535897932384626433832795;
static int16_t pcm(double x){return int16_t(std::lround(std::clamp(x,-1.,32767./32768)*32768));}
struct Resonator {
    double b0=0,b2=0,a1=0,a2=0,z1=0,z2=0;
    void tune(double hz){const double w=2*Pi*hz/48000,alpha=std::sin(w)/60,a0=1+alpha;b0=alpha/a0;b2=-b0;a1=-2*std::cos(w)/a0;a2=(1-alpha)/a0;}
    double process(double x){const double y=b0*x+z1;z1=-a1*y+z2;z2=b2*x-a2*y;return y;}
};
struct Result {double notch=0,cut=0,mute=0,rms=0,peak=0;int actions=0,notches=0;bool overload=false;};
static Result run(const char* name,double coupling,bool pair,bool protect,bool drift=false,bool dropNotch=false,bool stuckDryGain=false,int source=0){
    auto kernel=std::make_unique<InlineVoiceKernel>();Detector detector;
    kernel->configure(2,320,700,false,false);int16_t quiet[256]{};kernel->process(quiet,128);assert(kernel->snapshot().muted);kernel->ownerMute(false);
    Resonator a,b;a.tune(1000);b.tune(1750);std::array<double,192> air{};int cursor=0,reference=70,actualDryGain=70,monitor=700,nextControl=4800;
    int16_t observed[256]{};uint64_t seen=0;uint32_t noise=123456789;double energy=0;Result result;bool muted=false;
    for(int i=0;i<48000*4;i++){
        if(drift&&i%480==0)a.tune(1000+8.*i/48000);
        const double speaker=air[cursor],reflection=a.process(speaker)+(pair?b.process(speaker):0);
        noise=1664525*noise+1013904223;
        const double seed=(int(noise>>16)-32768)/32768.*.001;
        const double voice=source==1?(.22*std::sin(2*Pi*500*i/48000)+.10*std::sin(2*Pi*1000*i/48000)+.06*std::sin(2*Pi*1500*i/48000)):
                           source==2&&i<4800?.3*std::sin(2*Pi*1500*i/48000):0.;
        const double microphone=.95*std::tanh((seed+voice+coupling*reflection)/.95);
        int16_t output[2]{pcm(microphone*actualDryGain/100.),pcm(-microphone*actualDryGain/100.)};
        kernel->process(output,1);
        for(auto sample:output)assert(std::abs(int(sample))<=int(std::ceil(monitor/1000.*32768)));
        if(muted)assert(output[0]==0&&output[1]==0);
        air[cursor]=output[0]/32768.;cursor=(cursor+1)%int(air.size());
        observed[2*(i%128)]=pcm(microphone);observed[2*(i%128)+1]=pcm(-microphone);
        result.peak=std::max(result.peak,std::abs(microphone));if(i>=48000*3)energy+=microphone*microphone;
        if(i%128==127){
            detector.push(observed,128);
            if(i+1>=nextControl){
                nextControl+=4800;const auto state=detector.snapshot();
                if(state.sequence!=seen){
                    seen=state.sequence;result.actions++;result.overload=state.overload;const double ms=(i+1)/48.;
                    if(state.severity==1){if(!result.notch)result.notch=ms;if(protect&&!dropNotch){assert(kernel->notch(result.notches,int(std::lround(state.frequencyHz)),6));result.notches++;}}
                    if(state.severity==2){result.cut=ms;if(protect){reference/=2;if(!stuckDryGain)actualDryGain=reference;monitor=reference*10;kernel->configure(2,320,monitor,false,false);}}
                    if(state.severity==3){result.mute=ms;if(protect){kernel->ownerMute(true);monitor=0;muted=true;}}
                }
            }
        }
    }
    result.rms=std::sqrt(energy/48000);
    std::cout<<"{\"case\":\""<<name<<"\",\"notch_ms\":"<<result.notch<<",\"gain_cut_ms\":"<<result.cut<<",\"mute_ms\":"<<result.mute
        <<",\"actions\":"<<result.actions<<",\"actual_inline_notches\":"<<result.notches<<",\"overload\":"<<(result.overload?"true":"false")
        <<",\"last_second_rms\":"<<result.rms<<",\"microphone_peak\":"<<result.peak<<"}\n";
    return result;
}
int main(){
    const auto none=run("unprotected",3,false,false);
    const auto mild=run("single_notch",2.2,false,true);
    const auto strong=run("single_gain_cut",5,false,true);
    const auto pair=run("two_modes",3,true,true);
    const auto drift=run("8Hz_per_second_drift",3,false,true,true);
    const auto failedEq=run("notch_command_dropped",5,false,true,false,true);
    const auto failedGain=run("vendor_gain_stuck_inline_cap_active",5,false,true,false,false,true);
    const auto overload=run("saturated_overload",12,false,true);
    const auto voice=run("harmonic_singing",0,false,true,false,false,false,1);
    const auto transient=run("100ms_short_tone",0,false,true,false,false,false,2);
    assert(none.rms>.1);
    assert(mild.notch>0&&mild.rms<.005);
    assert(strong.notch>0&&strong.cut>strong.notch&&strong.rms<.005);
    assert(pair.notches>=2&&pair.rms<.005);
    assert(drift.notches>0&&drift.rms<.005);
    assert(failedEq.cut>failedEq.notch&&failedEq.rms<.005);
    assert(failedGain.mute>failedGain.cut&&failedGain.rms<.005);
    assert(overload.overload&&overload.mute>0&&overload.rms<.005);
    assert(voice.actions==0&&transient.actions==0);
}
