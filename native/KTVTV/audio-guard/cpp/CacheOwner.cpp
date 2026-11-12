#include <jni.h>
#include <atomic>
#include <mutex>
#include "../../strict-cache/StrictCacheClient.h"

namespace {
std::mutex mutex;
FridayCacheClient owner{-1};
std::atomic<uint64_t> cancellation{0};
constexpr int Timeout=80;
void closeOwner(){if(owner.fd>=0)friday_cache_end(&owner,0);else friday_cache_close(&owner);}
}
extern "C" JNIEXPORT jlongArray JNICALL Java_local_friday_ktv_audio_NativeCacheOwner_contractNative(JNIEnv* env,jclass){
    // A probe connection never begins or invalidates an existing owner lease.
    FridayCacheClient probe{-1};jlong values[8]{};
    values[0]=friday_cache_connect(&probe,FRIDAY_CACHE_OWNER,Timeout);
    if(!values[0]){values[1]=probe.server_pid;values[2]=probe.server_uid;values[3]=probe.capabilities;values[4]=probe.rate;values[5]=probe.channels;values[6]=probe.format;values[7]=jlong(probe.instance_cookie);}
    friday_cache_close(&probe);auto out=env->NewLongArray(8);if(out)env->SetLongArrayRegion(out,0,8,values);return out;
}
extern "C" JNIEXPORT jint JNICALL Java_local_friday_ktv_audio_NativeCacheOwner_beginNative(JNIEnv*,jclass,jlong lease,jint card,jint device,jlong expectedCookie){
    if(lease<=0||card<0||card>31||device<0||device>31)return -EINVAL;
    const auto epoch=cancellation.load();std::lock_guard<std::mutex> lock(mutex);closeOwner();
    int result=friday_cache_connect(&owner,FRIDAY_CACHE_OWNER,Timeout);uint64_t generation=0;
    if(!result&&(!expectedCookie||owner.instance_cookie!=uint64_t(expectedCookie)))result=-ESTALE;
    if(!result)result=friday_cache_begin(&owner,uint64_t(lease),card,device,&generation,Timeout);
    if(cancellation.load()!=epoch)result=-ESTALE;
    if(result)closeOwner();return result;
}
extern "C" JNIEXPORT jint JNICALL Java_local_friday_ktv_audio_NativeCacheOwner_bindNative(JNIEnv*,jclass,jlong lease){
    const auto epoch=cancellation.load();std::lock_guard<std::mutex> lock(mutex);
    if(lease<=0||owner.fd<0||owner.lease!=uint64_t(lease))return -ESTALE;
    int result=friday_cache_bind_reader(&owner,0,Timeout);
    if(cancellation.load()!=epoch)result=-ESTALE;if(result)closeOwner();return result;
}
extern "C" JNIEXPORT jint JNICALL Java_local_friday_ktv_audio_NativeCacheOwner_heartbeatNative(JNIEnv*,jclass,jlong lease){
    const auto epoch=cancellation.load();std::lock_guard<std::mutex> lock(mutex);
    if(lease<=0||owner.fd<0||owner.lease!=uint64_t(lease))return -ESTALE;
    int result=friday_cache_heartbeat(&owner,Timeout);
    if(cancellation.load()!=epoch)result=-ESTALE;if(result)closeOwner();return result;
}
extern "C" JNIEXPORT void JNICALL Java_local_friday_ktv_audio_NativeCacheOwner_endNative(JNIEnv*,jclass){
    cancellation.fetch_add(1);
    // STOP never waits for a busy control exchange. Its finite operation will
    // observe cancellation; disconnect or server expiry independently revokes.
    std::unique_lock<std::mutex> lock(mutex,std::try_to_lock);if(lock.owns_lock())closeOwner();
}
extern "C" JNIEXPORT jint JNICALL Java_local_friday_ktv_audio_NativeCacheOwner_muteInlineNative(JNIEnv*,jclass,jlong lease,jboolean muted){
    const auto epoch=cancellation.load();std::lock_guard<std::mutex> lock(mutex);
    if(lease<=0||owner.fd<0||owner.lease!=uint64_t(lease)||owner.capabilities!=FRIDAY_CACHE_INLINE_CAPABILITIES)return -ESTALE;
    int result=friday_cache_inline_mute(&owner,muted?1:0,Timeout);
    if(cancellation.load()!=epoch)result=-ESTALE;if(result)closeOwner();return result;
}
extern "C" JNIEXPORT jint JNICALL Java_local_friday_ktv_audio_NativeCacheOwner_notchesInlineNative(JNIEnv* env,jclass,jlong lease,jintArray frequencies,jintArray cuts){
    if(!frequencies||!cuts||env->GetArrayLength(frequencies)!=8||env->GetArrayLength(cuts)!=8)return -EINVAL;
    jint hz[8]{},db[8]{};env->GetIntArrayRegion(frequencies,0,8,hz);env->GetIntArrayRegion(cuts,0,8,db);
    if(env->ExceptionCheck())return -EINVAL;
    for(int i=0;i<8;i++)if(hz[i]<188||hz[i]>6492||db[i]<0||db[i]>12)return -EINVAL;
    const auto epoch=cancellation.load();std::lock_guard<std::mutex> lock(mutex);
    if(lease<=0||owner.fd<0||owner.lease!=uint64_t(lease)||owner.capabilities!=FRIDAY_CACHE_INLINE_CAPABILITIES)return -ESTALE;
    int64_t deadline=0;int result=friday_cache_deadline(Timeout,&deadline);
    for(int i=0;i<8&&!result;i++)result=friday_cache_inline_notch_until(&owner,i,hz[i],db[i],deadline);
    if(cancellation.load()!=epoch)result=-ESTALE;if(result)closeOwner();return result;
}
