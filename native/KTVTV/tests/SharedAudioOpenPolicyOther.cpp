#include "../android/cpp/SharedAudioOpenPolicy.h"
std::mutex* fridayPolicyMutexFromOtherTranslationUnit() { return &fridayAudioOpenPolicyMutex(); }
