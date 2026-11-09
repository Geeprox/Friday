#include "../android/cpp/PcmQueue.h"
#include <thread>
#include <cassert>
#include <iostream>
int main(){
    PcmQueue queue;constexpr int frames=2000000;
    std::thread producer([&]{int at=0;int16_t samples[480];while(at<frames){int count=std::min(240,frames-at);for(int i=0;i<count;i++){samples[i*2]=static_cast<int16_t>((at+i)%20000);samples[i*2+1]=static_cast<int16_t>(-samples[i*2]);}int n=queue.push(samples,count);at+=n;if(n==0)std::this_thread::yield();}});
    int at=0;int16_t samples[384];while(at<frames){int n=queue.pop(samples,std::min(192,frames-at));for(int i=0;i<n;i++){assert(samples[i*2]==static_cast<int16_t>((at+i)%20000));assert(samples[i*2+1]==-samples[i*2]);}at+=n;if(n==0)std::this_thread::yield();}producer.join();assert(queue.size()==0);queue.clear();for(auto value:queue.data)assert(value==0);std::cout<<"Native PCM queue preserves sample order across concurrent unequal bursts; no device opened\n";
}
