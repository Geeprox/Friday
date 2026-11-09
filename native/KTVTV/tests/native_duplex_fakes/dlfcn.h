#pragma once
#include <atomic>
#include <cstring>
#include <aaudio/AAudio.h>
constexpr int RTLD_NOW = 2, RTLD_LOCAL = 0;
inline std::atomic<int> fakePolicy{0}, fakePolicySetCalls{0}, fakePolicyFailAt{0};
inline std::atomic<bool> fakeSymbolsAvailable{true}, fakeMMapUsed{false};
inline int fakeGetPolicy() { return fakePolicy.load(); }
inline int fakeSetPolicy(int value) {
    if (++fakePolicySetCalls == fakePolicyFailAt) return -895;
    fakePolicy.store(value); return 0;
}
inline bool fakeIsMMapUsed(AAudioStream*) { return fakeMMapUsed; }
inline void* dlopen(const char*, int) { return fakeSymbolsAvailable ? reinterpret_cast<void*>(1) : nullptr; }
inline int dlclose(void*) { return 0; }
inline void* dlsym(void*, const char* name) {
    if (!std::strcmp(name, "AAudio_getMMapPolicy")) return reinterpret_cast<void*>(fakeGetPolicy);
    if (!std::strcmp(name, "AAudio_setMMapPolicy")) return reinterpret_cast<void*>(fakeSetPolicy);
    if (!std::strcmp(name, "AAudioStream_isMMapUsed")) return reinterpret_cast<void*>(fakeIsMMapUsed);
    return nullptr;
}
