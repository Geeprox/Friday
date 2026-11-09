#pragma once
namespace fridaywet {
// Reads are always client PCM16 stereo at 48 kHz. The framework can expose that
// stream over this USB HAL's 96 kHz hardware clock; that does not prove low
// acoustic latency or that the OEM cache hook avoids reopening physical capture.
inline bool inputContract(int clientRate,int channels,int format,int sharing,
                          int hardwareRate,int hardwareChannels,int hardwareFormat) {
    return clientRate==48000&&channels==2&&format==1&&sharing==1
        &&(hardwareRate==0||hardwareRate==48000||hardwareRate==96000)
        &&(hardwareChannels==0||hardwareChannels==2)
        &&(hardwareFormat==0||hardwareFormat==1);
}
}
