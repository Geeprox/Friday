#pragma once
#include <cstdint>
namespace fridaywet {
enum Fault { NoFault=0, InputFailure=1, InputStale=2, CallbackStale=3, RouteStale=4, RouteMismatch=5, OutputFailure=6, ContractMismatch=7 };
inline bool overdue(uint64_t now,uint64_t then,uint64_t limit){return then>0&&now>=then&&now-then>limit;}
inline int healthFault(uint64_t now,uint64_t started,uint64_t input,uint64_t callback,uint64_t route,bool outputError){
    if(outputError)return OutputFailure;
    if(overdue(now,input,100000000ULL)||(!input&&overdue(now,started,1000000000ULL)))return InputStale;
    if(overdue(now,callback,250000000ULL)||(!callback&&overdue(now,started,1500000000ULL)))return CallbackStale;
    if(overdue(now,route,750000000ULL)||(!route&&overdue(now,started,1500000000ULL)))return RouteStale;
    return NoFault;
}
}
