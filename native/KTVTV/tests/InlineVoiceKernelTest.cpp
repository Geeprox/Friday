#include "../android/cpp/wet/InlineVoiceKernel.h"
#include <atomic>
#include <cassert>
#include <cstdlib>
#include <iostream>
#include <memory>
#include <new>
#include <thread>
#include <vector>

static std::atomic<uint64_t> allocations{0};
void* operator new(std::size_t size){allocations++;if(void* p=std::malloc(size))return p;throw std::bad_alloc();}
void operator delete(void* p)noexcept{std::free(p);}
void operator delete(void* p,std::size_t)noexcept{std::free(p);}
using fridaywet::InlineVoiceKernel;
constexpr double Pi=3.1415926535897932384626433832795;
static int16_t pcm(double x){return int16_t(std::lround(std::clamp(x,-1.,32767./32768)*32768));}
static bool zero(const int16_t* p,int count){for(int i=0;i<count;i++)if(p[i])return false;return true;}
static void process(InlineVoiceKernel& k,int16_t* p,int frames){const auto before=allocations.load();k.process(p,frames);assert(allocations.load()==before);}
static void prime(InlineVoiceKernel& k){int16_t quiet[1024]{};process(k,quiet,512);assert(zero(quiet,1024));}
static std::unique_ptr<InlineVoiceKernel> opened(int profile=0,int wet=0,int gain=1000){
    auto k=std::make_unique<InlineVoiceKernel>();assert(k->configure(profile,wet,gain,false,false));k->ownerMute(false);prime(*k);return k;
}
static double attenuation(int hz,int cut){
    auto k=opened();assert(k->notch(0,hz,cut));int16_t input[256]{};double in=0,out=0;
    for(int at=0;at<48000;at+=128){for(int i=0;i<128;i++){const int t=at+i;input[2*i]=input[2*i+1]=pcm(.2*std::sin(2*Pi*hz*t/48000));if(t>=24000)in+=double(input[2*i])*input[2*i];}
        process(*k,input,128);for(int i=0;i<128;i++)if(at+i>=24000)out+=double(input[2*i])*input[2*i];}
    return 10*std::log10(out/in);
}
int main(){
    {
        InlineVoiceKernel k;assert(k.snapshot().muted&&k.snapshot().callbacks==0);
        assert(k.configure(2,320,700,false,false));const auto revision=k.snapshot().requestedRevision;
        int16_t input[256];std::fill(std::begin(input),std::end(input),20000);process(k,input,128);
        auto state=k.snapshot();assert(zero(input,256)&&state.muted&&state.profile==2&&state.wet==320&&state.monitorGain==700);
        assert(state.requestedRevision==revision&&state.appliedRevision==revision);
        assert(k.configure(2,320,700,false,false)&&k.snapshot().requestedRevision==revision);
        std::fill(std::begin(input),std::end(input),20000);process(k,input,128);assert(zero(input,256));
        k.ownerMute(false);std::fill(std::begin(input),std::end(input),4000);process(k,input,128);assert(!zero(input,256));
        k.ownerMute(true);assert(k.configure(5,180,900,false,false));std::fill(std::begin(input),std::end(input),20000);process(k,input,128);
        assert(zero(input,256)&&k.snapshot().muted&&k.snapshot().profile==5&&k.snapshot().wet==180);
    }
    {
        for(bool bypass:{false,true}){
            auto k=opened(bypass?2:0,bypass?320:0,500);if(bypass){k->configure(2,320,500,false,true);prime(*k);}
            int16_t impulse[256]{};impulse[0]=6000;impulse[1]=-6000;process(*k,impulse,128);
            assert(impulse[0]==6000&&impulse[1]==-6000&&zero(impulse+2,254)); // Same frame, no second dry gain.
        }
    }
    {
        InlineVoiceKernel k;const auto revision=k.snapshot().requestedRevision;
        for(auto c:std::vector<std::array<int,3>>{{-1,0,0},{7,0,0},{1,-1,0},{1,361,0},{1,0,-1},{1,0,1001}})assert(!k.configure(c[0],c[1],c[2],false,false));
        assert(k.snapshot().requestedRevision==revision);
        for(auto c:std::vector<std::array<int,3>>{{-1,1000,6},{8,1000,6},{0,187,6},{0,6493,6},{0,1000,-1},{0,1000,13},{0,0,0}})assert(!k.notch(c[0],c[1],c[2]));
        assert(k.notch(0,188,12)&&k.notch(7,6492,12));const auto before=k.snapshot().notchRequestedRevision;
        assert(k.notch(7,6492,12)&&k.snapshot().notchRequestedRevision==before);
        int16_t quiet[2]{};k.process(nullptr,128);k.process(quiet,-1);k.process(quiet,0);assert(k.snapshot().callbacks==0);
        for(uint32_t i=0;i<=InlineVoiceKernel::RevisionMask;i++){
            assert(k.configure(0,1+(i&1),1000,false,false));assert(k.snapshot().requestedRevision==((i+1)&InlineVoiceKernel::RevisionMask));
        }
    }
    {
        auto k=opened();assert(k->notch(0,1000,6));const auto revision=k->snapshot().notchRequestedRevision;
        int16_t samples[512]{};process(*k,samples,128);assert(k->snapshot().notchAppliedRevision!=revision);
        process(*k,samples,112);assert(k->snapshot().notchAppliedRevision==revision);
        k->notch(0,1100,12);process(*k,samples,128);k->notch(0,1200,6);process(*k,samples,256);
        assert(k->snapshot().notchRequestedRevision!=k->snapshot().notchAppliedRevision);
        process(*k,samples,96);assert(k->snapshot().notchRequestedRevision==k->snapshot().notchAppliedRevision);
    }
    {
        auto k=opened(2,320,700);k->configure(4,360,700,false,false);
        int16_t quiet[256]{};process(*k,quiet,128);
        assert(k->snapshot().appliedRevision!=k->snapshot().requestedRevision);
        process(*k,quiet,112);assert(k->snapshot().appliedRevision==k->snapshot().requestedRevision);
    }
    double notchError=0;
    for(int hz:{188,1000,6492})for(int cut:{0,6,12}){const double db=attenuation(hz,cut);notchError=std::max(notchError,std::abs(db+cut));assert(std::abs(db+cut)<.08);}
    {
        auto k=opened(2,320,700);int16_t samples[256]{};
        for(int b=0;b<400;b++){for(int i=0;i<128;i++)samples[2*i]=samples[2*i+1]=pcm(.3*std::sin(2*Pi*997*(b*128+i)/48000));process(*k,samples,128);}
        k->ownerMute(true);std::fill(std::begin(samples),std::end(samples),30000);process(*k,samples,128);assert(zero(samples,256));
        k->ownerMute(false);for(int b=0;b<500;b++){std::fill(std::begin(samples),std::end(samples),0);process(*k,samples,128);assert(zero(samples,256));}
        k->notch(0,1000,12);k->configure(4,360,700,true,false);process(*k,samples,128);assert(zero(samples,256));
        const auto requested=k->snapshot();k->resetAfterStop();const auto reset=k->snapshot();
        assert(reset.muted&&reset.profile==requested.profile&&reset.wet==requested.wet&&reset.monitorGain==requested.monitorGain);
        k->configure(4,360,700,false,false);std::fill(std::begin(samples),std::end(samples),4000);process(*k,samples,128);assert(zero(samples,256));
        k->ownerMute(false);prime(*k);for(int b=0;b<100;b++){std::fill(std::begin(samples),std::end(samples),0);process(*k,samples,128);assert(zero(samples,256));}
    }
    {
        auto reference=opened(2,320,700),lower=opened(2,320,700);int16_t a[256]{},b[256]{};
        for(int block=0;block<400;block++){
            for(int i=0;i<128;i++)a[2*i]=a[2*i+1]=b[2*i]=b[2*i+1]=pcm(.07*std::sin(2*Pi*997*(block*128+i)/48000));
            process(*reference,a,128);process(*lower,b,128);
        }
        lower->configure(2,320,350,false,false);int audibleTail=0;
        for(int block=0;block<20;block++){
            std::fill(std::begin(a),std::end(a),0);std::fill(std::begin(b),std::end(b),0);
            process(*reference,a,128);process(*lower,b,128);
            if(block>=2)for(int i=0;i<256;i++){assert(std::abs(2*int(b[i])-int(a[i]))<=2);audibleTail+=a[i]!=0;}
        }
        assert(audibleTail>0); // Old tail scales down, rather than remaining at the old level.
    }
    {
        auto whole=opened(2,320,700),split=opened(2,320,700);uint32_t noise=7;
        for(int at=0;at<96000;at+=128){
            for(auto* k:{whole.get(),split.get()}){
                if(at==128)k->notch(0,1000,6);
                if(at==256)k->notch(0,1100,12); // New request while the old fade is in flight.
                if(at==384)k->configure(4,360,700,false,false);
                if(at==512)k->configure(5,180,500,false,false); // Profile coalescing during fade.
                if(at==640)k->configure(5,180,500,false,true);
                if(at==768)k->ownerMute(true);
                if(at==896)k->ownerMute(false);
                if(at==1024)k->configure(1,250,700,false,false);
            }
            int16_t a[256]{},b[256]{};
            for(int i=0;i<256;i++){noise=1664525*noise+1013904223;a[i]=b[i]=int16_t(int(noise>>16)/3-10000);}
            process(*whole,a,128);int offset=0;
            while(offset<128){noise=1664525*noise+1013904223;const int n=std::min(128-offset,1+int(noise%31));process(*split,b+2*offset,n);offset+=n;}
            for(int i=0;i<256;i++)assert(a[i]==b[i]);
        }
        assert(whole->snapshot().frames==split->snapshot().frames&&whole->snapshot().appliedRevision==split->snapshot().appliedRevision);
    }
    uint32_t maximum=0;
    {
        auto k=opened();uint32_t random=91;int16_t block[256]{};
        for(int p=0;p<7;p++)for(int gain:{0,1,10,500,1000})for(int wet:{0,160,360}){
            k->configure(p,wet,gain,false,false);
            for(int b=0;b<120;b++){
                for(auto& x:block){random=1664525*random+1013904223;x=pcm((int(random>>16)-32768)/32768.*gain/1000.);}
                process(*k,block,128);for(auto x:block){maximum=std::max(maximum,uint32_t(std::abs(int(x))));assert(std::abs(int(x))<=gain*32768/1000);}
            }
        }
        // Sixty synthetic seconds, with an actual profile change every ten
        // seconds and repeated 48k fixed-Q filters, exercises long-lived state.
        k->configure(1,250,700,false,false);
        for(int at=0;at<48000*60;at+=128){
            if(at%(48000*10)==0){const int p=1+at/(48000*10);k->configure(p,fridaywet::Presets[p].defaultWet*1000,700,false,false);k->notch(0,1000,6);}
            for(int i=0;i<128;i++)block[2*i]=block[2*i+1]=pcm(.3*std::sin(2*Pi*997*(at+i)/48000));
            process(*k,block,128);for(auto x:block)assert(std::abs(int(x))<=22938);
        }
        assert(k->snapshot().frames>=48000*60&&k->snapshot().peak<=22938);
    }
    {
        auto k=opened(2,320,700);std::atomic<bool> start{false};
        std::thread control([&]{while(!start.load(std::memory_order_acquire)){}for(int i=0;i<10000;i++){k->configure(i%7,i%361,i%1001,i%11==0,i%13==0);k->notch(i%8,188+i%6305,i%13);k->ownerMute(i%7==0);}});
        start.store(true,std::memory_order_release);int16_t block[256]{};
        for(int b=0;b<10000;b++){std::fill(std::begin(block),std::end(block),10000);k->process(block,128);const auto s=k->snapshot();assert(s.peak<=32768&&s.requestedRevision<=InlineVoiceKernel::RevisionMask&&s.appliedRevision<=InlineVoiceKernel::RevisionMask);}
        control.join();k->ownerMute(true);std::fill(std::begin(block),std::end(block),30000);process(*k,block,128);assert(zero(block,256));
    }
    assert(sizeof(InlineVoiceKernel)<450000);
    std::cout<<"{\"inline_unit_tests\":\"passed\",\"notch_center_max_error_db\":"<<notchError<<",\"same_frame_dry\":true,\"dry_gain_applied_twice\":false,\"callback_allocations\":0,\"partition_exact\":true,\"long_run_seconds\":60,\"maximum_pcm_peak\":"<<maximum<<",\"kernel_bytes\":"<<sizeof(InlineVoiceKernel)<<",\"device_audio_actions\":false}\n";
}
