#ifndef FRIDAY_VENDOR_PAYLOAD_PROFILE_H
#define FRIDAY_VENDOR_PAYLOAD_PROFILE_H
#include <array>
#include <cstddef>
#include <cstdint>
namespace fridaystrict {
// Exact private firmware input; no vendor bytes are part of the source tree.
constexpr char PayloadName[]="libfridaygm.payload.so";
constexpr char PayloadPath[]="/vendor/lib64/libfridaygm.payload.so";
constexpr char ModulePath[]="/vendor/lib64/libtlkg.hal.globalmic.so";
constexpr char PayloadSha[]="1bb2966772f03d4c341c9fbe268a0b273970e0fe72cc2ce79cca69819631cdc3";
constexpr char PayloadRxSha[]="c40ec407e67879bc712cd73526cda573fa28dfc8aaffdfd49dc849d27137332d";
constexpr size_t PayloadBytes=644808,PayloadRxBytes=614408;
constexpr uintptr_t PayloadDynamic=0x9b430,PayloadDynamicBytes=0x210;
constexpr uintptr_t PayloadOpen=0x31334,PayloadDescriptor=0x9d008;
constexpr std::array<uintptr_t,11> PayloadOps{{
    0x31588,0x315e0,0x31708,0x31628,0x31658,0x3146c,
    0x314ac,0x31550,0x314ec,0x31698,0x316d8
}};
}
#endif
