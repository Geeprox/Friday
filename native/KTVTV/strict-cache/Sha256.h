#ifndef FRIDAY_STRICT_SHA256_H
#define FRIDAY_STRICT_SHA256_H
#include <array>
#include <cstdint>
#include <cstring>
namespace fridaystrict {
class Sha256 {
    uint32_t h_[8]={0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19};
    uint8_t block_[64]{};uint64_t length_=0;size_t used_=0;
    static uint32_t r(uint32_t x,int n){return(x>>n)|(x<<(32-n));}
    void block(){
        static constexpr uint32_t k[64]={0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
            0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,
            0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
            0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,
            0xd192e819,0xd6990624,0xf40e3585,0x106aa070,0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
            0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2};
        uint32_t w[64];for(int i=0;i<16;i++)w[i]=(uint32_t(block_[4*i])<<24)|(uint32_t(block_[4*i+1])<<16)|(uint32_t(block_[4*i+2])<<8)|block_[4*i+3];
        for(int i=16;i<64;i++){uint32_t a=r(w[i-15],7)^r(w[i-15],18)^(w[i-15]>>3),b=r(w[i-2],17)^r(w[i-2],19)^(w[i-2]>>10);w[i]=w[i-16]+a+w[i-7]+b;}
        uint32_t a=h_[0],b=h_[1],c=h_[2],d=h_[3],e=h_[4],f=h_[5],g=h_[6],v=h_[7];
        for(int i=0;i<64;i++){uint32_t t1=v+(r(e,6)^r(e,11)^r(e,25))+((e&f)^(~e&g))+k[i]+w[i];uint32_t t2=(r(a,2)^r(a,13)^r(a,22))+((a&b)^(a&c)^(b&c));v=g;g=f;f=e;e=d+t1;d=c;c=b;b=a;a=t1+t2;}
        h_[0]+=a;h_[1]+=b;h_[2]+=c;h_[3]+=d;h_[4]+=e;h_[5]+=f;h_[6]+=g;h_[7]+=v;
    }
public:
    void add(const void* memory,size_t bytes){const auto* p=static_cast<const uint8_t*>(memory);length_+=bytes;while(bytes){size_t n=64-used_;if(n>bytes)n=bytes;std::memcpy(block_+used_,p,n);p+=n;used_+=n;bytes-=n;if(used_==64){block();used_=0;}}}
    std::array<uint8_t,32> finish(){uint64_t bits=length_*8;block_[used_++]=0x80;if(used_>56){while(used_<64)block_[used_++]=0;block();used_=0;}
        while(used_<56)block_[used_++]=0;for(int i=7;i>=0;i--)block_[used_++]=uint8_t(bits>>(8*i));block();std::array<uint8_t,32> out{};
        for(int i=0;i<32;i++)out[i]=uint8_t(h_[i/4]>>(24-8*(i%4)));return out;}
    static bool matches(const void* memory,size_t bytes,const char* hex){Sha256 h;h.add(memory,bytes);auto v=h.finish();static constexpr char digits[]="0123456789abcdef";unsigned different=0;
        for(int i=0;i<32;i++){different|=unsigned(hex[i*2]^digits[v[i]>>4]);different|=unsigned(hex[i*2+1]^digits[v[i]&15]);}return !different;}
};
}
#endif
