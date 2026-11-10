#include <aaudio/AAudio.h>
#include <jni.h>
#include <atomic>
#include <algorithm>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <time.h>

struct Probe {std::atomic<int64_t> frames{0};std::atomic<int32_t> peak{0};int channels;bool input;};
static aaudio_data_callback_result_t callback(AAudioStream*,void* user,void* audio,int32_t frames){
    auto* p=static_cast<Probe*>(user);
    if(p->input){const auto* samples=static_cast<int16_t*>(audio);int peak=0;for(int i=0;i<frames*p->channels;i++)peak=std::max(peak,std::abs(static_cast<int>(samples[i])));p->peak.store(std::max(p->peak.load(),peak));}
    else std::memset(audio,0,static_cast<size_t>(frames)*p->channels*sizeof(int16_t));
    p->frames.fetch_add(frames);return AAUDIO_CALLBACK_RESULT_CONTINUE;
}
static void probe(int inputDevice,int channels,bool input,jlong* values){
    for(auto sharing:{AAUDIO_SHARING_MODE_EXCLUSIVE,AAUDIO_SHARING_MODE_SHARED}){
        AAudioStreamBuilder* builder=nullptr;AAudioStream* stream=nullptr;Probe state;state.input=input;state.channels=channels;
        aaudio_result_t result=AAudio_createStreamBuilder(&builder);
        if(result!=AAUDIO_OK){values[0]=-result;return;}
        AAudioStreamBuilder_setDirection(builder,input?AAUDIO_DIRECTION_INPUT:AAUDIO_DIRECTION_OUTPUT);
        AAudioStreamBuilder_setDeviceId(builder,input?inputDevice:AAUDIO_UNSPECIFIED);
        AAudioStreamBuilder_setFormat(builder,AAUDIO_FORMAT_PCM_I16);AAudioStreamBuilder_setSampleRate(builder,48000);
        AAudioStreamBuilder_setChannelCount(builder,channels);AAudioStreamBuilder_setPerformanceMode(builder,AAUDIO_PERFORMANCE_MODE_LOW_LATENCY);
        AAudioStreamBuilder_setSharingMode(builder,sharing);AAudioStreamBuilder_setDataCallback(builder,callback,&state);
        if(input)AAudioStreamBuilder_setInputPreset(builder,AAUDIO_INPUT_PRESET_UNPROCESSED);
        else{AAudioStreamBuilder_setUsage(builder,AAUDIO_USAGE_GAME);AAudioStreamBuilder_setContentType(builder,AAUDIO_CONTENT_TYPE_MUSIC);}
        result=AAudioStreamBuilder_openStream(builder,&stream);AAudioStreamBuilder_delete(builder);
        if(result!=AAUDIO_OK){values[0]=-result;continue;}
        if(AAudioStream_getSampleRate(stream)!=48000||AAudioStream_getChannelCount(stream)!=channels||AAudioStream_getFormat(stream)!=AAUDIO_FORMAT_PCM_I16||(input&&AAudioStream_getDeviceId(stream)!=inputDevice)){AAudioStream_close(stream);values[0]=999;continue;}
        result=AAudioStream_requestStart(stream);
        if(result==AAUDIO_OK){for(int i=0;i<15&&state.frames.load()<9600;i++){timespec delay{0,20000000};nanosleep(&delay,nullptr);}}
        values[0]=result==AAUDIO_OK?1:-result;values[1]=AAudioStream_getPerformanceMode(stream);values[2]=AAudioStream_getFramesPerBurst(stream);values[3]=AAudioStream_getBufferSizeInFrames(stream);values[4]=AAudioStream_getSharingMode(stream);values[5]=AAudioStream_getDeviceId(stream);values[6]=state.frames.load();values[7]=state.peak.load();
        AAudioStream_requestStop(stream);AAudioStream_close(stream);
        if(values[0]==1&&values[1]==AAUDIO_PERFORMANCE_MODE_LOW_LATENCY)return;
    }
}
extern "C" JNIEXPORT jlongArray JNICALL Java_local_friday_ktv_NativeAudioProbe_inspect(JNIEnv* env,jclass,jint input,jint channels){
    jlong values[16]{};if(input<=0||(channels!=1&&channels!=2))return env->NewLongArray(16);
    probe(input,2,false,values);probe(input,channels,true,values+8);
    auto out=env->NewLongArray(16);if(out)env->SetLongArrayRegion(out,0,16,values);return out;
}
