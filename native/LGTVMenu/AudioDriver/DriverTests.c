// In-process fake host only. Does not load a driver, open hardware, or play audio.
#include "LGTVAudio.c"
#include <assert.h>
#include <stdio.h>

static int notifications=0;
static OSStatus changed(AudioServerPlugInHostRef h,AudioObjectID o,UInt32 n,const AudioObjectPropertyAddress *a) { ++notifications;return 0; }
static void report(int volume,bool mute,bool ready) {
    CFNumberRef v=CFNumberCreate(NULL,kCFNumberIntType,&volume);
    const void *keys[]={CFSTR("volume"),CFSTR("muted"),CFSTR("ready")};
    const void *values[]={v,mute?kCFBooleanTrue:kCFBooleanFalse,ready?kCFBooleanTrue:kCFBooleanFalse};
    CFDictionaryRef d=CFDictionaryCreate(NULL,keys,values,3,&kCFTypeDictionaryKeyCallBacks,&kCFTypeDictionaryValueCallBacks);
    assert(LGTVApplyReported(sizeof(d),&d)==0);CFRelease(d);CFRelease(v);
}
int main(void) {
    AudioServerPlugInHostInterface host={0};host.PropertiesChanged=changed;gPlugIn_Host=&host;
    gLGTVRing=LGTVRingCreate();assert(gLGTVRing);
    AudioValueRange rates[2];UInt32 returned=0;
    AudioObjectPropertyAddress ratesAddress={kAudioDevicePropertyAvailableNominalSampleRates,kAudioObjectPropertyScopeGlobal,0};
    assert(LGTVAudio_GetPropertyData(gAudioServerPlugInDriverRef,kObjectID_Device,0,&ratesAddress,0,NULL,sizeof(rates),&returned,rates)==0);
    assert(returned==sizeof(rates)&&rates[0].mMinimum==44100&&rates[0].mMaximum==44100&&rates[1].mMinimum==48000&&rates[1].mMaximum==48000);
    AudioObjectPropertyAddress sources={kAudioSelectorControlPropertyAvailableItems,kAudioObjectPropertyScopeGlobal,0};
    UInt32 sourceCountBytes=0;
    assert(LGTVAudio_GetPropertyDataSize(gAudioServerPlugInDriverRef,kObjectID_DataSource_Output_Master,0,&sources,0,NULL,&sourceCountBytes)==0);
    assert(sourceCountBytes==sizeof(UInt32)); // No sample-driver placeholder outputs in the system picker.
    AudioObjectPropertyAddress a={kAudioLevelControlPropertyScalarValue,kAudioObjectPropertyScopeGlobal,0};
    Float32 volume=.18;
    assert(LGTVAudio_SetPropertyData(gAudioServerPlugInDriverRef,kObjectID_Volume_Output_Master,0,&a,0,NULL,sizeof(volume),&volume)!=0);
    report(17,false,true);assert(gLGTVIntentRevision==0);
    assert(LGTVAudio_SetPropertyData(gAudioServerPlugInDriverRef,kObjectID_Volume_Output_Master,0,&a,0,NULL,sizeof(volume),&volume)==0);
    assert(gLGTVIntentRevision==1);
    report(17,false,true);assert(gLGTVIntentRevision==1);
    Float32 pcm[]={.75,-.5,.25,-.125},out[4]={0};
    AudioServerPlugInIOCycleInfo cycle={0};cycle.mOutputTime.mSampleTime=9000;cycle.mInputTime.mSampleTime=10024;
    assert(LGTVAudio_DoIOOperation(gAudioServerPlugInDriverRef,kObjectID_Device,kObjectID_Stream_Output,0,kAudioServerPlugInIOOperationWriteMix,2,&cycle,pcm,NULL)==0);
    assert(LGTVAudio_DoIOOperation(gAudioServerPlugInDriverRef,kObjectID_Device,kObjectID_Stream_Input,0,kAudioServerPlugInIOOperationReadInput,2,&cycle,out,NULL)==0);
    assert(memcmp(pcm,out,sizeof(pcm))==0); // Volume is hardware metadata, no double attenuation.
    report(17,true,true);
    LGTVAudio_DoIOOperation(gAudioServerPlugInDriverRef,kObjectID_Device,kObjectID_Stream_Input,0,kAudioServerPlugInIOOperationReadInput,2,&cycle,out,NULL);
    for(int i=0;i<4;++i) assert(out[i]==0);
    report(17,false,false);assert(atomic_load(&gLGTVSilence));
    // Aggregates may expose planar buffers and choose either subdevice order.
    Float32 left[]={.75,.25},right[]={-.5,-.125},planes[4][2];
    struct { UInt32 count; AudioBuffer buffers[2]; } input={2,{{1,sizeof(left),left},{1,sizeof(right),right}}};
    struct { UInt32 count; AudioBuffer buffers[4]; } output={4};
    for(int i=0;i<4;++i) output.buffers[i]=(AudioBuffer){1,sizeof(planes[i]),planes[i]};
    for(UInt32 offset=0;offset<=2;offset+=2) {
        memset(planes,0x7f,sizeof(planes));
        assert(LGTVRouteAudio((AudioBufferList*)&input,(AudioBufferList*)&output,offset));
        assert(memcmp(planes[offset],left,sizeof(left))==0);
        assert(memcmp(planes[offset+1],right,sizeof(right))==0);
        UInt32 virtualOffset=offset?0:2;
        for(int frame=0;frame<2;++frame) assert(planes[virtualOffset][frame]==0&&planes[virtualOffset+1][frame]==0);
    }
    assert(!LGTVRouteAudio(NULL,(AudioBufferList*)&output,0));
    for(int i=0;i<4;++i) for(int frame=0;frame<2;++frame) assert(planes[i][frame]==0);
    assert(notifications>0);LGTVRingDestroy(gLGTVRing);
    puts("Driver metadata, feedback isolation, unchanged PCM and mute gates passed.");return 0;
}
