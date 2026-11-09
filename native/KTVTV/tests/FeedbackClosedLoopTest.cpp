#include "../android/cpp/wet/FeedbackDetector.h"
#include "../android/cpp/wet/WetKernel.h"
#include <array>
#include <cassert>
#include <cmath>
#include <iostream>
#include <memory>

// Offline causal model, not a measured room or an emulator of a vendor HAL.
// The only delay here is the synthetic acoustic plant. Production wet output
// uses its real one-frame queue/render API; no extra block or dry lookahead.
using namespace fridaywet;
using fridaywet::feedback::Detector;
constexpr double Pi=3.14159265358979323846;
static int16_t pcm(double x){return int16_t(std::lround(std::clamp(x,-1.,32767./32768)*32768));}
struct Biquad {
    double b0=1,b1=0,b2=0,a1=0,a2=0,z1=0,z2=0;
    double process(double x){const double y=b0*x+z1;z1=b1*x-a1*y+z2;z2=b2*x-a2*y;return y;}
    void bandpass(double hz,double q){
        const double w=2*Pi*hz/48000,alpha=std::sin(w)/(2*q),a0=1+alpha;
        b0=alpha/a0;b1=0;b2=-b0;a1=-2*std::cos(w)/a0;a2=(1-alpha)/a0;
    }
    void cut(double hz){
        // RBJ -6 dB peaking filter, Q6: a causal stand-in for Guard's tuple.
        const double w=2*Pi*hz/48000,a=std::pow(10.,-6./40),alpha=std::sin(w)/12,a0=1+alpha/a;
        b0=(1+alpha*a)/a0;b1=-2*std::cos(w)/a0;b2=(1-alpha*a)/a0;a1=b1;a2=(1-alpha/a)/a0;
        z1=z2=0;
    }
};
struct Result {double notchMs=0,cutMs=0,muteMs=0,peak=0,tailRms=0;int actions=0,level=0;bool overload=false;};
static Result loop(const char* name,double roomGain,bool pair,bool protectedPath,bool drift=false,bool ineffectiveEq=false){
    Detector detector;auto kernel=std::make_unique<WetKernel>();
    std::array<Biquad,3> notches{};Biquad roomA,roomB;roomA.bandpass(1000,30);roomB.bandpass(1750,30);
    std::array<double,192> air{};int cursor=0,notchCount=0,reference=70;bool muted=false;
    kernel->configure(int(Profile::Ktv),320,reference*10,false,false);
    int16_t capture[256]{};uint64_t seen=0;int nextControl=4800;Result r;double energy=0;
    uint32_t noise=123456789;
    for(int i=0;i<48000*4;i++){
        // Slight geometry motion changes the resonant mode; it never resets DSP.
        if(drift&&i%480==0)roomA.bandpass(1000+8.*i/48000,30);
        const double speaker=air[cursor];
        const double reflected=roomA.process(speaker)+(pair?roomB.process(speaker):0.);
        noise=1664525*noise+1013904223;
        const double seed=(int(noise>>16)-32768)/32768.*.001;
        // A bounded, smooth ADC surrogate avoids confusing this tonal test with
        // the independently tested PCM clipping/overload protection.
        const double microphone=.95*std::tanh((seed+roomGain*reflected)/.95);
        const int16_t input[2]{pcm(microphone),pcm(-microphone)};int16_t wet[2]{};
        const uint64_t now=1000000000ULL+uint64_t(i)*1000000000ULL/48000;
        kernel->push(input,1,now);kernel->render(wet,1,now,true);
        double dry=microphone;for(int n=0;n<notchCount;n++)dry=notches[n].process(dry);
        const double output=muted?0:dry*dryBudget(reference,320)/100.+wet[0]/32768.;
        assert(std::isfinite(output)&&std::abs(output)<=reference/100.+1./32768);
        air[cursor]=output;cursor=(cursor+1)%int(air.size());
        capture[2*(i%128)]=input[0];capture[2*(i%128)+1]=input[1];
        r.peak=std::max(r.peak,std::abs(microphone));
        if(i>=48000*3)energy+=microphone*microphone;
        if(i%128==127){
            detector.push(capture,128);
            if(i+1>=nextControl){
                nextControl+=4800;const auto state=detector.snapshot();
                if(state.sequence!=seen){
                    seen=state.sequence;r.actions++;r.level=state.severity;r.overload=state.overload;
                    const double ms=(i+1)/48.;
                    if(state.severity==1){if(!r.notchMs)r.notchMs=ms;if(protectedPath&&!ineffectiveEq&&notchCount<3)notches[notchCount++].cut(state.frequencyHz);}
                    if(state.severity==2){r.cutMs=ms;if(protectedPath)reference/=2;}
                    if(state.severity==3){r.muteMs=ms;if(protectedPath){reference=0;muted=true;}}
                    if(protectedPath)kernel->configure(int(Profile::Ktv),320,reference*10,false,muted);
                }
            }
        }
    }
    r.tailRms=std::sqrt(energy/48000);
    std::cout<<"{\"case\":\""<<name<<"\",\"notch_ms\":"<<r.notchMs<<",\"cut_ms\":"<<r.cutMs<<",\"mute_ms\":"<<r.muteMs
        <<",\"actions\":"<<r.actions<<",\"severity\":"<<r.level<<",\"overload\":"<<(r.overload?"true":"false")<<",\"microphone_peak\":"<<r.peak<<",\"last_second_rms\":"<<r.tailRms<<"}\n";
    return r;
}
static int dualTone(){
    Detector d;int16_t input[256]{};
    for(int at=0;at<48000;at+=128){for(int i=0;i<128;i++){
        const int t=at+i;const double x=.2*std::sin(2*Pi*1000*t/48000)+.2*std::sin(2*Pi*1750*t/48000);
        input[2*i]=pcm(x);input[2*i+1]=pcm(-x);
    }d.push(input,128);}
    std::cout<<"{\"case\":\"equal_nonharmonic_peaks\",\"severity\":"<<d.snapshot().severity<<",\"sequence\":"<<d.snapshot().sequence<<"}\n";
    return d.snapshot().severity;
}
int main(){
    const int dual=dualTone();
    const auto unprotected=loop("single_unprotected",3,false,false);
    const auto mild=loop("single_notch",2.2,false,true);
    const auto strong=loop("single_gain_cut",5,false,true);
    const auto persistent=loop("ineffective_eq_requires_mute",5,false,true,false,true);
    const auto extreme=loop("single_mute",12,false,true);
    const auto twin=loop("two_modes",3,true,true);
    const auto moving=loop("drifting_mode_8Hz_per_second",3,false,true,true);
    assert(dual==3);assert(unprotected.tailRms>.1);
    assert(mild.notchMs>0&&mild.cutMs==0&&mild.tailRms<.005);
    assert(strong.notchMs>0&&strong.cutMs>strong.notchMs&&strong.cutMs<=600&&strong.tailRms<.005);
    assert(extreme.overload&&extreme.muteMs>0&&extreme.muteMs<=300&&extreme.tailRms<.005);
    assert(persistent.muteMs>persistent.cutMs&&persistent.cutMs>persistent.notchMs&&persistent.muteMs<=900&&persistent.tailRms<.005&&!persistent.overload);
    assert(twin.notchMs>0&&twin.tailRms<.005);assert(moving.notchMs>0&&moving.tailRms<.005);
}
