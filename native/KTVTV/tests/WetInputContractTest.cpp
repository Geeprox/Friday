#include "../android/cpp/wet/WetInputContract.h"
#include <cassert>
int main(){
    using fridaywet::inputContract;
    assert(inputContract(48000,2,1,1,96000,2,1));
    assert(inputContract(48000,2,1,1,48000,2,1));
    assert(!inputContract(96000,2,1,1,96000,2,1));
    assert(!inputContract(48000,1,1,1,96000,2,1));
    assert(!inputContract(48000,2,2,1,96000,2,1));
    assert(!inputContract(48000,2,1,0,96000,2,1));
    assert(!inputContract(48000,2,1,1,44100,2,1));
    assert(!inputContract(48000,2,1,1,96000,1,1));
    assert(!inputContract(48000,2,1,1,96000,2,2));
}
