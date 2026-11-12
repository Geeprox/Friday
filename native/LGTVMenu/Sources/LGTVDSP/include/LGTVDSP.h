#ifndef LGTV_DSP_H
#define LGTV_DSP_H
#include <stdbool.h>
#include <stdint.h>
#include <CoreAudio/CoreAudio.h>

typedef struct LGTVAudioRing LGTVAudioRing;
LGTVAudioRing *LGTVRingCreate(void);
void LGTVRingDestroy(LGTVAudioRing *ring);
void LGTVRingReset(LGTVAudioRing *ring);
void LGTVRingWrite(LGTVAudioRing *ring, int64_t sampleTime, uint32_t frames, const float *stereo);
void LGTVRingRead(LGTVAudioRing *ring, int64_t sampleTime, uint32_t frames, float *stereo);

// Copies only virtual-device input into the selected physical output channels.
// No gain, allocation, locks, logging, files or network work occurs in this callback.
bool LGTVRouteAudio(const AudioBufferList *input, AudioBufferList *output,
                    uint32_t destinationChannelOffset);
typedef struct { uint64_t callbacks; uint64_t routedFrames; uint64_t nonzeroSamples; } LGTVRouteStats;
void LGTVResetRouteStats(void);
LGTVRouteStats LGTVGetRouteStats(void);
// Client-side only; LGTVForwarder.c is deliberately excluded from the HAL bundle.
OSStatus LGTVCreateRouteIOProc(AudioDeviceID device, uint32_t outputOffset, AudioDeviceIOProcID *proc);
#endif
