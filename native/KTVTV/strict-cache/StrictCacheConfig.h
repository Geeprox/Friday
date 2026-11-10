#ifndef FRIDAY_STRICT_CACHE_CONFIG_H
#define FRIDAY_STRICT_CACHE_CONFIG_H
#include "StrictCacheCore.h"
#include <cstdlib>
#include <cstring>
namespace fridaystrict {
inline bool configAuthority(unsigned uid,unsigned mode,unsigned links,bool directory){
    return (uid==0||uid==1041)&&(mode&07777)==(directory?0700:0600)&&(directory||links==1);
}
/* Mutable bounded local configuration, never RPC content. No live UID defaults. */
inline int parseConfig(char* text,size_t bytes,Config& c){
    if(!bytes||bytes>256||std::memchr(text,0,bytes))return -EINVAL;text[bytes]=0;bool owner=false,reader=false;char* save=nullptr;
    for(char* line=strtok_r(text,"\n",&save);line;line=strtok_r(nullptr,"\n",&save)){
        if(!*line||*line=='#')continue;char* equal=std::strchr(line,'=');if(!equal)return -EINVAL;*equal++=0;
        if(!*equal)return -EINVAL;for(char* p=equal;*p;p++)if(*p<'0'||*p>'9')return -EINVAL;
        errno=0;char* end=nullptr;unsigned long uid=strtoul(equal,&end,10);if(errno||*end||uid<10000||uid>2147483647)return -EINVAL;
        if(!std::strcmp(line,"owner_uid")&&!owner){c.ownerUid=uint32_t(uid);owner=true;}
        else if(!std::strcmp(line,"reader_uid")&&!reader){c.readerUid=uint32_t(uid);reader=true;}
        else return -EINVAL;
    }
    return owner&&reader?0:-EINVAL;
}
}
#endif
