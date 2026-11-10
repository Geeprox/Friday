#pragma once
#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>

namespace fridaywet {
enum class Profile : uint8_t { Dry=0, Pop=1, Ktv=2, Power=3, Opera=4, Studio=5, Echo=6 };
inline constexpr int ProfileCount=7;
inline constexpr bool validProfile(int id){return id>=0&&id<ProfileCount;}
struct Preset {
    const char* key;
    float preDelayMs, decaySeconds, dampingHz, highpassHz, roomScale, early, late, defaultWet;
    float echoLeftMs=0,echoRightMs=0,echoFeedback=0;
};
inline constexpr std::array<Preset,ProfileCount> Presets{{
    {"dry",0,0,0,0,1,0,0,0},
    {"pop",10,.72f,6500,120,.72f,.40f,.48f,.25f},
    {"ktv",18,1.25f,5600,110,1.00f,.48f,.64f,.32f},
    {"power",28,1.65f,5100,145,1.12f,.30f,.60f,.28f},
    {"opera",35,2.60f,7200,100,1.44f,.42f,.74f,.36f},
    {"studio",3,.38f,7200,150,.50f,.30f,.28f,.18f},
    {"echo",0,.42f,4200,160,1,.70f,0,.16f,78,104,.23f}
}};

// Original fixed-size stereo FDN implementation. Only delayed energy reaches output;
// echo uses the same bounded history for a short, damped ping-pong return. Input
// tone shaping and wet-only ducking are causal: no dry term, lookahead, new input
// queue, heap/locks/I/O in process/reset. These are tunings, not acoustic guarantees.
class WetDsp {
public:
    static constexpr int Rate=48000, Lines=8, DelayMax=4096, EarlyMax=8192;
    WetDsp(){
        for(int p=1;p<ProfileCount;p++){
            const auto& preset=Presets[p];auto& c=prepared_[p];
            c.pre=int(std::lround(preset.preDelayMs*48));
            c.hp=std::exp(-2*Pi*preset.highpassHz/Rate);
            c.damp=1-std::exp(-2*Pi*preset.dampingHz/Rate);
            c.inputDamp=1-std::exp(-2*Pi*std::min(10000.f,preset.dampingHz*1.5f)/Rate);
            c.echoLeft=int(std::lround(preset.echoLeftMs*48));
            c.echoRight=int(std::lround(preset.echoRightMs*48));
            for(int i=0;i<Lines;i++){
                c.length[i]=int(std::lround(BaseLengths[i]*preset.roomScale))|1;
                c.feedback[i]=std::pow(.001f,float(c.length[i])/(Rate*preset.decaySeconds));
            }
        }
    }
    ~WetDsp(){clear();}
    void select(Profile profile){profile_=validProfile(int(profile))?profile:Profile::Dry;clear();}
    Profile profile()const{return profile_;}
    void clear(){
        for(auto& line:lines_)line.fill(0);
        for(auto& line:early_)line.fill(0);
        indices_={};damping_={};hpInput_={};hpState_={};inputTone_={};echoDamping_={};
        vocalEnvelope_=0;earlyAt_=0;
    }
    void process(float left,float right,float& outLeft,float& outRight){
        outLeft=outRight=0;
        const int p=int(profile_);if(p<=0||!validProfile(p))return;
        const auto& c=prepared_[p];const auto& preset=Presets[p];
        // Bound even finite corrupt input before filter arithmetic can overflow.
        float input[2]{boundedInput(left),boundedInput(right)};
        for(int ch=0;ch<2;ch++){
            const float hp=c.hp*(hpState_[ch]+input[ch]-hpInput_[ch]);
            hpInput_[ch]=input[ch];hpState_[ch]=zap(hp);
            inputTone_[ch]=zap(inputTone_[ch]+c.inputDamp*(hpState_[ch]-inputTone_[ch]));
            input[ch]=softBound(inputTone_[ch],1.f);
        }
        const float level=std::max(std::abs(input[0]),std::abs(input[1]));
        // Linked 4 ms attack / 120 ms release controls only the wet amplitude.
        // Quiet input stays linear; a sustained loud phrase leaves more dry clarity.
        vocalEnvelope_=zap(vocalEnvelope_+(level-vocalEnvelope_)*(level>vocalEnvelope_?1.f/192:1.f/5760));
        const float duck=std::max(.45f,1/(1+2*std::max(0.f,vocalEnvelope_-.08f)));
        if(c.echoLeft>0){
            // Read before writing: every output term is a delayed sample. Cross
            // feedback is damped and < 1; there is no added dry-monitor delay.
            const float delayed[2]{readEarly(0,c.echoLeft),readEarly(1,c.echoRight)};
            for(int ch=0;ch<2;ch++)echoDamping_[ch]=zap(echoDamping_[ch]+c.damp*(delayed[ch]-echoDamping_[ch]));
            early_[0][earlyAt_]=zap(input[0]+preset.echoFeedback*echoDamping_[1]);
            early_[1][earlyAt_]=zap(input[1]+preset.echoFeedback*echoDamping_[0]);
            outLeft=softBound(preset.early*delayed[0]*duck,.9f);
            outRight=softBound(preset.early*delayed[1]*duck,.9f);
            earlyAt_=(earlyAt_+1)%EarlyMax;return;
        }
        for(int ch=0;ch<2;ch++)early_[ch][earlyAt_]=input[ch];
        const float preL=readEarly(0,c.pre),preR=readEarly(1,c.pre);
        // Every early-reflection tap is strictly after its input sample.
        outLeft=preset.early*(.45f*readEarly(0,c.pre+383)+.27f*readEarly(1,c.pre+811)
            -.18f*readEarly(0,c.pre+1297)+.10f*readEarly(1,c.pre+1871));
        outRight=preset.early*(.45f*readEarly(1,c.pre+431)+.27f*readEarly(0,c.pre+877)
            -.18f*readEarly(1,c.pre+1361)+.10f*readEarly(0,c.pre+1949));
        std::array<float,Lines> y{};
        for(int i=0;i<Lines;i++){
            const float sample=lines_[i][indices_[i]];
            damping_[i]=zap(damping_[i]+c.damp*(sample-damping_[i]));y[i]=damping_[i];
            outLeft+=preset.late*.353553391f*LeftSigns[i]*sample;
            outRight+=preset.late*.353553391f*RightSigns[i]*sample;
        }
        // Orthonormal Hadamard scattering; each delay gain is strictly below 1.
        for(int width=1;width<Lines;width*=2)for(int begin=0;begin<Lines;begin+=width*2)
            for(int j=0;j<width;j++){const float a=y[begin+j],b=y[begin+j+width];y[begin+j]=a+b;y[begin+j+width]=a-b;}
        for(int i=0;i<Lines;i++){
            const float injection=(i&1?preR:preL)*InjectSigns[i]*.176776695f;
            lines_[i][indices_[i]]=zap(injection+c.feedback[i]*y[i]*.353553391f);
            if(++indices_[i]>=c.length[i])indices_[i]=0;
        }
        earlyAt_=(earlyAt_+1)%EarlyMax;
        outLeft=softBound(outLeft*duck,.9f);outRight=softBound(outRight*duck,.9f);
    }
private:
    static constexpr float Pi=3.14159265358979323846f;
    static constexpr std::array<int,Lines> BaseLengths{{1493,1601,1747,1867,1999,2131,2273,2399}};
    static constexpr std::array<float,Lines> LeftSigns{{1,1,-1,1,-1,1,-1,-1}};
    static constexpr std::array<float,Lines> RightSigns{{1,-1,1,1,1,-1,-1,1}};
    static constexpr std::array<float,Lines> InjectSigns{{1,1,1,-1,-1,1,-1,-1}};
    struct Prepared {int pre=0,echoLeft=0,echoRight=0;float hp=0,damp=0,inputDamp=0;std::array<int,Lines> length{};std::array<float,Lines> feedback{};};
    std::array<Prepared,ProfileCount> prepared_{};
    std::array<std::array<float,DelayMax>,Lines> lines_{};
    std::array<std::array<float,EarlyMax>,2> early_{};
    std::array<int,Lines> indices_{};std::array<float,Lines> damping_{};
    std::array<float,2> hpInput_{},hpState_{},inputTone_{},echoDamping_{};
    float vocalEnvelope_=0;int earlyAt_=0;Profile profile_=Profile::Dry;
    static float clean(float x){return std::isfinite(x)?x:0;}
    static float boundedInput(float x){return std::isfinite(x)?std::clamp(x,-4.f,4.f):0;}
    static float zap(float x){return std::abs(x)<1e-20f?0:clean(x);}
    static float softBound(float value,float ceiling){
        if(!std::isfinite(value))return 0;
        const float magnitude=std::abs(value),knee=.8f*ceiling;
        if(magnitude<=knee)return value;
        const float excess=magnitude-knee,space=ceiling-knee;
        return std::copysign(knee+space*(excess/(space+excess)),value);
    }
    float readEarly(int ch,int delay)const{return early_[ch][(earlyAt_-delay+EarlyMax)%EarlyMax];}
};
}
