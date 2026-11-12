#include <CoreAudio/CoreAudio.h>
#include <CoreFoundation/CoreFoundation.h>
#include <unistd.h>
#include <string.h>
#include <stdlib.h>

static AudioDeviceID find(CFStringRef uid) {
    AudioObjectPropertyAddress a={kAudioHardwarePropertyDevices,kAudioObjectPropertyScopeGlobal,0};
    UInt32 size=0;
    if(AudioObjectGetPropertyDataSize(kAudioObjectSystemObject,&a,0,NULL,&size)) return 0;
    AudioDeviceID *list=calloc(1,size);AudioDeviceID found=0;
    if(list&&!AudioObjectGetPropertyData(kAudioObjectSystemObject,&a,0,NULL,&size,list)) {
        for(UInt32 i=0;i<size/sizeof(*list);++i) {
            AudioObjectPropertyAddress u={kAudioDevicePropertyDeviceUID,kAudioObjectPropertyScopeGlobal,0};
            CFStringRef value=NULL;UInt32 n=sizeof(value);
            if(!AudioObjectGetPropertyData(list[i],&u,0,NULL,&n,&value)&&value) {
                if(CFEqual(value,uid)) found=list[i];CFRelease(value);
            }
        }
    }
    free(list);return found;
}
int main(int argc,char **argv) {
    if(argc!=2) return 2;
    // No audio samples or credentials enter this pipe. EOF means the app exited/crashed.
    char byte;while(read(STDIN_FILENO,&byte,1)>0) { if(byte=='K') return 0; }
    AudioObjectPropertyAddress a={kAudioHardwarePropertyDefaultOutputDevice,kAudioObjectPropertyScopeGlobal,0};
    AudioDeviceID current=0;UInt32 size=sizeof(current);
    if(AudioObjectGetPropertyData(kAudioObjectSystemObject,&a,0,NULL,&size,&current)) return 3;
    if(current!=find(CFSTR("local.friday.LGTVAudio.device"))) return 0;
    CFStringRef uid=CFStringCreateWithCString(NULL,argv[1],kCFStringEncodingUTF8);
    AudioDeviceID output=uid?find(uid):0;if(uid) CFRelease(uid);
    if(!output) output=find(CFSTR("BuiltInSpeakerDevice"));
    if(!output) return 4;
    return AudioObjectSetPropertyData(kAudioObjectSystemObject,&a,0,NULL,sizeof(output),&output)?5:0;
}
