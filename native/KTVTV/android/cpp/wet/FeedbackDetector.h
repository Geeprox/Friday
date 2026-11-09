#pragma once

#include <array>
#include <algorithm>
#include <cmath>
#include <cstdint>

namespace fridaywet { namespace feedback {

// A tonal-risk observer, not proof of an acoustic feedback loop. No PCM output.
// Reset is an explicit operator/session action; severity never recovers itself.
// frequencyHz names the last requested notch/latch tone (0 for overload).
// peak is the most recent window's normalized absolute stereo peak; clipping
// counts samples across both channels. ready means a full analysis window exists.
// overload identifies sustained clipping/near-full-scale saturation safety,
// not a tonal-feedback inference (a heavily compressed source can also trip it).
struct Snapshot {
    uint64_t processedFrames=0, sequence=0, clippingSamples=0;
    int severity=0;
    double frequencyHz=0, peak=0;
    bool ready=false, latched=false, overload=false;
};

class Detector {
public:
    static constexpr int SampleRate=48000, Window=2048, Hop=512;
    Detector() {
        for(int i=0;i<Window;i++)hann_[i]=.5-.5*std::cos(2*Pi*i/(Window-1));
        reset();
    }
    void reset() {
        const auto sequence=state_.sequence;
        ring_.fill(0); state_=Snapshot{};state_.sequence=sequence;write_=filled_=since_=0;
        stable_=growth_=overloadFrames_=0;candidate_=lowFrequency_=highFrequency_=0;
        initialRms_=lastRms_=maximumRms_=0;actionFrame_=lastNotchFrame_=0;
        notchFrequency_=0;notchCount_=0;
    }
    // Caller supplies interleaved stereo PCM16 at 48 kHz. No allocations, locks,
    // callbacks, platform calls or output writes occur on this path.
    void push(const int16_t* stereo,int frames) {
        if(!stereo||frames<=0)return;
        for(int i=0;i<frames;i++) {
            const int left=stereo[2*i],right=stereo[2*i+1];
            ring_[2*write_]=static_cast<int16_t>(left);ring_[2*write_+1]=static_cast<int16_t>(right);
            write_=(write_+1)&(Window-1);filled_=std::min(Window,filled_+1);
            state_.processedFrames++;
            state_.clippingSamples+=(std::abs(left)>=32760)+(std::abs(right)>=32760);
            if(++since_>=Hop){since_=0;if(filled_==Window)analyze();}
        }
    }
    // Single-thread owner API: copy this value in the same reader that calls
    // push/reset, then publish the copy through the host's atomic metrics bridge.
    Snapshot snapshot() const {return state_;}
private:
    static constexpr double Pi=3.1415926535897932384626433832795;
    static constexpr int GrowthFrames=5760;  // 120 ms of stable observations
    static constexpr int SteadyFrames=8640;  // 180 ms, after FFT warmup
    static constexpr int CutFrames=8640;     // 180 ms after notch
    static constexpr int LatchFrames=12000;  // 250 ms after gain cut
    static constexpr int OverloadFrames=4800; // 100 ms persistent overload windows
    std::array<int16_t,Window*2> ring_{};
    std::array<double,Window> hann_{},real_{},imag_{};
    Snapshot state_{};
    int write_=0,filled_=0,since_=0,stable_=0,growth_=0,overloadFrames_=0;
    double candidate_=0,lowFrequency_=0,highFrequency_=0,initialRms_=0,lastRms_=0,maximumRms_=0;
    uint64_t actionFrame_=0,lastNotchFrame_=0;
    double notchFrequency_=0;
    unsigned notchCount_=0;
    static double power(double re,double im){return re*re+im*im;}
    void fft() {
        for(int i=1,j=0;i<Window;i++) {
            int bit=Window>>1;for(;j&bit;bit>>=1)j^=bit;j^=bit;
            if(i<j){std::swap(real_[i],real_[j]);std::swap(imag_[i],imag_[j]);}
        }
        for(int size=2;size<=Window;size*=2) {
            const double angle=-2*Pi/size,wr=std::cos(angle),wi=std::sin(angle);
            for(int start=0;start<Window;start+=size) {
                double re=1,im=0;
                for(int j=0;j<size/2;j++) {
                    const int a=start+j,b=a+size/2;
                    const double tr=re*real_[b]-im*imag_[b],ti=re*imag_[b]+im*real_[b];
                    real_[b]=real_[a]-tr;imag_[b]=imag_[a]-ti;real_[a]+=tr;imag_[a]+=ti;
                    const double next=re*wr-im*wi;im=re*wi+im*wr;re=next;
                }
            }
        }
    }
    void clearCandidate() {
        stable_=growth_=0;candidate_=lowFrequency_=highFrequency_=0;
        initialRms_=lastRms_=maximumRms_=0;
    }
    void startCandidate(double frequency,double rms) {
        candidate_=lowFrequency_=highFrequency_=frequency;initialRms_=lastRms_=maximumRms_=rms;
        stable_=Hop;growth_=0;
    }
    void action(int severity,double frequency,bool overload=false) {
        state_.severity=severity;state_.frequencyHz=frequency;state_.sequence++;
        state_.overload=overload;state_.latched=severity==3;actionFrame_=state_.processedFrames;
        if(severity==1){notchFrequency_=frequency;lastNotchFrame_=state_.processedFrames;notchCount_++;}
    }
    double band(int bin,int radius=2) const {
        double sum=0;for(int i=std::max(1,bin-radius);i<=std::min(Window/2-1,bin+radius);i++)sum+=real_[i];return sum;
    }
    struct Tone {double frequency=0,rms=0;bool valid=false;};
    double frequencyAt(int bin) const {
        const double l=std::log(std::max(real_[bin-1],1e-30)),c=std::log(std::max(real_[bin],1e-30)),r=std::log(std::max(real_[bin+1],1e-30));
        const double denominator=l-2*c+r;
        const double fraction=std::abs(denominator)>1e-12?std::max(-.5,std::min(.5,.5*(l-r)/denominator)):0;
        return (bin+fraction)*SampleRate/Window;
    }
    Tone spectrum(int channel,double energy) {
        Tone tone;tone.rms=std::sqrt(energy/Window);
        if(tone.rms<.035)return tone;
        for(int i=0;i<Window;i++){real_[i]=ring_[((write_+i)&(Window-1))*2+channel]/32768.0*hann_[i];imag_[i]=0;}
        fft();
        double total=0,largest=0;int best=0;
        for(int i=1;i<Window/2;i++) {
            real_[i]=power(real_[i],imag_[i]);total+=real_[i];
            if(i>=8&&i<=277&&real_[i]>largest){largest=real_[i];best=i;}
        }
        if(best==0||total<=1e-15)return tone;
        double narrow=band(best);
        int second=0;double next=0;
        for(int i=8;i<=277;i++)if(std::abs(i-best)>4&&real_[i]>next){second=i;next=real_[i];}
        const double other=second?band(second):0;
        const double a=frequencyAt(best),b=second?frequencyAt(second):0,low=std::min(a,b),high=std::max(a,b);
        // A weak/missing fundamental can leave (say) the second and third
        // vocal harmonics dominant. Veto small-integer harmonic pairs too;
        // neither peak need be an integer multiple of the other. Genuine
        // feedback modes at those same ratios are a deliberate blind spot.
        // Integer multiples retain the existing 10% harmonic threshold below:
        // a saturating feedback tone can legitimately have weak 3rd/5th partials.
        static constexpr int ratios[][2]={{3,2},{5,2},{4,3},{5,3},{5,4},{6,5}};
        if(other/total>.04)for(const auto& ratio:ratios)
            if(std::abs(high-low*ratio[0]/ratio[1])<=double(SampleRate)/Window)return tone;
        if(narrow/total<.72){
            // Two unrelated room modes can divide the energy so neither clears
            // the single-peak test. Only admit two isolated, substantial peaks
            // with <=10% residual spectrum; a broad/formant-rich source fails.
            if(narrow/total<.20||other/total<.20||(narrow+other)/total<.90)return tone;
            // Keep following an existing mode when equal peaks trade rank.
            // Both candidates still face the harmonic/subharmonic veto below.
            if(candidate_>0&&std::abs(b-candidate_)<=10&&std::abs(a-candidate_)>10&&
               (tone.rms*std::sqrt(other/total)>=.10||tone.rms*std::sqrt(narrow/total)<.10)){best=second;narrow=other;}
            tone.rms*=std::sqrt(narrow/total);
        }
        double harmonic=0;for(int n=2;n<=6&&best*n<Window/2-2;n++)harmonic+=band(best*n);
        if(harmonic/total>.10)return tone;
        // A voice/instrument formant can make its second/third harmonic the
        // largest bin. Look below it too instead of mistaking that harmonic for
        // an isolated fundamental. This deliberately rejects harmonic stacks, at
        // the cost of missing risks mixed with strong harmonically related music.
        for(int divisor=2;divisor<=5;divisor++) {
            const int sub=static_cast<int>(std::round(best/double(divisor)));
            if(sub>=3&&band(sub,1)/total>.04)return tone;
        }
        tone.frequency=frequencyAt(best);tone.valid=true;return tone;
    }
    void analyze() {
        state_.ready=true;
        double leftEnergy=0,rightEnergy=0,peak=0;int clips=0,leftPlateau=0,rightPlateau=0;
        for(int i=0;i<Window;i++) {
            const int at=((write_+i)&(Window-1))*2;
            const double l=ring_[at]/32768.0,r=ring_[at+1]/32768.0;
            leftEnergy+=l*l;rightEnergy+=r*r;peak=std::max(peak,std::max(std::abs(l),std::abs(r)));
            clips+=(std::abs(int(ring_[at]))>=32760)+(std::abs(int(ring_[at+1]))>=32760);
            leftPlateau+=std::abs(l)>=.90;rightPlateau+=std::abs(r)>=.90;
        }
        state_.peak=peak;
        if(state_.latched)return;
        const double overallRms=std::sqrt(std::max(leftEnergy,rightEnergy)/Window);
        // A saturating ADC/limiter can add harmonics and remain below PCM's
        // clipping code. Its dense, near-full-scale plateau must not evade the
        // harmonic veto forever. This is deliberately an overload safety event,
        // not feedback identification. Full-scale sine/noise and brief transients
        // do not satisfy the high-RMS, majority-plateau, persistent conjunction.
        const bool clipped=clips>=Window*2*.02&&overallRms>=.2;
        const bool saturated=(leftEnergy/Window>=.85*.85&&leftPlateau>=Window/2)||
                             (rightEnergy/Window>=.85*.85&&rightPlateau>=Window/2);
        overloadFrames_=(clipped||saturated)?std::min(OverloadFrames,overloadFrames_+Hop):0;
        if(overloadFrames_>=OverloadFrames){action(3,0,true);return;}
        // Analyze each channel independently. A stronger noisy channel must not
        // hide a tonal risk in the other, and opposite polarity never cancels.
        const Tone left=spectrum(0,leftEnergy),right=spectrum(1,rightEnergy);
        if(!left.valid&&!right.valid){clearCandidate();return;}
        Tone tone=right.valid&&(!left.valid||right.rms>left.rms)?right:left;
        // Two independent channel modes can trade amplitude rank every few
        // windows. Follow an already stable, similarly strong mode instead of
        // resetting its dwell forever; a much weaker old mode cannot mask the
        // stronger channel's new risk.
        if(left.valid&&std::abs(left.frequency-candidate_)<=10&&left.rms>=tone.rms*.7&&(left.rms>=.10||tone.rms<.10))tone=left;
        else if(right.valid&&std::abs(right.frequency-candidate_)<=10&&right.rms>=tone.rms*.7&&(right.rms>=.10||tone.rms<.10))tone=right;
        const double rms=tone.rms,frequency=tone.frequency;
        if(candidate_==0||std::abs(frequency-candidate_)>10){startCandidate(frequency,rms);return;}
        lowFrequency_=std::min(lowFrequency_,frequency);highFrequency_=std::max(highFrequency_,frequency);
        if(highFrequency_-lowFrequency_>10){startCandidate(frequency,rms);return;}
        const bool rising=rms>lastRms_*1.003;
        stable_=std::min(SampleRate*2,stable_+Hop);growth_=std::min(1000,growth_+(rms>lastRms_*1.01));lastRms_=rms;maximumRms_=std::max(maximumRms_,rms);
        const bool growing=stable_>=GrowthFrames&&growth_>=3&&rising&&rms>=initialRms_*1.3&&rms>=maximumRms_*.95;
        const bool sustained=stable_>=SteadyFrames&&rms>=.10&&(state_.severity>0||rms>=maximumRms_*.85);
        if(!growing&&!sustained)return;
        if(state_.severity==0){action(1,frequency);return;}
        const bool sameTone=std::abs(frequency-notchFrequency_)<=35;
        if(state_.severity==1&&!sameTone) {
            // At most three different notch requests per explicit session. No
            // refresh heartbeat or drifting tone can create an unbounded stream.
            // Exhausting that budget must still protect a fourth stable tone:
            // use the global gain cap, then the persistent-risk mute latch.
            if(notchCount_>=3)action(2,frequency);
            else if(state_.processedFrames-lastNotchFrame_>=12000)action(1,frequency);
            return;
        }
        // A gain cut is global: a new stable strong frequency cannot evade its
        // final safety latch merely by moving outside the previous notch.
        if(state_.severity==2&&state_.processedFrames-actionFrame_>=LatchFrames&&rms>=.10){action(3,frequency);return;}
        if(!sameTone)return;
        if(state_.severity==1&&state_.processedFrames-actionFrame_>=CutFrames&&rms>=.10){action(2,frequency);return;}

    }
};

}} // namespace fridaywet::feedback
