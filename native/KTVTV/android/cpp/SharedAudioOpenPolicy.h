#pragma once
#include <mutex>

// All users of AAudio_setMMapPolicy in this process must share this mutex.
// Diagnostic and production opens use the same inline-function static instance.
inline std::mutex& fridayAudioOpenPolicyMutex() {
    static std::mutex mutex;
    return mutex;
}
