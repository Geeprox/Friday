// Confine the SDK worker's UDP sockets to the configured LAN interface.
// This process-local workaround is needed on multi-interface Macs. It does not
// change the system route, VPN, firewall or another process's sockets.
#include <sys/socket.h>
#include <netinet/in.h>
#include <net/if.h>
#include <stdint.h>
#include <stdlib.h>
#include <unistd.h>

static int camera_socket(int domain, int type, int protocol) {
    int fd = socket(domain, type, protocol);
    const char *name = getenv("FRIDAY_CAMERA_INTERFACE");
    if (fd >= 0 && name && (type & 0xff) == SOCK_DGRAM) {
        unsigned int index = if_nametoindex(name);
        if (!index) { close(fd); return -1; }
        int result = 0;
        if (domain == AF_INET) result = setsockopt(fd, IPPROTO_IP, IP_BOUND_IF, &index, sizeof(index));
        if (domain == AF_INET6) result = setsockopt(fd, IPPROTO_IPV6, IPV6_BOUND_IF, &index, sizeof(index));
        if (result) { close(fd); return -1; }
    }
    return fd;
}
__attribute__((used)) static struct { const void *replacement; const void *replacee; }
interpose_camera_socket __attribute__((section("__DATA,__interpose"))) = {
    (void *)(uintptr_t)&camera_socket, (void *)(uintptr_t)&socket
};
