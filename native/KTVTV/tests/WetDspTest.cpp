#include "../android/cpp/wet/WetDsp.h"
#include "../android/cpp/wet/WetKernel.h"
#include "../android/cpp/wet/WetHealth.h"
#include <cassert>
#include <iostream>
#include <thread>
#include <memory>
#include <random>
#include <chrono>
#include <cstdlib>
#include <limits>

static std::atomic<uint64_t> allocations{0};
void* operator new(std::size_t n){allocations.fetch_add(1);if(void* p=std::malloc(n))return p;throw std::bad_alloc();}
void operator delete(void* p)noexcept{std::free(p);}
void operator delete(void* p,std::size_t)noexcept{std::free(p);}
using namespace fridaywet;
static bool zero(const int16_t* p,int n){for(int i=0;i<n;i++)if(p[i])return false;return true;}
static double toneEnergy(WetDsp& dsp,Profile profile,float amplitude,float frequency){
    constexpr int Rate=WetDsp::Rate;
    dsp.select(profile);double energy=0;
    for(int i=0;i<Rate*3;i++){
        const float input=amplitude*std::sin(2*3.141592653589793*frequency*i/Rate);float l,r;
        dsp.process(input,-input,l,r);
        if(i>=Rate*2)energy+=double(l)*l+double(r)*r;
    }
    return energy;
}

int main(){
    const auto begin=std::chrono::steady_clock::now();
    std::array<int,ProfileCount> first{};first.fill(-1);
    std::array<double,ProfileCount> early{},tail{},late{},checksum{};
    auto dsp=std::make_unique<WetDsp>();
    for(int profile=0;profile<ProfileCount;profile++){
        dsp->select(static_cast<Profile>(profile));
        for(int i=0;i<48000*7;i++){
            float l=0,r=0;dsp->process(i==0?1.f:0.f,i==0?-1.f:0.f,l,r);
            assert(std::isfinite(l)&&std::isfinite(r));
            assert(std::abs(l)<=.9f&&std::abs(r)<=.9f);
            if(i==0)assert(l==0&&r==0); // No first-sample dry term in any preset.
            if(first[profile]<0&&(l!=0||r!=0))first[profile]=i;
            if(i<24000)early[profile]+=double(l)*l+double(r)*r;
            if(i>=24000&&i<48000*2)tail[profile]+=double(l)*l+double(r)*r;
            if(i>48000*6)late[profile]+=double(l)*l+double(r)*r;
            if(i<48000)checksum[profile]+=(l-r)*(i%127);
        }
        if(profile==0){assert(first[profile]==-1&&early[profile]==0);}
        else{
            const auto& preset=Presets[profile];
            const int onset=profile==int(Profile::Echo)?int(preset.echoLeftMs*48):int(std::lround(preset.preDelayMs*48))+383;
            assert(first[profile]==onset&&onset>0);
            assert(early[profile]>0&&tail[profile]>0&&late[profile]<early[profile]*.0001);
        }
    }
    for(int a=1;a<ProfileCount;a++)for(int b=a+1;b<ProfileCount;b++)assert(std::abs(checksum[a]-checksum[b])>.001);
    // The small room has substantially less late energy than the existing pop room.
    assert(tail[5]/early[5]<tail[1]/early[1]*.25);
    assert(tail[4]/early[4]>tail[2]/early[2]); // Opera retains its longer-tail identity.

    // A left-only slapback crosses to the other channel only after both delays;
    // each repeat loses energy, and no undelayed/cancelled mono copy is emitted.
    dsp->select(Profile::Echo);int leftOnset=-1,rightOnset=-1;
    double echoFirst=0,echoRepeat=0,echoSecondRepeat=0;
    for(int i=0;i<48000;i++){
        float l,r;dsp->process(i==0?.2f:0.f,0,l,r);
        if(l!=0&&leftOnset<0)leftOnset=i;if(r!=0&&rightOnset<0)rightOnset=i;
        if(i>=3744&&i<4224)echoFirst+=double(l)*l;
        if(i>=8736&&i<9216)echoRepeat+=double(r)*r;
        if(i>=12480&&i<12960)echoSecondRepeat+=double(l)*l;
    }
    assert(leftOnset==3744&&rightOnset==8736);
    assert(echoRepeat>echoFirst*.001&&echoRepeat<echoFirst*.1);
    assert(echoSecondRepeat>0&&echoSecondRepeat<echoRepeat*.1);

    // Causal input shaping suppresses rumble and harsh treble. Quiet singing
    // stays linear while a sustained loud phrase lowers its wet/dry ratio.
    const double quiet=toneEnergy(*dsp,Profile::Studio,.04f,997);
    const double quietDouble=toneEnergy(*dsp,Profile::Studio,.08f,997);
    const double loud=toneEnergy(*dsp,Profile::Studio,.60f,997);
    const double loudRatio=loud/(quiet*225);
    assert(quiet>0&&std::abs(quietDouble/quiet-4)<.01);
    assert(loudRatio>.1&&loudRatio<.65);
    double phraseTail=0,tailEnd=0,recovered=0;
    for(int i=0;i<48000*2;i++){
        float l,r;dsp->process(0,0,l,r);const double e=double(l)*l+double(r)*r;
        if(i<24000)phraseTail+=e;if(i>72000)tailEnd+=e;
    }
    assert(phraseTail>quiet*.01&&tailEnd<quiet*.0001);
    for(int i=0;i<48000*3;i++){
        const float x=.04f*std::sin(2*3.141592653589793*997*i/48000);float l,r;dsp->process(x,-x,l,r);
        if(i>=48000*2)recovered+=double(l)*l+double(r)*r;
    }
    assert(std::abs(recovered/quiet-1)<.001); // Wet duck releases without truncating the tail.
    const double echoMid=toneEnergy(*dsp,Profile::Echo,.04f,997);
    assert(toneEnergy(*dsp,Profile::Echo,.04f,40)<echoMid*.3);
    assert(toneEnergy(*dsp,Profile::Echo,.04f,18000)<echoMid*.3);

    // Invalid floats never poison subsequent samples; huge finite input is
    // bounded before arithmetic, and selecting/clearing resets every state.
    for(int p=1;p<ProfileCount;p++){
        dsp->select(static_cast<Profile>(p));
        for(int i=0;i<48000;i++){float l,r;dsp->process(NAN,INFINITY,l,r);assert(l==0&&r==0);}
        for(int i=0;i<48000*2;i++){
            const float x=i%5?((i&1)?std::numeric_limits<float>::max():-std::numeric_limits<float>::max()):NAN;
            float l,r;dsp->process(x,-x,l,r);
            assert(std::isfinite(l)&&std::isfinite(r)&&std::abs(l)<=.9f&&std::abs(r)<=.9f);
        }
        dsp->clear();
        for(int i=0;i<8192;i++){float l,r;dsp->process(0,0,l,r);assert(l==0&&r==0);}
    }
    dsp->select(static_cast<Profile>(255));assert(dsp->profile()==Profile::Dry);
    // A single channel is preserved; opposite polarity also produced nonzero wet above.
    dsp->select(Profile::Ktv);double rightOnly=0;
    for(int i=0;i<48000;i++){float l,r;dsp->process(0,i==0?1.f:0.f,l,r);rightOnly+=l*l+r*r;}assert(rightOnly>0);
    dsp->clear();for(int i=0;i<48000;i++){float l,r;dsp->process(0,0,l,r);assert(l==0&&r==0);}

    // Six minutes of deterministic, stationary excitation exercise every FDN
    // and echo recurrence without reset. Each then decays through twelve seconds
    // of silence; no persistent self-oscillation or numeric state can hide behind
    // per-block clearing. These samples are never played or written to a file.
    const auto beforeLongRun=allocations.load();uint32_t state=20261104;double longRunPeak=0;
    for(int p=1;p<ProfileCount;p++){
        dsp->select(static_cast<Profile>(p));double initialEnergy=0,finalEnergy=0;
        for(int i=0;i<WetDsp::Rate*60;i++){
            state=1664525*state+1013904223;
            const float x=.3f*std::sin(2*3.141592653589793*997*i/48000)+.04f*(int(state>>16)-32768)/32768.f;
            float l,r;dsp->process(x,-x,l,r);const double e=double(l)*l+double(r)*r;
            assert(std::isfinite(e)&&std::abs(l)<=.9f&&std::abs(r)<=.9f);
            longRunPeak=std::max(longRunPeak,double(std::max(std::abs(l),std::abs(r))));
            if(i>=WetDsp::Rate*5&&i<WetDsp::Rate*7)initialEnergy+=e;
            if(i>=WetDsp::Rate*58)finalEnergy+=e;
        }
        assert(initialEnergy>0&&finalEnergy>initialEnergy*.8&&finalEnergy<initialEnergy*1.2);
        for(int i=0;i<WetDsp::Rate*12;i++){float l,r;dsp->process(0,0,l,r);assert(std::isfinite(l)&&std::isfinite(r));if(i>WetDsp::Rate*10)assert(std::abs(l)<1e-7&&std::abs(r)<1e-7);}
    }
    assert(allocations.load()==beforeLongRun);

    auto kernel=std::make_unique<WetKernel>();int16_t input[256]{},output[512]{};uint64_t ns=1000000000ULL;
    assert(!kernel->configure(7,300,200,false,false)&&!kernel->configure(-1,300,200,false,false));
    assert(!kernel->configure(1,1001,200,false,false));
    for(int p=0;p<ProfileCount;p++){
        Command c;c.profile=p;c.wet=1000;c.monitorGain=999;c.bypass=false;c.muted=false;c.epoch=0x7ffff;c.revision=0xfffff;
        const auto copy=Command::unpack(c.pack());
        assert(copy.profile==p&&copy.wet==1000&&copy.monitorGain==999&&!copy.bypass&&!copy.muted&&copy.epoch==c.epoch&&copy.revision==c.revision);
        assert(copy.active()==(p!=0));
    }
    assert(kernel->configure(1,300,200,false,false));
    const auto before=allocations.load();
    for(int block=0;block<800;block++){
        for(int i=0;i<128;i++)input[2*i]=input[2*i+1]=int16_t(18000*std::sin(2*3.141592653589793*997*(block*128+i)/48000));
        kernel->push(input,128,ns);kernel->render(output,128,ns,true);ns+=2666667;
    }
    assert(allocations.load()==before);assert(kernel->totals.nonzero.load()>0);
    auto clears=kernel->totals.clears.load();
    assert(kernel->configure(2,320,200,false,false));
    kernel->push(input,128,ns);kernel->render(output,128,ns,true);ns+=2666667;
    assert(kernel->totals.transitionFrames.load()==112);assert(kernel->totals.clears.load()==clears);assert(!zero(output,256));
    kernel->push(input,128,ns);kernel->render(output,128,ns,true);ns+=2666667;
    assert(kernel->totals.transitionFrames.load()==0&&kernel->totals.clears.load()==clears+1);
    assert(kernel->totals.currentProfile.load()==2&&kernel->totals.currentEpoch.load()==int(kernel->command().epoch));
    // Wet gain changes take 240 frames even for a tiny amplitude delta.
    kernel->configure(2,321,200,false,false);kernel->push(input,128,ns);kernel->render(output,128,ns,true);ns+=2666667;
    assert(kernel->totals.gainRampFrames.load()==112);
    kernel->push(input,128,ns);kernel->render(output,128,ns,true);ns+=2666667;assert(kernel->totals.gainRampFrames.load()==0);

    // Dry, mute, zero gain, bypass and unhealthy callback have no wet or dry passthrough.
    for(int reason=0;reason<5;reason++){
        kernel->configure(2,400,500,false,false);
        for(int b=0;b<200;b++){kernel->push(input,128,ns);kernel->render(output,128,ns,true);ns+=2666667;}
        if(reason==0)kernel->configure(0,0,500,false,false);
        if(reason==1)kernel->configure(2,400,500,true,false);
        if(reason==2)kernel->configure(2,400,0,false,false);
        if(reason==3)kernel->bypass();
        kernel->push(input,128,ns);kernel->render(output,128,ns,reason!=4);ns+=2666667;assert(zero(output,256));
        kernel->configure(2,400,500,false,false);
        int16_t silence[256]{};
        for(int b=0;b<500;b++){kernel->push(silence,128,ns);kernel->render(output,128,ns,true);ns+=2666667;assert(zero(output,256));}
    }
    // Queue never grows beyond 1024 frames and does not turn backlog into long echo.
    for(int i=0;i<40;i++)kernel->push(input,128,ns);
    assert(kernel->queueDepth()<=FrameQueue::Capacity&&kernel->totals.producerDropped.load()>0);
    kernel->render(output,128,ns,true);assert(kernel->queueDepth()<=256&&kernel->totals.consumerDropped.load()>0);
    kernel->push(input,128,ns);kernel->render(output,128,ns+100000000ULL,true);assert(kernel->totals.staleFrames.load()>0);
    kernel->clearAfterStop();assert(kernel->queueDepth()==0);

    // All profiles, including the new pair, switch with the same 240-frame wet
    // fade. The old tail cannot survive selection or emerge during new silence.
    const auto beforeSwitch=allocations.load();
    int16_t silence[256]{};
    for(int p=1;p<ProfileCount;p++){
        kernel->configure(p,360,700,false,false);
        for(int b=0;b<160;b++){kernel->push(input,128,ns);kernel->render(output,128,ns,true);ns+=2666667;}
        const int next=p==ProfileCount-1?1:p+1;
        kernel->configure(next,360,700,false,false);
        kernel->push(silence,128,ns);kernel->render(output,128,ns,true);ns+=2666667;
        assert(kernel->totals.transitionFrames.load()==112);
        kernel->push(silence,128,ns);kernel->render(output,128,ns,true);ns+=2666667;
        assert(kernel->totals.transitionFrames.load()==0&&kernel->totals.currentProfile.load()==next);
        for(int b=0;b<96;b++){kernel->push(silence,128,ns);kernel->render(output,128,ns,true);ns+=2666667;assert(zero(output,256));}
    }
    assert(allocations.load()==beforeSwitch);

    // Full-scale randomized input and every preset remain finite and bounded.
    std::mt19937 random(5);std::uniform_int_distribution<int> sample(-32768,32767);
    for(int p=1;p<ProfileCount;p++){
        kernel->configure(p,1000,1000,false,false);
        for(int b=0;b<800;b++){
            for(auto& x:input)x=int16_t(sample(random));kernel->push(input,128,ns);kernel->render(output,128,ns,true);ns+=2666667;
            for(int i=0;i<256;i++)assert(std::abs(int(output[i]))<=29491);
        }
    }
    // The output's actual per-sample ceiling and OEM dry setpoint share the
    // existing voice budget at each default wet, maximum wet and monitor level.
    for(int p=1;p<ProfileCount;p++)for(int gain:{0,1,20,70,100})for(int wet:{int(std::lround(Presets[p].defaultWet*1000)),1000}){
        kernel->clearAfterStop();kernel->configure(p,wet,gain*10,false,false);
        const float ceiling=wetCeiling(gain*10,wet);const int dry=dryBudget(gain,wet);
        const int maximum=int(std::ceil(ceiling*32767));
        assert(dry/100.f+ceiling<=gain/100.f+.000001f);
        for(int b=0;b<160;b++){
            for(int i=0;i<128;i++){
                const int t=b*128+i;
                input[2*i]=input[2*i+1]=int16_t(std::lround(32767*(.72*std::sin(2*3.141592653589793*997*t/48000)+.28*std::sin(2*3.141592653589793*187*t/48000))));
            }
            kernel->push(input,128,ns);kernel->render(output,128,ns,true);ns+=2666667;
            for(int i=0;i<256;i++){
                assert(std::abs(int(output[i]))<=maximum);
                // Align the dry sample's polarity with each actual wet sample:
                // this is the worst possible summation under a unit dry bound.
                const double mixedPeak=dry/100.+std::abs(int(output[i]))/32768.;
                assert(mixedPeak<=gain/100.+1./32768);
            }
        }
    }
    // Model the exact native watchdog thresholds, including a concurrent future timestamp.
    assert(healthFault(2000000000ULL,1000000000ULL,1999999999ULL,1999999999ULL,1999999999ULL,false)==NoFault);
    assert(healthFault(2000000000ULL,1000000000ULL,1800000000ULL,1999999999ULL,1999999999ULL,false)==InputStale);
    assert(healthFault(2000000000ULL,1000000000ULL,1999999999ULL,1700000000ULL,1999999999ULL,false)==CallbackStale);
    assert(healthFault(2000000000ULL,1000000000ULL,1999999999ULL,1999999999ULL,1100000000ULL,false)==RouteStale);
    assert(healthFault(2000000000ULL,1000000000ULL,1999999999ULL,2000000001ULL,2000000001ULL,false)==NoFault);
    assert(healthFault(2000000000ULL,1000000000ULL,1999999999ULL,1999999999ULL,1999999999ULL,true)==OutputFailure);

    // Actual concurrent SPSC stress. TSAN build runs this same workload separately.
    FrameQueue queue;std::atomic<bool> done{false};std::atomic<int> received{0};
    std::thread producer([&]{for(int i=1;i<=100000;i++){int16_t pair[2]{int16_t(i%30000),int16_t(-(i%30000))};while(!queue.push(pair,1,uint64_t(i),7))std::this_thread::yield();}done.store(true);});
    uint64_t last=0;while(!done.load()||queue.depth()){
        Frame f;if(queue.pop(f)){assert(f.readAtNs>last&&f.epoch==7&&f.l==-f.r);last=f.readAtNs;received.fetch_add(1);}else std::this_thread::yield();
    }
    producer.join();assert(received.load()==100000);queue.clearAfterStop();
    const auto elapsed=std::chrono::duration<double,std::milli>(std::chrono::steady_clock::now()-begin).count();
    std::cout<<"{\"synthetic_checks\":\"passed\",\"preset_count\":"<<ProfileCount<<",\"first_wet_frames\":[";
    for(int p=0;p<ProfileCount;p++)std::cout<<(p?",":"")<<first[p];
    std::cout<<"],\"sustained_loud_normalized_wet_energy\":"<<loudRatio<<",\"echo_repeat_energy_ratio\":"<<echoRepeat/echoFirst
        <<",\"callback_allocations\":0,\"concurrent_frames\":"<<received.load()<<",\"profile_fade_out_frames\":240,\"gain_ramp_frames\":240,\"host_elapsed_ms\":"<<elapsed
        <<",\"long_run_seconds\":360,\"long_run_peak\":"<<longRunPeak
        <<",\"dry_passthrough\":false,\"device_or_acoustic_test\":false}"<<std::endl;
}
