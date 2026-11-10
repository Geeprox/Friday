#pragma once
#include "WetDsp.h"
#include "VoiceMixBudget.h"
#include <atomic>
#include <cstring>

namespace fridaywet {
struct Command {
    int profile=0,wet=0,monitorGain=0;bool bypass=true,muted=true;uint32_t epoch=0,revision=0;
    bool active()const{return profile>0&&validProfile(profile)&&wet>0&&monitorGain>0&&!bypass&&!muted;}
    uint32_t resetKey()const{return uint32_t(profile)|(uint32_t(bypass)<<3)|(uint32_t(muted)<<4)|(uint32_t(wet==0)<<5)|(uint32_t(monitorGain==0)<<6);}
    uint64_t pack()const{return uint64_t(profile)|(uint64_t(wet)<<3)|(uint64_t(monitorGain)<<13)|(uint64_t(bypass)<<23)|(uint64_t(muted)<<24)|(uint64_t(revision)<<25)|(uint64_t(epoch)<<45);}
    static Command unpack(uint64_t value){Command c;c.profile=value&7;c.wet=(value>>3)&1023;c.monitorGain=(value>>13)&1023;c.bypass=(value>>23)&1;c.muted=(value>>24)&1;c.revision=(value>>25)&0xfffff;c.epoch=value>>45;return c;}
};
struct Frame {float l=0,r=0;uint64_t readAtNs=0;uint32_t epoch=0;};

// Strict SPSC: producer owns write index, consumer owns read index. Never overwrite
// an unread slot. Only consumer drops old frames; full producer blocks are dropped.
class FrameQueue {
public:
    static constexpr uint64_t Capacity=1024;
    bool push(const int16_t* pcm,int frames,uint64_t at,uint32_t epoch){
        if(!pcm||frames<1||frames>128)return false;
        const uint64_t w=written_.load(std::memory_order_relaxed),r=read_.load(std::memory_order_acquire);
        if(w-r+uint64_t(frames)>Capacity)return false;
        for(int i=0;i<frames;i++)buffer_[(w+i)%Capacity]={pcm[2*i]/32768.0f,pcm[2*i+1]/32768.0f,at,epoch};
        written_.store(w+frames,std::memory_order_release);return true;
    }
    bool pop(Frame& out){
        const uint64_t r=read_.load(std::memory_order_relaxed),w=written_.load(std::memory_order_acquire);
        if(r==w)return false;out=buffer_[r%Capacity];read_.store(r+1,std::memory_order_release);return true;
    }
    uint64_t discardTo(uint64_t keep){
        const uint64_t r=read_.load(std::memory_order_relaxed),w=written_.load(std::memory_order_acquire);
        const uint64_t drop=w-r>keep?w-r-keep:0;if(drop)read_.store(r+drop,std::memory_order_release);return drop;
    }
    uint64_t depth()const{const auto r=read_.load(std::memory_order_acquire),w=written_.load(std::memory_order_acquire);return w>=r?std::min(Capacity,w-r):0;}
    void clearAfterStop(){for(auto& f:buffer_)f={};written_.store(0);read_.store(0);}
private:
    std::array<Frame,Capacity> buffer_{};
    alignas(64) std::atomic<uint64_t> written_{0};alignas(64) std::atomic<uint64_t> read_{0};
};

class WetKernel {
public:
    struct Totals {
        std::atomic<uint64_t> pushed{0},producerDropped{0},consumerDropped{0},underrunFrames{0},staleFrames{0};
        std::atomic<uint64_t> rendered{0},nonzero{0},revisionNonzero{0},clears{0},clipped{0},maximumQueue{0},lastFrameAgeNs{0};
        std::atomic<int> peak{0},currentEpoch{0},appliedRevision{0},currentProfile{0},transitionFrames{0},gainRampFrames{0};
    } totals;
    WetKernel(){command_.store(Command{}.pack());}
    bool configure(int profile,int wet,int monitorGain,bool muted,bool bypass){
        if(!validProfile(profile)||wet<0||wet>1000||monitorGain<0||monitorGain>1000)return false;
        uint64_t old=command_.load(std::memory_order_relaxed);
        for(;;){const auto before=Command::unpack(old);Command next;
            next.profile=profile;next.wet=wet;next.monitorGain=monitorGain;next.muted=muted;next.bypass=bypass;
            if(next.resetKey()==before.resetKey()&&wet==before.wet&&monitorGain==before.monitorGain)return true;
            next.epoch=(before.epoch+(before.resetKey()!=next.resetKey()))&0x7ffff;
            next.revision=(before.revision+1)&0xfffff;
            if(command_.compare_exchange_weak(old,next.pack(),std::memory_order_release,std::memory_order_relaxed))return true;
        }
    }
    void bypass(){auto c=command();configure(c.profile,c.wet,c.monitorGain,c.muted,true);}
    Command command()const{return Command::unpack(command_.load(std::memory_order_acquire));}
    void push(const int16_t* pcm,int frames,uint64_t readAtNs){
        const auto c=command();if(!c.active())return;
        if(queue_.push(pcm,frames,readAtNs,c.epoch)){totals.pushed.fetch_add(frames,std::memory_order_relaxed);maximum(totals.maximumQueue,queue_.depth());}
        else totals.producerDropped.fetch_add(frames,std::memory_order_relaxed);
    }
    // Called only by the output queue callback. No allocation, locks, input reads,
    // Android/JNI calls, wait, log, file or socket operation.
    void render(int16_t* output,int frames,uint64_t nowNs,bool healthReady){
        if(!output||frames<0)return;
        const auto c=command();const bool enabled=c.active()&&healthReady;
        if(renderRevision_!=c.revision){renderRevision_=c.revision;totals.revisionNonzero.store(0);}
        if(c.epoch!=epoch_){
            epoch_=c.epoch;
            if(enabled&&!cleared_&&int(dsp_.profile())>0&&c.profile!=int(dsp_.profile())){
                // Preserve the old tail while only wet fades out for exactly 240 frames.
                pendingProfile_=c.profile;
                if(!transitionFrames_){transitionFrames_=240;transitionStep_=envelope_/240.0f;}
            }else{
                dsp_.select(static_cast<Profile>(c.profile));envelope_=0;cleared_=true;
                transitionFrames_=gainRampFrames_=0;gainTarget_=-1;totals.clears.fetch_add(1);
                totals.currentEpoch.store(int(c.epoch));
            }
        }
        totals.currentProfile.store(int(dsp_.profile()));
        if(!enabled){
            if(!cleared_){dsp_.clear();envelope_=0;cleared_=true;totals.clears.fetch_add(1);}
            if(int(dsp_.profile())!=c.profile)dsp_.select(static_cast<Profile>(c.profile));
            totals.currentProfile.store(c.profile);
            transitionFrames_=gainRampFrames_=0;gainTarget_=-1;totals.transitionFrames.store(0);totals.gainRampFrames.store(0);totals.currentEpoch.store(int(c.epoch));
            totals.appliedRevision.store(int(c.revision));
            totals.consumerDropped.fetch_add(queue_.discardTo(0));std::memset(output,0,size_t(frames)*2*sizeof(int16_t));totals.rendered.fetch_add(frames);return;
        }
        cleared_=false;
        const auto dropped=queue_.discardTo(384);totals.consumerDropped.fetch_add(dropped);
        if(dropped){dsp_.clear();envelope_=0;gainTarget_=-1;gainRampFrames_=0;totals.clears.fetch_add(1);}
        const float wanted=float(c.wet*c.monitorGain)/1000000.0f;
        const float ceiling=wetCeiling(c.monitorGain,c.wet);
        uint64_t missing=0,stale=0,nonzero=0,currentNonzero=0,clipped=0;int peak=0;uint64_t age=0;
        for(int i=0;i<frames;i++){
            Frame input;bool available=queue_.pop(input);
            if(available&&(input.epoch!=c.epoch||(nowNs>=input.readAtNs&&nowNs-input.readAtNs>80000000ULL))){available=false;stale++;}
            if(!available){input.l=input.r=0;missing++;}else age=nowNs>=input.readAtNs?nowNs-input.readAtNs:0;
            float l=0,r=0;const bool selectedProfileFrame=transitionFrames_==0;
            if(transitionFrames_){
                dsp_.process(0,0,l,r);envelope_=std::max(0.0f,envelope_-transitionStep_);
                if(--transitionFrames_==0){
                    dsp_.select(static_cast<Profile>(pendingProfile_));envelope_=0;gainTarget_=-1;gainRampFrames_=0;
                    totals.clears.fetch_add(1);totals.currentEpoch.store(int(c.epoch));totals.currentProfile.store(pendingProfile_);
                }
            }else{
                dsp_.process(input.l,input.r,l,r);
                // Fixed-duration 5 ms ramp, independent of target amplitude.
                if(wanted!=gainTarget_){gainTarget_=wanted;gainRampFrames_=240;gainStep_=(wanted-envelope_)/240.0f;}
                if(gainRampFrames_){envelope_+=gainStep_;if(--gainRampFrames_==0)envelope_=gainTarget_;}
            }
            float pair[2]{l*envelope_,r*envelope_};
            for(int ch=0;ch<2;ch++){
                if(!std::isfinite(pair[ch]))pair[ch]=0;
                if(std::abs(pair[ch])>ceiling)clipped++;
                const int sample=int(std::lround(softVoiceLimit(pair[ch],ceiling)*32767));
                output[i*2+ch]=int16_t(sample);peak=std::max(peak,std::abs(sample));nonzero+=sample!=0;
                currentNonzero+=selectedProfileFrame&&sample!=0;
            }
        }
        totals.rendered.fetch_add(frames);totals.underrunFrames.fetch_add(missing);totals.staleFrames.fetch_add(stale);
        totals.nonzero.fetch_add(nonzero);totals.clipped.fetch_add(clipped);totals.lastFrameAgeNs.store(age);
        totals.revisionNonzero.fetch_add(currentNonzero);
        totals.transitionFrames.store(transitionFrames_);totals.gainRampFrames.store(gainRampFrames_);
        if(!transitionFrames_&&!gainRampFrames_)totals.appliedRevision.store(int(c.revision));
        int previous=totals.peak.load();while(previous<peak&&!totals.peak.compare_exchange_weak(previous,peak)){}
    }
    uint64_t queueDepth()const{return queue_.depth();}
    void clearAfterStop(){queue_.clearAfterStop();dsp_.clear();envelope_=0;cleared_=true;transitionFrames_=gainRampFrames_=0;gainTarget_=-1;totals.clears.fetch_add(1);}
private:
    static_assert(ProfileCount<=7,"profile IDs must fit the reserved three command bits");
    static_assert(std::atomic<uint64_t>::is_always_lock_free,"64-bit controls must be lock-free");
    std::atomic<uint64_t> command_{};FrameQueue queue_;WetDsp dsp_;
    uint32_t epoch_=0,renderRevision_=0;float envelope_=0,gainTarget_=-1,gainStep_=0,transitionStep_=0;
    int gainRampFrames_=0,transitionFrames_=0,pendingProfile_=0;bool cleared_=true;
    static void maximum(std::atomic<uint64_t>& target,uint64_t value){auto old=target.load();while(old<value&&!target.compare_exchange_weak(old,value)){};}
};
}
