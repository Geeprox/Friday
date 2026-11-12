#pragma once
#include <SLES/OpenSLES.h>
#include <SLES/OpenSLES_Android.h>
#include <SLES/OpenSLES_AndroidConfiguration.h>
#include <atomic>
#include <cstring>
#include <jni.h>

// Derived from the existing project OpenSLOutput, scoped to ordinary MEDIA output.
// No AAudio output, input read, JNI, allocation, locks, files, or network in callback.
struct WetOpenSL {
    SLObjectItf engineObject=nullptr,mixObject=nullptr,playerObject=nullptr;
    SLEngineItf engine=nullptr;SLPlayItf player=nullptr;
    SLAndroidSimpleBufferQueueItf queue=nullptr;SLAndroidConfigurationItf config=nullptr;
    jobject routing=nullptr;
    std::atomic<bool> active{false};std::atomic<int> error{0};
    void* owner=nullptr;void (*fill)(void*,int16_t*,int)=nullptr;
    int frames=240,next=0,stage=0;SLuint32 performance=SL_ANDROID_PERFORMANCE_NONE;
    int16_t buffers[2][960]{};
    static void callback(SLAndroidSimpleBufferQueueItf caller,void* context){
        auto* self=static_cast<WetOpenSL*>(context);if(!self->active.load(std::memory_order_acquire))return;
        auto* buffer=self->buffers[self->next];self->next^=1;self->fill(self->owner,buffer,self->frames);
        if(self->active.load(std::memory_order_acquire)){
            const auto result=(*caller)->Enqueue(caller,buffer,self->frames*4);
            if(result!=SL_RESULT_SUCCESS){self->error.store(result);self->active.store(false);}
        }
    }
    bool ok(SLresult result){if(result!=SL_RESULT_SUCCESS)error.store(result);return result==SL_RESULT_SUCCESS;}
    bool open(void* context,void (*renderer)(void*,int16_t*,int)){
        owner=context;fill=renderer;
        stage=201;if(!ok(slCreateEngine(&engineObject,0,nullptr,0,nullptr,nullptr)))return false;
        stage=202;if(!ok((*engineObject)->Realize(engineObject,SL_BOOLEAN_FALSE)))return false;
        stage=203;if(!ok((*engineObject)->GetInterface(engineObject,SL_IID_ENGINE,&engine)))return false;
        stage=204;if(!ok((*engine)->CreateOutputMix(engine,&mixObject,0,nullptr,nullptr)))return false;
        stage=205;if(!ok((*mixObject)->Realize(mixObject,SL_BOOLEAN_FALSE)))return false;
        SLDataLocator_AndroidSimpleBufferQueue locator{SL_DATALOCATOR_ANDROIDSIMPLEBUFFERQUEUE,2};
        SLDataFormat_PCM format{SL_DATAFORMAT_PCM,2,SL_SAMPLINGRATE_48,SL_PCMSAMPLEFORMAT_FIXED_16,SL_PCMSAMPLEFORMAT_FIXED_16,SL_SPEAKER_FRONT_LEFT|SL_SPEAKER_FRONT_RIGHT,SL_BYTEORDER_LITTLEENDIAN};
        SLDataSource source{&locator,&format};SLDataLocator_OutputMix output{SL_DATALOCATOR_OUTPUTMIX,mixObject};SLDataSink sink{&output,nullptr};
        const SLInterfaceID ids[]{SL_IID_BUFFERQUEUE,SL_IID_ANDROIDCONFIGURATION};const SLboolean required[]{SL_BOOLEAN_TRUE,SL_BOOLEAN_TRUE};
        stage=206;if(!ok((*engine)->CreateAudioPlayer(engine,&playerObject,&source,&sink,2,ids,required)))return false;
        stage=207;if(!ok((*playerObject)->GetInterface(playerObject,SL_IID_ANDROIDCONFIGURATION,&config)))return false;
        SLuint32 media=SL_ANDROID_STREAM_MEDIA,requested=SL_ANDROID_PERFORMANCE_NONE;
        stage=208;if(!ok((*config)->SetConfiguration(config,SL_ANDROID_KEY_STREAM_TYPE,&media,sizeof(media))))return false;
        if(!ok((*config)->SetConfiguration(config,SL_ANDROID_KEY_PERFORMANCE_MODE,&requested,sizeof(requested))))return false;
        stage=209;if(!ok((*playerObject)->Realize(playerObject,SL_BOOLEAN_FALSE)))return false;
        SLuint32 size=sizeof(performance);
        stage=210;if(!ok((*config)->GetConfiguration(config,SL_ANDROID_KEY_PERFORMANCE_MODE,&size,&performance)))return false;
        if(performance!=SL_ANDROID_PERFORMANCE_NONE){error.store(50+performance);return false;}
        stage=211;if(!ok((*playerObject)->GetInterface(playerObject,SL_IID_PLAY,&player)))return false;
        stage=212;if(!ok((*playerObject)->GetInterface(playerObject,SL_IID_BUFFERQUEUE,&queue)))return false;
        stage=220;return ok((*queue)->RegisterCallback(queue,callback,this));
    }
    int start(){
        if(!queue||!player)return -1;next=0;std::memset(buffers,0,sizeof(buffers));active.store(true);
        stage=301;for(auto& buffer:buffers)if(!ok((*queue)->Enqueue(queue,buffer,frames*4))){active.store(false);return -1;}
        stage=302;return ok((*player)->SetPlayState(player,SL_PLAYSTATE_PLAYING))?0:-1;
    }
    int routedDevice(JNIEnv* env){
        if(!config)return 0;
        if(!routing&&(*config)->AcquireJavaProxy(config,SL_ANDROID_JAVA_PROXY_ROUTING,&routing)!=SL_RESULT_SUCCESS)return 0;
        auto type=env->GetObjectClass(routing);auto getter=env->GetMethodID(type,"getRoutedDevice","()Landroid/media/AudioDeviceInfo;");env->DeleteLocalRef(type);
        if(!getter||env->ExceptionCheck()){env->ExceptionClear();return 0;}
        auto device=env->CallObjectMethod(routing,getter);if(env->ExceptionCheck()){env->ExceptionClear();return 0;}if(!device)return 0;
        type=env->GetObjectClass(device);auto idMethod=env->GetMethodID(type,"getId","()I");int id=idMethod?env->CallIntMethod(device,idMethod):0;env->DeleteLocalRef(type);env->DeleteLocalRef(device);
        if(env->ExceptionCheck()){env->ExceptionClear();return 0;}return id;
    }
    void stop(){active.store(false,std::memory_order_release);if(player)(*player)->SetPlayState(player,SL_PLAYSTATE_STOPPED);}
    void close(){
        stop();if(queue)(*queue)->Clear(queue);
        if(routing&&config){(*config)->ReleaseJavaProxy(config,SL_ANDROID_JAVA_PROXY_ROUTING);routing=nullptr;}
        if(playerObject)(*playerObject)->Destroy(playerObject);
        playerObject=nullptr;player=nullptr;queue=nullptr;config=nullptr;
        if(mixObject)(*mixObject)->Destroy(mixObject);mixObject=nullptr;
        if(engineObject)(*engineObject)->Destroy(engineObject);engineObject=nullptr;engine=nullptr;
        std::memset(buffers,0,sizeof(buffers));owner=nullptr;fill=nullptr;
        // Preserve the last open/start stage for post-stop diagnostics. Closure
        // is reported separately; overwriting a failure with 400 hides its cause.
    }
};
