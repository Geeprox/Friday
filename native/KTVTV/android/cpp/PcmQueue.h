#pragma once
#include <atomic>
#include <algorithm>
#include <cstdint>
#include <cstring>

/** One producer and one consumer. No allocation, locks or waits in callbacks. */
struct PcmQueue {
    static constexpr int CAPACITY=8192;
    int16_t data[CAPACITY*2]{};int channels=2;
    std::atomic<uint64_t> written{0},read{0};
    uint64_t size()const{const auto r=read.load(std::memory_order_acquire);return written.load(std::memory_order_acquire)-r;}
    int push(const int16_t* samples,int frames){const auto w=written.load(std::memory_order_relaxed),r=read.load(std::memory_order_acquire);int n=std::min<int>(frames,CAPACITY-static_cast<int>(w-r));for(int i=0;i<n;i++)for(int c=0;c<channels;c++)data[((w+i)%CAPACITY)*channels+c]=samples[i*channels+c];written.store(w+n,std::memory_order_release);return n;}
    int pop(int16_t* samples,int frames){const auto r=read.load(std::memory_order_relaxed),w=written.load(std::memory_order_acquire);int n=std::min<int>(frames,static_cast<int>(w-r));for(int i=0;i<n;i++)for(int c=0;c<channels;c++)samples[i*channels+c]=data[((r+i)%CAPACITY)*channels+c];read.store(r+n,std::memory_order_release);return n;}
    void clear(){std::memset(data,0,sizeof(data));}
};
