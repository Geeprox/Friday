#include <aaudio/AAudio.h>
#include <jni.h>
#include <atomic>
#include <algorithm>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <new>
#include <time.h>
#include "PcmQueue.h"
#include "AudioRoutePolicy.h"
#include "OpenSLOutput.h"
#include "DirectVoiceMonitor.h"

namespace {
std::atomic<int> lastOpenCode{0};
int64_t now(){timespec value{};clock_gettime(CLOCK_MONOTONIC,&value);return value.tv_sec*1000000000LL+value.tv_nsec;}
struct Duplex{
    AAudioStream* input=nullptr;AAudioStream* output=nullptr;PcmQueue in,out;OpenSLOutput platform;std::atomic<int> routeId{0};
    std::atomic<bool> stopped{false};std::atomic<int> error{0};
    std::atomic<int64_t> inputCallbackFrames{0},inputDropped{0},outputMissing{0},callbackFrames{0},firstMusic{-1};
    std::atomic<int64_t> inputLastAt{0},inputGapMax{0};std::atomic<int> inputWaits[101]{};
    std::atomic<int64_t> outputLastAt{0},outputGapMax{0},outputLastFrames{0};
    int64_t configuration[27]{};
    int inputId=0,inputChannels=2,outputTarget=384;
    bool monitorOnly=false;std::atomic<bool> monitorArmed{false};
    std::atomic<int64_t> monitorDeadline{0},monitorVoiceFrames{0},monitorTrimmed{0},monitorPeak{0};
};
void onError(AAudioStream*,void* user,aaudio_result_t error){auto* d=static_cast<Duplex*>(user);d->error.store(error);d->stopped.store(true);}
aaudio_data_callback_result_t inputData(AAudioStream*,void* user,void* data,int32_t frames){
    auto* d=static_cast<Duplex*>(user);const auto time=now();
    if(d->stopped.load()||(d->monitorOnly&&time>=d->monitorDeadline.load()))return AAUDIO_CALLBACK_RESULT_STOP;
    const auto previous=d->inputLastAt.exchange(time);if(previous>0){const auto gap=(time-previous)/1000;d->inputWaits[std::min<int64_t>(100,gap/1000)].fetch_add(1);d->inputGapMax.store(std::max(d->inputGapMax.load(),gap));}
    d->inputCallbackFrames.fetch_add(frames);int n=d->in.push(static_cast<int16_t*>(data),frames);d->inputDropped.fetch_add(frames-n);return AAUDIO_CALLBACK_RESULT_CONTINUE;
}
aaudio_data_callback_result_t outputData(AAudioStream*,void* user,void* data,int32_t frames){
    auto* d=static_cast<Duplex*>(user);auto* out=static_cast<int16_t*>(data);const auto time=now();
    if(d->stopped.load()){std::memset(out,0,static_cast<size_t>(frames)*2*sizeof(int16_t));return AAUDIO_CALLBACK_RESULT_STOP;}
    const auto previous=d->outputLastAt.exchange(time);if(previous>0)d->outputGapMax.store(std::max(d->outputGapMax.load(),(time-previous)/1000));
    d->outputLastFrames.store(frames);auto absolute=d->callbackFrames.fetch_add(frames);
    if(d->monitorOnly){
        std::memset(out,0,static_cast<size_t>(frames)*2*sizeof(int16_t));
        if(frames>480){d->error.store(1001);d->stopped.store(true);return AAUDIO_CALLBACK_RESULT_STOP;}
        int16_t input[960]{};
        // One consumer, bounded freshness: never replay a growing backlog.
        const int target=static_cast<int>(d->configuration[7])+frames;
        while(d->in.size()>static_cast<uint64_t>(target)){
            int n=d->in.pop(input,std::min(480,static_cast<int>(d->in.size())-target));
            if(!n)break;d->monitorTrimmed.fetch_add(n);
        }
        int n=d->in.pop(input,frames);
        if(directVoiceMayPlay(d->monitorArmed.load(),d->stopped.load(),time,d->monitorDeadline.load())){
            int peak=0;for(int i=0;i<n;i++){auto value=directVoiceSample(input[i*d->inputChannels],input[i*d->inputChannels+d->inputChannels-1]);out[i*2]=out[i*2+1]=value;peak=std::max(peak,std::abs(static_cast<int>(value)));}
            d->monitorPeak.store(std::max(d->monitorPeak.load(),static_cast<int64_t>(peak)));d->monitorVoiceFrames.fetch_add(n);d->outputMissing.fetch_add(frames-n);
        }
        std::memset(input,0,sizeof(input));return AAUDIO_CALLBACK_RESULT_CONTINUE;
    }
    int n=d->out.pop(out,frames);if(n>0){int64_t expected=-1;d->firstMusic.compare_exchange_strong(expected,absolute);}if(n<frames){std::memset(out+n*2,0,static_cast<size_t>(frames-n)*2*sizeof(int16_t));if(d->firstMusic.load()>=0)d->outputMissing.fetch_add(frames-n);}return AAUDIO_CALLBACK_RESULT_CONTINUE;
}
bool openStream(Duplex* d,bool input){
    for(auto sharing:{AAUDIO_SHARING_MODE_EXCLUSIVE,AAUDIO_SHARING_MODE_SHARED}){
        AAudioStreamBuilder* builder=nullptr;AAudioStream* stream=nullptr;if(AAudio_createStreamBuilder(&builder)!=AAUDIO_OK)return false;
        AAudioStreamBuilder_setDirection(builder,input?AAUDIO_DIRECTION_INPUT:AAUDIO_DIRECTION_OUTPUT);AAudioStreamBuilder_setFormat(builder,AAUDIO_FORMAT_PCM_I16);
        AAudioStreamBuilder_setSampleRate(builder,48000);AAudioStreamBuilder_setChannelCount(builder,input?d->inputChannels:2);
        AAudioStreamBuilder_setPerformanceMode(builder,AAUDIO_PERFORMANCE_MODE_LOW_LATENCY);AAudioStreamBuilder_setSharingMode(builder,sharing);
        AAudioStreamBuilder_setDataCallback(builder,input?inputData:outputData,d);AAudioStreamBuilder_setErrorCallback(builder,onError,d);
        if(input){AAudioStreamBuilder_setDeviceId(builder,d->inputId);AAudioStreamBuilder_setInputPreset(builder,AAUDIO_INPUT_PRESET_UNPROCESSED);}
        else{AAudioStreamBuilder_setUsage(builder,AAUDIO_USAGE_GAME);AAudioStreamBuilder_setContentType(builder,AAUDIO_CONTENT_TYPE_MUSIC);}
        const auto result=AAudioStreamBuilder_openStream(builder,&stream);AAudioStreamBuilder_delete(builder);if(result!=AAUDIO_OK)continue;
        const bool exact=AAudioStream_getSampleRate(stream)==48000&&AAudioStream_getChannelCount(stream)==(input?d->inputChannels:2)&&AAudioStream_getFormat(stream)==AAUDIO_FORMAT_PCM_I16&&(!input||AAudioStream_getDeviceId(stream)==d->inputId);
        const auto mode=AAudioStream_getPerformanceMode(stream);
        if(!exact||!callbackRouteAllowed(input,mode==AAUDIO_PERFORMANCE_MODE_LOW_LATENCY,mode==AAUDIO_PERFORMANCE_MODE_NONE,AAudioStream_getFramesPerBurst(stream),AAudioStream_getBufferSizeInFrames(stream))){AAudioStream_close(stream);continue;}
        if(input)d->input=stream;else{d->output=stream;d->outputTarget=std::max(240,AAudioStream_getFramesPerBurst(stream)*2);
            // Keep the negotiated platform buffer during device acceptance. The TCL
            // callback path stalled when forced down to two bursts; the application
            // producer queue is still independently bounded to two bursts.
        }return true;
    }return false;
}
void stop(Duplex* d){d->stopped.store(true);d->platform.stop();if(d->input)AAudioStream_requestStop(d->input);if(d->output)AAudioStream_requestStop(d->output);}
void close(Duplex* d){stop(d);if(d->input)AAudioStream_close(d->input);if(d->output)AAudioStream_close(d->output);d->platform.close();d->in.clear();d->out.clear();delete d;}
void briefWait(){timespec delay{0,100000};nanosleep(&delay,nullptr);}
Duplex* ptr(jlong handle){return reinterpret_cast<Duplex*>(handle);}
}
extern "C" JNIEXPORT jlong JNICALL Java_local_friday_ktv_NativeAudioIO_openNative(JNIEnv*,jclass,jint input,jint channels,jint burst){
    if(input<=0||(channels!=1&&channels!=2))return 0;auto* d=new(std::nothrow) Duplex;if(!d)return 0;d->inputId=input;d->inputChannels=channels;d->in.channels=channels;
    if(!d->platform.open(d,[](void* owner,int16_t* samples,int frames){outputData(nullptr,owner,samples,frames);},burst)){lastOpenCode.store(d->platform.stage*100+d->platform.error.load());close(d);return 0;}if(!openStream(d,true)){lastOpenCode.store(101);close(d);return 0;}lastOpenCode.store(0);
    d->outputTarget=d->platform.frames*3;d->configuration[3]=AAudioStream_getDeviceId(d->input);d->configuration[4]=d->platform.frames*2;d->configuration[5]=AAudioStream_getBufferSizeInFrames(d->input);d->configuration[6]=d->platform.frames;d->configuration[7]=AAudioStream_getFramesPerBurst(d->input);d->configuration[8]=d->platform.performance;d->configuration[9]=AAudioStream_getPerformanceMode(d->input);d->configuration[16]=48000;d->configuration[17]=48000;d->configuration[24]=2;return reinterpret_cast<jlong>(d);
}
extern "C" JNIEXPORT jlong JNICALL Java_local_friday_ktv_NativeAudioIO_openMonitorNative(JNIEnv* env,jclass cls,jint input,jint channels,jint burst){auto value=Java_local_friday_ktv_NativeAudioIO_openNative(env,cls,input,channels,burst);if(value)ptr(value)->monitorOnly=true;return value;}
extern "C" JNIEXPORT jlong JNICALL Java_local_friday_ktv_NativeAudioIO_openMonitorAAudioNative(JNIEnv*,jclass,jint input,jint channels,jint){
    if(input<=0||(channels!=1&&channels!=2))return 0;auto* d=new(std::nothrow) Duplex;if(!d)return 0;
    d->monitorOnly=true;d->inputId=input;d->inputChannels=channels;d->in.channels=channels;
    if(!openStream(d,false)||!openStream(d,true)){lastOpenCode.store(401);close(d);return 0;}
    d->configuration[2]=AAudioStream_getDeviceId(d->output);d->routeId.store(static_cast<int>(d->configuration[2]));
    d->configuration[3]=AAudioStream_getDeviceId(d->input);d->configuration[4]=AAudioStream_getBufferSizeInFrames(d->output);d->configuration[5]=AAudioStream_getBufferSizeInFrames(d->input);
    d->configuration[6]=AAudioStream_getFramesPerBurst(d->output);d->configuration[7]=AAudioStream_getFramesPerBurst(d->input);
    d->configuration[8]=AAudioStream_getPerformanceMode(d->output);d->configuration[9]=AAudioStream_getPerformanceMode(d->input);d->configuration[24]=1;lastOpenCode.store(0);return reinterpret_cast<jlong>(d);
}
extern "C" JNIEXPORT jint JNICALL Java_local_friday_ktv_NativeAudioIO_startNative(JNIEnv*,jclass,jlong handle){auto* d=ptr(handle);if(!d)return -1;if(d->monitorOnly)d->monitorDeadline.store(now()+20000000000LL);auto result=AAudioStream_requestStart(d->input);if(result==AAUDIO_OK){result=d->output?AAudioStream_requestStart(d->output):d->platform.start();if(result)lastOpenCode.store(d->output?1000-result:d->platform.stage*100+d->platform.error.load());}else lastOpenCode.store(1000-result);return result;}
extern "C" JNIEXPORT jboolean JNICALL Java_local_friday_ktv_NativeAudioIO_armMonitorNative(JNIEnv*,jclass,jlong handle,jint output){auto* d=ptr(handle);if(!d||!d->monitorOnly||d->stopped.load()||output<=0||d->routeId.load()!=output||now()>=d->monitorDeadline.load())return false;d->monitorDeadline.store(std::min<int64_t>(d->monitorDeadline.load(),now()+15000000000LL));d->monitorArmed.store(true);return true;}
extern "C" JNIEXPORT void JNICALL Java_local_friday_ktv_NativeAudioIO_cancelMonitorNative(JNIEnv*,jclass,jlong handle){auto* d=ptr(handle);if(d&&d->monitorOnly){d->monitorArmed.store(false);d->stopped.store(true);}}
extern "C" JNIEXPORT jlongArray JNICALL Java_local_friday_ktv_NativeAudioIO_monitorMetricsNative(JNIEnv* env,jclass,jlong handle){auto* d=ptr(handle);auto result=env->NewLongArray(12);if(!d||!result)return result;jlong error=d->platform.error.load()?10000+d->platform.error.load():std::abs(d->error.load());jlong values[]{d->inputCallbackFrames.load(),d->callbackFrames.load(),d->monitorVoiceFrames.load(),d->inputDropped.load(),d->monitorTrimmed.load(),d->outputMissing.load(),d->monitorPeak.load(),error,d->configuration[8],d->configuration[9],d->configuration[4],d->configuration[5]};env->SetLongArrayRegion(result,0,12,values);return result;}
extern "C" JNIEXPORT jint JNICALL Java_local_friday_ktv_NativeAudioIO_routeNative(JNIEnv* env,jclass,jlong handle){auto* d=ptr(handle);if(!d)return 0;int id=d->output?AAudioStream_getDeviceId(d->output):d->platform.routedDevice(env);if(id>0)d->routeId.store(id);return id;}
extern "C" JNIEXPORT jint JNICALL Java_local_friday_ktv_NativeAudioIO_readNative(JNIEnv* env,jclass,jlong handle,jshortArray array,jint frames){auto* d=ptr(handle);if(!d||frames<1||frames>480||env->GetArrayLength(array)<frames*d->inputChannels)return -1;auto deadline=now()+500000000;while(d->in.size()==0&&!d->stopped.load()&&now()<deadline)briefWait();if(d->stopped.load())return -1;int16_t data[960]{};int n=d->in.pop(data,frames);if(n)env->SetShortArrayRegion(array,0,n*d->inputChannels,data);std::memset(data,0,sizeof(data));return n*d->inputChannels;}
extern "C" JNIEXPORT jint JNICALL Java_local_friday_ktv_NativeAudioIO_writeNative(JNIEnv* env,jclass,jlong handle,jshortArray array,jint offset,jint samples){auto* d=ptr(handle);if(!d||samples<2||samples>960||samples%2||offset<0||env->GetArrayLength(array)<offset+samples)return -1;auto deadline=now()+500000000;while(d->out.size()>=static_cast<uint64_t>(d->outputTarget)&&!d->stopped.load()&&now()<deadline)briefWait();if(d->stopped.load()||now()>=deadline)return -1;int frames=std::min(samples/2,d->outputTarget-static_cast<int>(d->out.size()));int16_t data[960]{};env->GetShortArrayRegion(array,offset,frames*2,data);int n=d->out.push(data,frames);std::memset(data,0,sizeof(data));return n*2;}
extern "C" JNIEXPORT void JNICALL Java_local_friday_ktv_NativeAudioIO_metricsNative(JNIEnv* env,jclass,jlong handle,jlongArray array){
    auto* d=ptr(handle);if(!d||env->GetArrayLength(array)<27)return;jlong m[27]{};
    const auto time=now(),first=d->firstMusic.load(),callbackTime=d->outputLastAt.load(),callbackSize=d->outputLastFrames.load();const auto consumed=d->out.read.load();
    // A callback estimate, never advertised as a hardware/DAC timestamp. No platform getters
    // or locks run on either producer/consumer. Acoustic acceptance remains a separate gate.
    const int64_t callbacks=d->callbackFrames.load();
    const int64_t within=callbackTime>0?std::min<int64_t>(callbackSize,std::max<int64_t>(0,time-callbackTime)*48000/1000000000LL):0;
    const int64_t presented=std::max<int64_t>(0,callbacks-callbackSize+within-d->configuration[4]);
    m[0]=first<0?0:std::max<int64_t>(0,std::min<int64_t>(consumed,presented-first-d->outputMissing.load()));
    m[1]=d->out.size()+std::max<int64_t>(0,callbacks-presented);
    for(int i=2;i<=9;i++)m[i]=d->configuration[i];m[2]=d->routeId.load();m[10]=d->inputDropped.load();m[11]=d->outputMissing.load();m[12]=-1;m[13]=-1;m[14]=-d->error.load();
    const auto inputTime=d->inputLastAt.load();m[15]=inputTime>0?(std::max<int64_t>(0,time-inputTime)+d->in.size()*1000000000LL/48000)/1000:0;
    m[16]=48000;m[17]=48000;m[18]=d->in.size();int total=0;for(auto& count:d->inputWaits)total+=count.load();int sum=0;for(int i=0;i<=100;i++){sum+=d->inputWaits[i].load();if(sum*100>=total*95){m[19]=(i+1)*1000;break;}}m[20]=d->inputGapMax.load();m[21]=callbacks;m[22]=callbackSize;m[23]=d->outputGapMax.load();m[24]=d->configuration[24];if(d->platform.error.load())m[14]=10000+d->platform.error.load();m[25]=time/1000000;m[26]=d->inputCallbackFrames.load();env->SetLongArrayRegion(array,0,27,m);
}
extern "C" JNIEXPORT void JNICALL Java_local_friday_ktv_NativeAudioIO_stopNative(JNIEnv*,jclass,jlong handle){if(handle)stop(ptr(handle));}
extern "C" JNIEXPORT void JNICALL Java_local_friday_ktv_NativeAudioIO_closeNative(JNIEnv*,jclass,jlong handle){if(handle)close(ptr(handle));}

extern "C" JNIEXPORT jint JNICALL Java_local_friday_ktv_NativeAudioIO_openCodeNative(JNIEnv*,jclass){return lastOpenCode.load();}
