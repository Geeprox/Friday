#include "../android/cpp/wet/FeedbackDetector.h"
#include <atomic>
#include <cassert>
#include <cstdlib>
#include <iostream>
#include <new>
#include <random>
#include <vector>

static std::atomic<uint64_t> allocations{0};
void* operator new(std::size_t bytes){allocations++;if(void* p=std::malloc(bytes))return p;throw std::bad_alloc();}
void operator delete(void* p)noexcept{std::free(p);}
void operator delete(void* p,std::size_t)noexcept{std::free(p);}
using fridaywet::feedback::Detector;
using fridaywet::feedback::Snapshot;
static constexpr double pi=3.14159265358979323846;
static int16_t pcm(double x){return static_cast<int16_t>(std::max(-32768.0,std::min(32767.0,std::round(x*32768))));}
static double sine(double hz,int i){return std::sin(2*pi*hz*i/48000.0);}
struct Receipt {Snapshot last;uint64_t notch=0,cut=0,latch=0;int actions=0;};
template<class Signal>
static Receipt run(Detector& detector,int frames,Signal signal,bool anti=false){
    Receipt receipt{};int16_t samples[512*2]{};uint64_t sequence=detector.snapshot().sequence;
    for(int at=0;at<frames;at+=512){
        const int count=std::min(512,frames-at);
        for(int i=0;i<count;i++){const auto value=pcm(signal(at+i));samples[i*2]=value;samples[i*2+1]=anti?static_cast<int16_t>(-int(value)):value;}
        const auto before=allocations.load();detector.push(samples,count);assert(allocations.load()==before);
        receipt.last=detector.snapshot();assert(std::isfinite(receipt.last.frequencyHz)&&std::isfinite(receipt.last.peak));
        if(receipt.last.sequence!=sequence){
            assert(receipt.last.sequence==sequence+1);sequence=receipt.last.sequence;receipt.actions++;
            if(receipt.last.severity==1&&!receipt.notch)receipt.notch=receipt.last.processedFrames;
            if(receipt.last.severity==2&&!receipt.cut)receipt.cut=receipt.last.processedFrames;
            if(receipt.last.severity==3&&!receipt.latch)receipt.latch=receipt.last.processedFrames;
        }
    }return receipt;
}
static void noAction(const char* name,const Receipt& r){if(r.last.severity!=0){std::cerr<<name<<" unexpected severity "<<r.last.severity<<" at "<<r.last.frequencyHz<<" Hz\n";std::abort();}}
int main(){
    {
        Detector d;const auto r=run(d,48000,[](int i){return std::min(.45,.045*std::exp(i/48000.0*4))*sine(1000,i);});
        assert(r.notch>0&&r.notch<=12000);assert(r.cut>r.notch&&r.latch>r.cut);assert(r.last.latched&&!r.last.overload);assert(std::abs(r.last.frequencyHz-1000)<3);assert(r.actions==3);
        std::cout<<"growing feedback surrogate ms: notch="<<r.notch/48.0<<" cut="<<r.cut/48.0<<" latch="<<r.latch/48.0<<"\n";
    }
    {
        Detector d;const auto r=run(d,48000,[](int i){return .25*sine(2200,i);},true);
        assert(r.last.latched&&r.actions==3&&r.notch>0&&r.notch<=12000);assert(std::abs(r.last.frequencyHz-2200)<3);
        std::cout<<"steady antiphase ms: notch="<<r.notch/48.0<<" cut="<<r.cut/48.0<<" latch="<<r.latch/48.0<<"\n";
        const auto sequence=r.last.sequence;const auto after=run(d,480000,[](int){return 0.;});assert(after.last.sequence==sequence&&after.last.latched);
        d.reset();assert(!d.snapshot().ready&&!d.snapshot().latched&&d.snapshot().severity==0&&d.snapshot().processedFrames==0);
    }
    {
        // Equal unrelated modes used to fail the single-band 72% test entirely.
        Detector d;auto r=run(d,48000,[](int i){return .2*sine(1000,i)+.2*sine(1750,i);},true);
        assert(r.last.latched&&!r.last.overload&&r.notch<=12000&&r.actions==3);
        std::cout<<"equal dual-mode ms: notch="<<r.notch/48.0<<" cut="<<r.cut/48.0<<" latch="<<r.latch/48.0<<"\n";
        d.reset();r=run(d,48000,[](int i){return .09*sine(1000,i)+.09*sine(1750,i);});noAction("quiet dual mode",r);
        d.reset();r=run(d,48000,[](int i){return .2*sine(500,i)+.2*sine(1000,i);});noAction("two-harmonic voice",r);
        d.reset();r=run(d,48000,[](int i){return .015*sine(250,i)+.2*sine(500,i)+.2*sine(750,i);});noAction("weak fundamental with dominant second/third harmonics",r);
        d.reset();r=run(d,48000,[](int i){return .2*sine(500,i)+.2*sine(750,i);});noAction("missing fundamental harmonic pair",r);
        d.reset();r=run(d,48000,[](int i){return .28*sine(500,i)+.13*sine(750,i);});noAction("dominant second harmonic with weak third",r);
        // Swapping which of two stable modes is strongest must not continually
        // restart the candidate's dwell time or produce repeated notch spam.
        d.reset();r=run(d,48000,[](int i){const double a=.2+.008*std::sin(2*pi*5*i/48000.);return a*sine(1000,i)+(.4-a)*sine(1750,i);});
        assert(r.last.latched&&!r.last.overload&&r.notch<=12000&&r.actions==3);
        d.reset();r=run(d,48000,[](int i){return .12*sine(1000,i)+(i<12000?0:.16*sine(1750,i));});
        assert(r.last.latched&&std::abs(r.last.frequencyHz-1750)<3);
    }
    {
        Detector d;auto r=run(d,48000,[](int i){return .075*sine(1800,i);});noAction("quiet sustained tone",r);
    }
    {
        Detector d;auto r=run(d,48000,[](int i){return .22*sine(500,i)+.13*sine(1000,i)+.09*sine(1500,i)+.05*sine(2000,i);});noAction("harmonic vowel",r);
        d.reset();r=run(d,48000,[](int i){return .075*sine(500,i)+.25*sine(1000,i)+.03*sine(1500,i);});noAction("dominant second-harmonic vowel",r);
        d.reset();r=run(d,48000,[](int i){const double t=i/48000.;return std::exp(-3*t)*(.3*sine(1200,i)+.08*sine(2400,i)+.04*sine(3600,i));});noAction("decaying instrument",r);
    }
    {
        Detector d;auto r=run(d,96000,[](int i){const double t=i/48000.;return .3*std::sin(2*pi*(500*t+500*t*t));});noAction("sweep",r);
        d.reset();r=run(d,96000,[](int i){const double t=i/48000.;return .25*std::sin(2*pi*1800*t+2*std::sin(2*pi*6*t));});noAction("vibrato instrument",r);
    }
    {
        std::mt19937 random(20261102);std::uniform_real_distribution<double> noise(-.4,.4);
        Detector d;auto r=run(d,48000*3,[&](int){return noise(random);});noAction("broadband noise",r);
        d.reset();r=run(d,48000,[](int i){return i<4800?.3*sine(1500,i):0.;});noAction("100ms short tone",r);
        d.reset();r=run(d,48000,[](int i){return i<3840?std::min(.7,.025*std::exp(i/48000.0*50))*sine(1500,i):0.;});noAction("80ms growing transient",r);
    }
    {
        Detector d;auto r=run(d,48000,[](int i){return i%2?1.2:-1.2;});assert(r.last.latched&&r.last.overload&&r.last.severity==3&&r.last.frequencyHz==0&&r.actions==1&&r.last.clippingSamples==96000);assert(r.latch<=9600);std::cout<<"overload safety latch ms="<<r.latch/48.0<<"\n";
        d.reset();r=run(d,48000,[](int i){return i<480?(i%2?1.2:-1.2):0.;});noAction("10ms clipping transient",r);assert(r.last.clippingSamples==960);
        // An analog limiter can keep every PCM sample below the digital clip
        // code while leaving a dangerous high-RMS plateau and strong harmonics.
        d.reset();r=run(d,48000,[](int i){return .95*std::tanh(12*sine(1000,i));});
        assert(r.last.latched&&r.last.overload&&r.last.frequencyHz==0&&r.last.clippingSamples==0&&r.actions==1&&r.latch<=9600);
        std::cout<<"near-full-scale saturation latch ms="<<r.latch/48.0<<"\n";
        d.reset();r=run(d,48000,[](int i){return i<3840?.95*std::tanh(12*sine(1000,i)):0.;});noAction("80ms saturation transient",r);
        d.reset();r=run(d,48000,[](int i){return .6*sine(500,i)+.25*sine(1000,i)+.14*sine(1500,i);});noAction("loud harmonic voice",r);
    }
    {
        Detector d;auto r=run(d,48000*10,[](int){return 0.;});noAction("long silence",r);assert(r.last.processedFrames==480000&&r.last.ready&&r.last.sequence==0&&r.last.peak==0);
        int16_t single[2]={30000,-30000};const auto before=d.snapshot();d.push(nullptr,100);d.push(single,0);assert(d.snapshot().processedFrames==before.processedFrames);
        d.reset();std::mt19937 random(7);std::uniform_real_distribution<double> noise(-.1,.1);r=run(d,48000*30,[&](int){return noise(random);});noAction("30 second numerical stability",r);assert(r.last.processedFrames==1440000);
    }
    {
        Detector d;auto a=run(d,12000,[](int i){return .18*sine(900,i);});assert(a.last.severity==1&&a.actions==1);
        run(d,4800,[](int){return 0.;});auto b=run(d,16000,[](int i){return .18*sine(1500,i);});assert(b.last.severity==1&&b.actions==1);
        run(d,4800,[](int){return 0.;});auto c=run(d,16000,[](int i){return .18*sine(2100,i);});assert(c.last.severity==1&&c.actions==1);
        run(d,4800,[](int){return 0.;});auto e=run(d,16000,[](int i){return .18*sine(2700,i);});assert(e.actions==1&&e.last.sequence==4&&e.last.severity==2);
        auto persistent=run(d,48000,[](int i){return .18*sine(2700,i);});
        assert(persistent.actions==1&&persistent.last.sequence==5&&persistent.last.latched);
        auto after=run(d,48000,[](int i){return .3*sine(3300,i);});assert(after.actions==0&&after.last.sequence==5);
    }
    {
        Detector d;int16_t input[1024]{};std::mt19937 random(42);std::uniform_real_distribution<double> noise(-.6,.6);
        for(int at=0;at<48000;at+=512){const int count=std::min(512,48000-at);for(int i=0;i<count;i++){input[2*i]=pcm(.25*sine(1300,at+i));input[2*i+1]=pcm(noise(random));}
            const auto before=allocations.load();d.push(input,count);assert(allocations.load()==before);}
        assert(d.snapshot().latched&&!d.snapshot().overload&&std::abs(d.snapshot().frequencyHz-1300)<3);
    }
    {
        // Independent stereo modes, each stable but swapping loudness every
        // 100 ms, must not reset the one candidate indefinitely.
        Detector d;int16_t input[1024]{};uint64_t notch=0;
        for(int at=0;at<48000;at+=512){const int count=std::min(512,48000-at);for(int i=0;i<count;i++){
            const int t=at+i;const double a=.2+.008*std::sin(2*pi*5*t/48000.);
            input[2*i]=pcm(a*sine(1000,t));input[2*i+1]=pcm((.4-a)*sine(1750,t));
        }const auto before=allocations.load();d.push(input,count);assert(allocations.load()==before);
            if(d.snapshot().severity==1&&!notch)notch=d.snapshot().processedFrames;}
        assert(notch>0&&notch<=12000&&d.snapshot().latched&&d.snapshot().sequence==3&&!d.snapshot().overload);
        d.reset();
        for(int at=0;at<48000;at+=512){const int count=std::min(512,48000-at);for(int i=0;i<count;i++){
            const int t=at+i;input[2*i]=pcm(.12*sine(1000,t));input[2*i+1]=pcm(t<12000?0:.16*sine(1750,t));
        }d.push(input,count);}
        assert(d.snapshot().latched&&std::abs(d.snapshot().frequencyHz-1750)<3);
    }
    {
        // Exact packet boundaries must not change the signal decision.
        Detector whole,split;int16_t input[1024]{};std::mt19937 random(19);
        for(int at=0;at<48000;at+=512){const int count=std::min(512,48000-at);for(int i=0;i<count;i++)input[2*i]=input[2*i+1]=pcm(.25*sine(1700,at+i));
            whole.push(input,count);for(int cursor=0;cursor<count;){const int length=std::min(count-cursor,1+int(random()%127));split.push(input+cursor*2,length);cursor+=length;}}
        const auto a=whole.snapshot(),b=split.snapshot();assert(a.processedFrames==b.processedFrames&&a.sequence==b.sequence&&a.severity==b.severity&&a.frequencyHz==b.frequencyHz);
        const auto sequence=a.sequence;whole.reset();assert(whole.snapshot().sequence==sequence);run(whole,48000,[](int i){return .25*sine(1700,i);});assert(whole.snapshot().sequence==sequence+3);
    }
    {
        Detector d;auto first=run(d,12000,[](int i){return .6*sine(1100,i);});assert(first.last.severity==1);
        auto residual=run(d,48000,[](int i){return .28*sine(1100,i);});assert(residual.last.severity==3);
        d.reset();first=run(d,12000,[](int i){return .25*sine(1100,i);});assert(first.last.severity==1);
        auto suppressed=run(d,48000,[](int i){return .02*sine(1100,i);});assert(suppressed.actions==0&&suppressed.last.severity==1&&!suppressed.last.latched);
    }
    {
        Detector d;auto initial=run(d,22000,[](int i){return .25*sine(900,i);});assert(initial.last.severity==2);
        run(d,4800,[](int){return 0.;});auto moved=run(d,16000,[](int i){return .25*sine(2100,i);});assert(moved.last.latched);
        d.reset();auto low=run(d,48000,[](int i){return .25*sine(80,i);});noAction("below configured observation band",low);
        d.reset();auto high=run(d,48000,[](int i){return .25*sine(9000,i);});noAction("above configured observation band",high);
    }
    assert(sizeof(Detector)<100000);
    std::cout<<"FeedbackDetectorTest passed: growth, steady/antiphase, dual modes/rank changes, harmonics, sweep/vibrato, noise/transients, clipping/saturation overload distinction, latching/reset, bounded requests, no push allocations\n";
}
