#include "LGTVDSP.h"

static OSStatus route(AudioDeviceID device, const AudioTimeStamp *now,
                      const AudioBufferList *input, const AudioTimeStamp *inputTime,
                      AudioBufferList *output, const AudioTimeStamp *outputTime, void *context) {
    (void)device;(void)now;(void)inputTime;(void)outputTime;
    LGTVRouteAudio(input,output,(uint32_t)(uintptr_t)context);
    return noErr;
}
OSStatus LGTVCreateRouteIOProc(AudioDeviceID device,uint32_t outputOffset,AudioDeviceIOProcID *proc) {
    if(outputOffset!=0&&outputOffset!=2) return kAudioHardwareIllegalOperationError;
    return AudioDeviceCreateIOProcID(device,route,(void*)(uintptr_t)outputOffset,proc);
}
