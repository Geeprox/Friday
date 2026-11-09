#pragma once
#include "WetDsp.h"
#include "VoiceMixBudget.h"
#include <atomic>
#include <cstring>

namespace fridaywet {

// Sole audio-thread owner of causal effects. Input is the existing system
// output block AFTER VolUnit, not a second dry-monitor stream. No FIFO, I/O,
// locks, allocations or lookahead. Only the owner can release its separate gate.
class InlineVoiceKernel {
public:
    static constexpr uint32_t RevisionMask=0xfffff;
    static constexpr int RampFrames=240;
    struct Snapshot {
        uint32_t requestedRevision=0,appliedRevision=0;
        int profile=0,wet=0,monitorGain=0;
        bool muted=true,bypass=true;
        uint64_t callbacks=0,frames=0;
        uint32_t peak=0;
        uint32_t notchRequestedRevision=0,notchAppliedRevision=0;
    };
    InlineVoiceKernel(){for(auto& request:notchRequests_)request.store(notchWord(1000,0,0),std::memory_order_relaxed);}
    ~InlineVoiceKernel(){resetAfterStop();}
    bool configure(int profile,int wetPermille,int monitorGainPermille,bool muted,bool bypass){
        if(!validProfile(profile)||wetPermille<0||wetPermille>360||monitorGainPermille<0||monitorGainPermille>1000)return false;
        uint64_t prior=command_.load(std::memory_order_relaxed);
        for(;;){const auto old=Command::unpack(prior);Command next{profile,wetPermille,monitorGainPermille,muted,bypass,old.revision};
            if(next.pack()==prior)return true;
            next.revision=(old.revision+1)&RevisionMask;
            if(command_.compare_exchange_weak(prior,next.pack(),std::memory_order_release,std::memory_order_relaxed)){
                if(muted)muteEpoch_.fetch_add(1,std::memory_order_release);
                return true;
            }
        }
    }
    void ownerMute(bool value){
        ownerMuted_.store(value,std::memory_order_release);
        // Even a revoke/rebind pulse inside one callback cannot leave that
        // callback's previous-owner PCM in the caller's block.
        if(value)muteEpoch_.fetch_add(1,std::memory_order_release);
    }
    bool notch(int slot,int frequencyHz,int cutDb){
        if(slot<0||slot>=8||frequencyHz<188||frequencyHz>6492||cutDb<0||cutDb>12)return false;
        auto& request=notchRequests_[slot];uint64_t prior=request.load(std::memory_order_relaxed);
        for(;;){if(notchHz(prior)==frequencyHz&&notchCut(prior)==cutDb)return true;
            const uint64_t next=notchWord(frequencyHz,cutDb,((prior>>17)+1)&RevisionMask);
            if(request.compare_exchange_weak(prior,next,std::memory_order_release,std::memory_order_relaxed)){
                notchRevision_.fetch_add(1,std::memory_order_release);return true;
            }
        }
    }
    void process(int16_t* stereo,int frames){
        if(!stereo||frames<=0)return;
        const auto epoch=muteEpoch_.load(std::memory_order_acquire);
        const uint64_t packed=command_.load(std::memory_order_acquire);const auto c=Command::unpack(packed);
        const auto notchRevision=notchRevision_.load(std::memory_order_acquire);
        std::array<uint64_t,8> requests{};
        for(int n=0;n<8;n++)requests[n]=notchRequests_[n].load(std::memory_order_acquire);
        if(ownerMuted_.load(std::memory_order_acquire)||c.muted||c.gain==0){
            silence(stereo,frames);
            // Quiet is a safe time to install coefficients without a transient.
            if(audioCommand_!=packed||!silent_){clearAudio();wet_[0].select(static_cast<Profile>(c.profile));audioCommand_=packed;}
            for(int n=0;n<8;n++)filters_[n].prepareQuiet(requests[n]);
            silent_=true;gain_.reset(c.gain/1000.f);
            appliedRevision_.store(c.revision,std::memory_order_release);
            acknowledgeNotches(notchRevision,requests);
            return;
        }
        if(silent_){silent_=false;audible_.reset(0);audible_.aim(1);}
        audioCommand_=packed;
        ratio_.aim(c.profile>0&&!c.bypass?c.wet/1000.f:0.f);gain_.aim(c.gain/1000.f);
        for(int n=0;n<8;n++)filters_[n].aim(requests[n]);
        const float ceiling=c.gain/1000.f;const int pcmCeiling=c.gain*32768/1000;uint32_t peak=0;
        for(int i=0;i<frames;i++){
            if(!profileFade_&&int(wet_[active_].profile())!=c.profile){
                wet_[1-active_].select(static_cast<Profile>(c.profile));profileFade_=RampFrames;
            }
            const float ratio=ratio_.tick(),wetGain=gain_.tick(),audible=audible_.tick();
            const float left=stereo[2*i]/32768.f,right=stereo[2*i+1]/32768.f;
            float wl=0,wr=0;
            if(ratio>0||profileFade_){
                // Normalize only the wet excitation; multiply its return by
                // the current level. Dry is never multiplied by monitorGain a
                // second time, and an old tail follows a reduced target level.
                wet_[active_].process(left/ceiling,right/ceiling,wl,wr);
                if(profileFade_){
                    float nl=0,nr=0;wet_[1-active_].process(left/ceiling,right/ceiling,nl,nr);
                    const float blend=(RampFrames-profileFade_+1)/float(RampFrames);
                    wl+=(nl-wl)*blend;wr+=(nr-wr)*blend;
                    if(--profileFade_==0){wet_[active_].clear();active_=1-active_;}
                }
                wetDormant_=false;
            }else if(!wetDormant_){wet_[0].clear();wet_[1].clear();wetDormant_=true;}
            const float scale=audible/(1+ratio);
            double l=(left+wl*wetGain*ratio)*scale,r=(right+wr*wetGain*ratio)*scale;
            for(auto& filter:filters_)filter.process(l,r);
            for(int ch=0;ch<2;ch++){
                const double value=ch?r:l;
                const float safe=std::isfinite(value)?float(std::clamp(value,-16.,16.)):0.f;
                const int sample=std::clamp(int(std::lround(softVoiceLimit(safe,ceiling)*32768.f)),-pcmCeiling,std::min(32767,pcmCeiling));
                stereo[2*i+ch]=int16_t(sample);peak=std::max(peak,uint32_t(std::abs(sample)));
            }
        }
        const auto latest=Command::unpack(command_.load(std::memory_order_acquire));
        if(ownerMuted_.load(std::memory_order_acquire)||latest.muted||latest.gain==0||muteEpoch_.load(std::memory_order_acquire)!=epoch){
            clearAudio();silent_=true;silence(stereo,frames);
            appliedRevision_.store((latest.revision-1)&RevisionMask,std::memory_order_release);return;
        }
        // A stricter cap arriving while this block was computed also bounds
        // the block before the caller writes it; it does not reapply dry gain.
        if(latest.gain<c.gain){
            const int maximum=latest.gain*32768/1000;peak=0;
            for(int i=0;i<frames*2;i++){stereo[i]=int16_t(std::clamp(int(stereo[i]),-maximum,std::min(32767,maximum)));peak=std::max(peak,uint32_t(std::abs(int(stereo[i]))));}
        }
        peak_.store(peak,std::memory_order_relaxed);callbacks_.fetch_add(1,std::memory_order_release);frames_.fetch_add(uint64_t(frames),std::memory_order_release);
        if(!ratio_.remaining&&!gain_.remaining&&!audible_.remaining&&!profileFade_&&int(wet_[active_].profile())==c.profile&&command_.load(std::memory_order_acquire)==packed)
            appliedRevision_.store(c.revision,std::memory_order_release);
        acknowledgeNotches(notchRevision,requests);
    }
    Snapshot snapshot()const{
        const auto c=Command::unpack(command_.load(std::memory_order_acquire));Snapshot s;
        s.requestedRevision=c.revision;s.appliedRevision=appliedRevision_.load(std::memory_order_acquire);
        s.profile=c.profile;s.wet=c.wet;s.monitorGain=c.gain;s.muted=c.muted||ownerMuted_.load(std::memory_order_acquire);s.bypass=c.bypass;
        s.callbacks=callbacks_.load(std::memory_order_acquire);s.frames=frames_.load(std::memory_order_acquire);s.peak=peak_.load(std::memory_order_relaxed);
        s.notchRequestedRevision=notchRevision_.load(std::memory_order_acquire)&RevisionMask;s.notchAppliedRevision=notchAppliedRevision_.load(std::memory_order_acquire);return s;
    }
    // Caller must have stopped/joined the sole producer. Preserve requested
    // sound settings; a new owner starts closed with no previous-session notch.
    void resetAfterStop(){
        ownerMute(true);clearAudio();silent_=true;audioCommand_=~uint64_t(0);
        for(int n=0;n<8;n++){
            const auto prior=notchRequests_[n].load(std::memory_order_relaxed);
            const auto flat=notchWord(1000,0,((prior>>17)+1)&RevisionMask);
            notchRequests_[n].store(flat,std::memory_order_release);filters_[n].prepareQuiet(flat);
        }
        const auto revision=notchRevision_.fetch_add(1,std::memory_order_release)+1;
        notchAppliedRevision_.store((revision-1)&RevisionMask,std::memory_order_release);
        appliedRevision_.store((Command::unpack(command_.load()).revision-1)&RevisionMask,std::memory_order_release);peak_.store(0);
    }
private:
    static_assert(std::atomic<uint64_t>::is_always_lock_free&&std::atomic<uint32_t>::is_always_lock_free&&std::atomic<bool>::is_always_lock_free,"inline controls must be lock-free");
    struct Command {
        int profile=0,wet=0,gain=0;bool muted=true,bypass=true;uint32_t revision=0;
        uint64_t pack()const{return uint64_t(profile)|(uint64_t(wet)<<3)|(uint64_t(gain)<<12)|(uint64_t(muted)<<22)|(uint64_t(bypass)<<23)|(uint64_t(revision)<<24);}
        static Command unpack(uint64_t word){return {int(word&7),int((word>>3)&511),int((word>>12)&1023),bool((word>>22)&1),bool((word>>23)&1),uint32_t((word>>24)&RevisionMask)};}
    };
    struct Ramp {
        float value=0,target=0,step=0;int remaining=0;
        void reset(float v){value=target=v;remaining=0;step=0;}
        void aim(float v){if(v!=target){target=v;remaining=RampFrames;step=(target-value)/RampFrames;}}
        float tick(){if(remaining){value+=step;if(--remaining==0)value=target;}return value;}
    };
    static constexpr uint64_t notchWord(int hz,int cut,uint64_t revision){return uint64_t(hz)|(uint64_t(cut)<<13)|(revision<<17);}
    static int notchHz(uint64_t word){return int(word&8191);}
    static int notchCut(uint64_t word){return int((word>>13)&15);}
    struct Biquad {
        double b0=1,b1=0,b2=0,a1=0,a2=0;std::array<double,2> z1{},z2{};bool flat=true;
        void clear(){z1={};z2={};}
        void set(int hz,int cut){
            clear();flat=cut==0;if(flat){b0=1;b1=b2=a1=a2=0;return;}
            constexpr double pi=3.1415926535897932384626433832795;
            const double w=2*pi*hz/48000,a=std::pow(10.,-cut/40.),alpha=std::sin(w)/12.,a0=1+alpha/a;
            b0=(1+alpha*a)/a0;b1=-2*std::cos(w)/a0;b2=(1-alpha*a)/a0;a1=b1;a2=(1-alpha/a)/a0;
        }
        double process(double x,int ch){
            if(flat)return x;
            const double y=b0*x+z1[ch],next1=b1*x-a1*y+z2[ch],next2=b2*x-a2*y;
            if(!std::isfinite(y)||!std::isfinite(next1)||!std::isfinite(next2)){clear();return 0;}
            z1[ch]=std::abs(next1)<1e-24?0:next1;z2[ch]=std::abs(next2)<1e-24?0:next2;return y;
        }
    };
    struct Filter {
        Biquad current{},next{};uint64_t applied=notchWord(1000,0,0),target=applied,pending=applied;int remaining=0;
        void clear(){current.clear();next.clear();remaining=0;}
        void prepareQuiet(uint64_t request){if(applied!=request||remaining){current.set(notchHz(request),notchCut(request));next=current;applied=target=pending=request;remaining=0;}else{pending=request;current.clear();next.clear();}}
        void aim(uint64_t request){pending=request;}
        void process(double& l,double& r){
            if(!remaining&&pending!=applied){next.set(notchHz(pending),notchCut(pending));target=pending;remaining=RampFrames;}
            if(!remaining){l=current.process(l,0);r=current.process(r,1);return;}
            const double blend=(RampFrames-remaining+1)/double(RampFrames);
            const double a=current.process(l,0),b=current.process(r,1);
            l=a+(next.process(l,0)-a)*blend;r=b+(next.process(r,1)-b)*blend;
            if(--remaining==0){current=next;applied=target;}
        }
    };
    std::atomic<uint64_t> command_{Command{}.pack()};std::atomic<bool> ownerMuted_{true};std::atomic<uint32_t> muteEpoch_{0};
    std::array<std::atomic<uint64_t>,8> notchRequests_{};std::atomic<uint32_t> notchRevision_{0},notchAppliedRevision_{0},appliedRevision_{0},peak_{0};
    std::atomic<uint64_t> callbacks_{0},frames_{0};
    WetDsp wet_[2];std::array<Filter,8> filters_{};Ramp ratio_{},gain_{},audible_{};
    int active_=0,profileFade_=0;bool silent_=true,wetDormant_=true;uint64_t audioCommand_=~uint64_t(0);
    void clearAudio(){for(auto& wet:wet_)wet.select(Profile::Dry);for(auto& filter:filters_)filter.clear();active_=profileFade_=0;ratio_.reset(0);gain_.reset(0);audible_.reset(0);wetDormant_=true;audioCommand_=~uint64_t(0);}
    void silence(int16_t* output,int count){std::memset(output,0,size_t(count)*2*sizeof(int16_t));peak_.store(0,std::memory_order_relaxed);callbacks_.fetch_add(1,std::memory_order_release);frames_.fetch_add(uint64_t(count),std::memory_order_release);}
    void acknowledgeNotches(uint32_t revision,const std::array<uint64_t,8>& requests){
        if(notchRevision_.load(std::memory_order_acquire)!=revision)return;
        for(int n=0;n<8;n++)if(filters_[n].remaining||filters_[n].applied!=requests[n]||notchRequests_[n].load(std::memory_order_acquire)!=requests[n])return;
        notchAppliedRevision_.store(revision&RevisionMask,std::memory_order_release);
    }
};
}
