#include <jni.h>
#include <time.h>
#include <new>
#include <thread>
#include <array>
#include "WetKernel.h"
#include "FeedbackDetector.h"
#include "WetHealth.h"
#include "../../../strict-cache/StrictCacheClient.h"
#include <chrono>
#include "WetOpenSL.h"

namespace {
uint64_t nowNs(){timespec t{};clock_gettime(CLOCK_MONOTONIC,&t);return uint64_t(t.tv_sec)*1000000000ULL+t.tv_nsec;}
enum Metric {INPUT_OPEN,INPUT_START,INPUT_STOP,INPUT_CLOSE,INPUT_ID,RATE,CHANNELS,FORMAT,SHARING,HW_RATE,HW_CHANNELS,HW_FORMAT,BURST,CAPACITY,BUFFER,INPUT_FRAMES,INPUT_NONZERO,INPUT_PEAK,INPUT_ZEROS,INPUT_READ_ERROR,INPUT_LAST_NS,INPUT_READ_MAX_NS,INPUT_WRITTEN,INPUT_READ,INPUT_XRUNS,INPUT_TS_RESULT,INPUT_TS_POSITION,INPUT_TS_NS,INPUT_TS_SAMPLE_NS,OUTPUT_OPEN,OUTPUT_START,OUTPUT_STAGE,OUTPUT_PERFORMANCE,OUTPUT_EXPECTED,OUTPUT_ACTUAL,OUTPUT_ROUTE_NS,OUTPUT_CALLBACKS,OUTPUT_LAST_NS,OUTPUT_MAX_NS,OUTPUT_FRAMES,OUTPUT_NONZERO,OUTPUT_PEAK,OUTPUT_CLIPPED,QUEUE_DEPTH,QUEUE_MAX,QUEUE_PRODUCER_DROPPED,QUEUE_CONSUMER_DROPPED,QUEUE_MISSING,QUEUE_STALE,QUEUE_LAST_AGE_NS,CLEARS,PROFILE,WET,GAIN,MUTED,BYPASS,EPOCH,APPLIED_EPOCH,FAULT,RUNNING,INPUT_CLOSED,OUTPUT_CLOSED,CREATED_NS,STARTED_NS,ENDED_NS,TRANSITION_FRAMES,GAIN_RAMP_FRAMES,REVISION_WET_SAMPLES,QUEUE_GENERATION,FEEDBACK_FRAMES,FEEDBACK_SEQUENCE,FEEDBACK_LEVEL,FEEDBACK_HZ,FEEDBACK_PEAK,FEEDBACK_CLIPPED,FEEDBACK_READY,FEEDBACK_LATCHED,INPUT_DISCARDED,INPUT_BACKLOG,CACHE_PID,CACHE_UID,CACHE_GENERATION,CACHE_CAPS,CACHE_SEQUENCE,INLINE_MODE,INLINE_FLAGS,INLINE_CALLBACKS,INLINE_FRAMES,INLINE_PEAK,COUNT};
using namespace fridaywet;
struct Wet {
    FridayCacheClient cache{-1};WetOpenSL output;fridaywet::WetKernel kernel;fridaywet::feedback::Detector detector;std::thread reader;
    std::array<int64_t,COUNT> m{};
    std::atomic<bool> run{false},routeReady{false};std::atomic<int> fault{0};
    std::atomic<uint64_t> inputFrames{0},inputNonzero{0},inputZeros{0},lastInput{0},readMaximum{0};
    std::atomic<int> inputPeak{0},inputError{0};
    std::atomic<uint64_t> feedbackFrames{0},feedbackWord{0},feedbackClipped{0},inputDiscarded{0},inputBacklog{0};
    std::atomic<int> feedbackPeak{0};std::atomic<uint64_t> callbacks{0},lastCallback{0},callbackMaximum{0},routeAt{0};
    // Only reader owns the transport. Control/JNI threads publish desired
    // commands locally; they never interleave RPCs on the PCM socket.
    std::array<std::atomic<int64_t>,12> inlineState{};
    bool inlineMode=false;uint32_t sentRevision=UINT32_MAX;uint64_t statusAt=0,lastInlineCallbacks=0;
    int expectedInput,expectedOutput;bool valid=false,finished=false;uint64_t started=0;
    Wet(int in,int out,uint64_t lease):expectedInput(in),expectedOutput(out){
        m[CREATED_NS]=nowNs();m[INPUT_START]=m[INPUT_STOP]=m[INPUT_CLOSE]=-1;m[OUTPUT_START]=-1;
        m[OUTPUT_EXPECTED]=out;m[INPUT_TS_RESULT]=-1;
        if(in<=0||out<=0||!lease){fault.store(ContractMismatch);return;}
        // This path has no AudioRecord/AAudio/USB device input constructor. The
        // platform endpoint can only read the already-owned native dry cache.
        m[INPUT_OPEN]=friday_cache_connect(&cache,FRIDAY_CACHE_READER,80);
        if(m[INPUT_OPEN]){fault.store(ContractMismatch);return;}
        m[CACHE_PID]=cache.server_pid;m[CACHE_UID]=cache.server_uid;m[CACHE_CAPS]=cache.capabilities;
        m[INPUT_ID]=in; // Referenced dry device identity, not an AAudio route ACK.
        m[RATE]=cache.rate;m[CHANNELS]=cache.channels;m[FORMAT]=cache.format;
        m[SHARING]=-1;m[HW_RATE]=m[HW_CHANNELS]=m[HW_FORMAT]=-1;
        m[INPUT_OPEN]=friday_cache_attach(&cache,lease,80);
        if(m[INPUT_OPEN]){fault.store(ContractMismatch);return;}
        m[CACHE_GENERATION]=cache.generation;
        inlineMode=cache.capabilities==FRIDAY_CACHE_INLINE_CAPABILITIES;
        valid=friday_cache_capabilities_valid(cache.capabilities)&&cache.rate==48000&&cache.channels==2&&cache.format==1;
        if(!valid){fault.store(ContractMismatch);return;}
        if(inlineMode){m[INLINE_MODE]=1;m[OUTPUT_CLOSED]=1;return;}
        m[OUTPUT_OPEN]=output.open(this,render)?0:-1;valid=valid&&m[OUTPUT_OPEN]==0;
    }
    static void max(std::atomic<uint64_t>& target,uint64_t v){auto old=target.load();while(old<v&&!target.compare_exchange_weak(old,v)){};}
    static void render(void* context,int16_t* samples,int frames){
        auto* w=static_cast<Wet*>(context);const auto begin=nowNs();w->lastCallback.store(begin);w->callbacks.fetch_add(1);
        const auto read=w->lastInput.load(),route=w->routeAt.load();
        const bool ready=w->run.load()&&!w->fault.load()&&w->routeReady.load()&&read>0&&begin>=read&&begin-read<=100000000ULL
            &&route>0&&begin>=route&&begin-route<=750000000ULL;
        w->kernel.render(samples,frames,begin,ready);max(w->callbackMaximum,nowNs()-begin);
    }
    void fail(int code){int expected=0;fault.compare_exchange_strong(expected,code);run.store(false);kernel.bypass();}
    int start(){
        if(!valid||finished||run.load()||started)return -1;
        started=nowNs();m[STARTED_NS]=started;run.store(true);
        m[INPUT_START]=0;
        if(!inlineMode){m[OUTPUT_START]=output.start();if(m[OUTPUT_START]!=0){fail(OutputFailure);return -1;}}
        reader=std::thread([this]{readLoop();});return 0;
    }
    void readLoop(){
        int16_t pcm[256]{};
        while(run.load(std::memory_order_acquire)){
            const auto begin=nowNs();
            if(inlineMode&&!synchronizeInline(begin))break;
            uint64_t sequence=0;
            const int result=friday_cache_read128(&cache,pcm,&sequence,10);
            const int count=result==0?128:result==-EAGAIN?0:result;
            const auto at=nowNs();max(readMaximum,at-begin);
            if(count==0)std::this_thread::sleep_for(std::chrono::milliseconds(1));
            if(count>0&&count<=128){
                inputFrames.fetch_add(count);lastInput.store(at);int peak=0;uint64_t nonzero=0;
                for(int i=0;i<count*2;i++){peak=std::max(peak,std::abs(int(pcm[i])));nonzero+=pcm[i]!=0;}
                inputNonzero.fetch_add(nonzero);int old=inputPeak.load();while(old<peak&&!inputPeak.compare_exchange_weak(old,peak)){}
                detector.push(pcm,count);const auto protection=detector.snapshot();
                feedbackFrames.store(protection.processedFrames);feedbackClipped.store(protection.clippingSamples);
                feedbackPeak.store(int(std::min(32768.0,protection.peak*32768)));
                // Sequence, level, frequency and readiness are one atomic packet.
                const uint64_t packed=(protection.sequence<<20)|(uint64_t(protection.severity)<<17)
                    |(uint64_t(protection.ready)<<16)|(uint64_t(protection.latched)<<15)
                    |uint64_t(std::clamp(int(std::lround(protection.frequencyHz)),0,12000));
                feedbackWord.store(packed,std::memory_order_release);
                if(protection.latched){if(inlineMode){const auto c=kernel.command();kernel.configure(c.profile,c.wet,c.monitorGain,true,true);}else kernel.bypass();}
                else if(!inlineMode)kernel.push(pcm,count,at);
            }else if(count==0)inputZeros.fetch_add(1);else{inputError.store(count);fail(InputFailure);}
            volatile int16_t* clear=pcm;for(int i=0;i<256;i++)clear[i]=0;
            const auto last=lastInput.load(),cb=lastCallback.load(),route=routeAt.load();
            if(inlineMode){
                if(at-started>1500000000ULL&&(!last||!cb||at-last>150000000ULL||at-cb>250000000ULL))fail(InputFailure);
            }else{const int health=healthFault(at,started,last,cb,route,output.error.load()!=0);if(health)fail(health);}
        }
        // Stop enqueuing immediately; Java control owns all OpenSL stop/close calls.
        output.active.store(false);
        if(inlineMode)friday_cache_close(&cache); // Reader loss closes the in-stream safety gate.
    }
    bool synchronizeInline(uint64_t at){
        const auto c=kernel.command();
        if(c.revision!=sentRevision){
            const int result=friday_cache_inline_configure(&cache,c.profile,c.wet,c.monitorGain,c.muted,c.bypass,10);
            if(result==-EAGAIN)return true;
            if(result){inputError.store(result);fail(InputFailure);return false;}
            sentRevision=c.revision;
        }
        if(statusAt&&at-statusAt<50000000ULL)return true;
        FridayInlineMetrics s{};const int result=friday_cache_inline_status(&cache,&s,10);
        if(result==-EAGAIN)return true;
        if(result||s.fault||(s.flags&77)!=77){inputError.store(result?result:-ESTALE);fail(InputFailure);return false;}
        if(s.callbacks<lastInlineCallbacks){fail(InputFailure);return false;}
        if(s.callbacks>lastInlineCallbacks){lastInlineCallbacks=s.callbacks;lastCallback.store(at);}
        inlineState[0].store(s.requested_revision);inlineState[1].store(s.applied_revision);
        inlineState[2].store(s.profile);inlineState[3].store(s.wet);inlineState[4].store(s.monitor_gain);
        inlineState[5].store(s.flags);inlineState[6].store(s.callbacks);inlineState[7].store(s.frames);inlineState[8].store(s.peak);
        statusAt=at;return true;
    }
    void snapshot(JNIEnv* env){
        m[INPUT_READ]=inputFrames.load();m[INPUT_WRITTEN]=m[INPUT_READ];m[INPUT_XRUNS]=-1;
        m[CACHE_SEQUENCE]=m[INPUT_READ]/128; // Delivered packet count, not ADC timestamp.
        if(output.playerObject&&started&&!finished){
            const int actual=output.routedDevice(env);m[OUTPUT_ACTUAL]=actual;
            if(actual==expectedOutput){routeAt.store(nowNs());routeReady.store(true);}
            else if(actual>0){routeReady.store(false);fail(RouteMismatch);}
        }
        m[INPUT_FRAMES]=inputFrames.load();m[INPUT_NONZERO]=inputNonzero.load();m[INPUT_PEAK]=inputPeak.load();m[INPUT_ZEROS]=inputZeros.load();m[INPUT_READ_ERROR]=inputError.load();m[INPUT_LAST_NS]=lastInput.load();m[INPUT_READ_MAX_NS]=readMaximum.load();
        m[OUTPUT_STAGE]=output.stage;m[OUTPUT_PERFORMANCE]=output.performance;m[OUTPUT_ROUTE_NS]=routeAt.load();m[OUTPUT_CALLBACKS]=callbacks.load();m[OUTPUT_LAST_NS]=lastCallback.load();m[OUTPUT_MAX_NS]=callbackMaximum.load();
        const auto& t=kernel.totals;const auto c=kernel.command();m[OUTPUT_FRAMES]=t.rendered.load();m[OUTPUT_NONZERO]=t.nonzero.load();m[OUTPUT_PEAK]=t.peak.load();m[OUTPUT_CLIPPED]=t.clipped.load();
        m[QUEUE_DEPTH]=kernel.queueDepth();m[QUEUE_MAX]=t.maximumQueue.load();m[QUEUE_PRODUCER_DROPPED]=t.producerDropped.load();m[QUEUE_CONSUMER_DROPPED]=t.consumerDropped.load();m[QUEUE_MISSING]=t.underrunFrames.load();m[QUEUE_STALE]=t.staleFrames.load();m[QUEUE_LAST_AGE_NS]=t.lastFrameAgeNs.load();
        m[CLEARS]=t.clears.load();m[PROFILE]=c.profile;m[WET]=c.wet;m[GAIN]=c.monitorGain;m[MUTED]=c.muted;m[BYPASS]=c.bypass;m[EPOCH]=c.revision;m[APPLIED_EPOCH]=t.appliedRevision.load();m[TRANSITION_FRAMES]=t.transitionFrames.load();m[GAIN_RAMP_FRAMES]=t.gainRampFrames.load();m[REVISION_WET_SAMPLES]=t.revisionNonzero.load();m[QUEUE_GENERATION]=c.epoch;m[FAULT]=fault.load();m[RUNNING]=run.load();
        const auto protection=feedbackWord.load(std::memory_order_acquire);
        m[FEEDBACK_FRAMES]=feedbackFrames.load();m[FEEDBACK_SEQUENCE]=protection>>20;m[FEEDBACK_LEVEL]=(protection>>17)&7;
        m[FEEDBACK_HZ]=protection&0x7fff;m[FEEDBACK_READY]=(protection>>16)&1;m[FEEDBACK_LATCHED]=(protection>>15)&1;
        m[FEEDBACK_PEAK]=feedbackPeak.load();m[FEEDBACK_CLIPPED]=feedbackClipped.load();m[INPUT_DISCARDED]=inputDiscarded.load();m[INPUT_BACKLOG]=inputBacklog.load();
        if(inlineMode){
            const auto flags=inlineState[5].load();m[EPOCH]=inlineState[0].load();m[APPLIED_EPOCH]=inlineState[1].load();
            m[PROFILE]=inlineState[2].load();m[WET]=inlineState[3].load();m[GAIN]=inlineState[4].load();
            m[MUTED]=(flags&16)!=0;m[BYPASS]=(flags&32)!=0;m[TRANSITION_FRAMES]=m[GAIN_RAMP_FRAMES]=m[EPOCH]==m[APPLIED_EPOCH]?0:1;
            m[OUTPUT_CALLBACKS]=inlineState[6].load();m[INLINE_FLAGS]=flags;m[INLINE_CALLBACKS]=m[OUTPUT_CALLBACKS];
            m[INLINE_FRAMES]=inlineState[7].load();m[INLINE_PEAK]=inlineState[8].load();
            // There is no app output stream or measured AudioDevice route here.
            m[OUTPUT_ACTUAL]=m[OUTPUT_ROUTE_NS]=m[OUTPUT_FRAMES]=m[OUTPUT_NONZERO]=m[REVISION_WET_SAMPLES]=0;
        }
    }
    void finish(){
        if(finished)return;kernel.bypass();run.store(false);routeReady.store(false);output.stop();
        m[INPUT_STOP]=0;
        if(reader.joinable())reader.join();output.close();m[OUTPUT_CLOSED]=1;
        friday_cache_close(&cache);m[INPUT_CLOSE]=0;m[INPUT_CLOSED]=1;
        kernel.clearAfterStop();m[ENDED_NS]=nowNs();finished=true;
    }
    ~Wet(){finish();}
};
Wet* get(jlong p){return reinterpret_cast<Wet*>(p);}
}
extern "C" JNIEXPORT jlong JNICALL Java_local_friday_audio_NativeWet_openNative(JNIEnv*,jclass,jint input,jint output,jlong lease){return reinterpret_cast<jlong>(new(std::nothrow)Wet(input,output,uint64_t(lease)));}
extern "C" JNIEXPORT jboolean JNICALL Java_local_friday_audio_NativeWet_validNative(JNIEnv*,jclass,jlong p){return p&&get(p)->valid&&!get(p)->finished;}
extern "C" JNIEXPORT jint JNICALL Java_local_friday_audio_NativeWet_startNative(JNIEnv*,jclass,jlong p){return p?get(p)->start():-1;}
extern "C" JNIEXPORT jboolean JNICALL Java_local_friday_audio_NativeWet_configureNative(JNIEnv*,jclass,jlong p,jint profile,jint wet,jint gain,jboolean muted,jboolean bypass){return p&&!get(p)->finished&&get(p)->kernel.configure(profile,wet,gain,muted||((get(p)->feedbackWord.load()>>15)&1),bypass||((get(p)->feedbackWord.load()>>15)&1));}
extern "C" JNIEXPORT void JNICALL Java_local_friday_audio_NativeWet_bypassNative(JNIEnv*,jclass,jlong p){if(p)get(p)->kernel.bypass();}
extern "C" JNIEXPORT void JNICALL Java_local_friday_audio_NativeWet_finishNative(JNIEnv*,jclass,jlong p){if(p)get(p)->finish();}
extern "C" JNIEXPORT void JNICALL Java_local_friday_audio_NativeWet_disposeNative(JNIEnv*,jclass,jlong p){delete get(p);}
extern "C" JNIEXPORT jlongArray JNICALL Java_local_friday_audio_NativeWet_metricsNative(JNIEnv* env,jclass,jlong p){
    auto out=env->NewLongArray(COUNT);if(!out)return nullptr;if(p){get(p)->snapshot(env);std::array<jlong,COUNT> values{};for(int i=0;i<COUNT;i++)values[i]=get(p)->m[i];env->SetLongArrayRegion(out,0,COUNT,values.data());}return out;
}

extern "C" JNIEXPORT jlongArray JNICALL Java_local_friday_audio_NativeWet_cacheContractNative(JNIEnv* env,jclass){
    FridayCacheClient c{-1};jlong values[8]{};
    values[0]=friday_cache_connect(&c,FRIDAY_CACHE_READER,80);
    if(values[0]==0){values[1]=c.server_pid;values[2]=c.server_uid;values[3]=c.capabilities;values[4]=c.rate;values[5]=c.channels;values[6]=c.format;values[7]=jlong(c.instance_cookie);}
    friday_cache_close(&c);auto out=env->NewLongArray(8);if(out)env->SetLongArrayRegion(out,0,8,values);return out;
}
