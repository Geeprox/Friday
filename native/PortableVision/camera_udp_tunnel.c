/* Friday 摄像头子进程专用 UDP 适配。
 * 只转发登记摄像头与 CS2 发现包；宿主转发器按凭据和目标双重检查。
 * glibc 的 fortify 收包入口也必须解封装，否则 SDK 会把封装头当协议头。
 * 不解析媒体、云台命令或账号，不修改第三方 SDK 二进制。
 */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <dlfcn.h>
#include <errno.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>

/* pthread_once 避免 SDK 多线程同时解析 libc 符号的数据竞争。 */
static pthread_once_t symbols_once = PTHREAD_ONCE_INIT;
static ssize_t (*real_sendto)(int, const void *, size_t, int,
                              const struct sockaddr *, socklen_t);
static ssize_t (*real_recvfrom)(int, void *, size_t, int, struct sockaddr *,
                                socklen_t *);
static int (*real_close)(int);
static ssize_t (*real_recv)(int, void *, size_t, int);
static int (*real_recvmmsg)(int, struct mmsghdr *, unsigned, int,
                            struct timespec *);
static void load_symbols(void) {
  real_sendto = dlsym(RTLD_NEXT, "sendto");
  real_recvfrom = dlsym(RTLD_NEXT, "recvfrom");
  real_close = dlsym(RTLD_NEXT, "close");
  real_recv = dlsym(RTLD_NEXT, "recv");
  real_recvmmsg = dlsym(RTLD_NEXT, "recvmmsg");
}

static struct sockaddr_in relay;
static struct in_addr camera;
static unsigned char token[32];
static int enabled = 0;
static _Atomic unsigned char tracked[65536];
__attribute__((constructor)) static void init(void) {
  const char *r = getenv("FRIDAY_RELAY_IP"),
             *c = getenv("FRIDAY_RELAY_CAMERA_IP"),
             *t = getenv("FRIDAY_RELAY_TOKEN"),
             *p = getenv("FRIDAY_RELAY_PORT");
  if (!r || !c || !t || strlen(t) != 64 || !p)
    return;
  char *end = 0;
  long port = strtol(p, &end, 10);
  if (*end || port < 1024 || port > 65535)
    return;
  relay.sin_family = AF_INET;
  relay.sin_port = htons((unsigned short)port);
  if (inet_pton(AF_INET, r, &relay.sin_addr) != 1 ||
      inet_pton(AF_INET, c, &camera) != 1)
    return;
  for (int i = 0; i < 32; i++) {
    char b[3] = {t[2 * i], t[2 * i + 1], 0};
    char *end = 0;
    long v = strtol(b, &end, 16);
    if (*end)
      return;
    token[i] = (unsigned char)v;
  }
  enabled = 1;
}
ssize_t sendto(int fd, const void *buf, size_t n, int flags,
               const struct sockaddr *a, socklen_t al) {
  pthread_once(&symbols_once, load_symbols);
  if (enabled && fd >= 0 && fd < 65536 && a &&
      al >= sizeof(struct sockaddr_in) && a->sa_family == AF_INET) {
    const struct sockaddr_in *dest = (const void *)a;
    unsigned port = ntohs(dest->sin_port);
    if (dest->sin_addr.s_addr == camera.s_addr ||
        (port == 32108 && n == 4 && !memcmp(buf, "\xf1\x30\0\0", 4))) {
      if (n > 65465) {
        errno = EMSGSIZE;
        return -1;
      }
      unsigned char packet[65507];
      memcpy(packet, "FRU1", 4);
      memcpy(packet + 4, token, 32);
      memcpy(packet + 36, &camera.s_addr, 4);
      memcpy(packet + 40, &dest->sin_port, 2);
      memcpy(packet + 42, buf, n);
      tracked[fd] = 1;
      ssize_t r =
          real_sendto(fd, packet, n + 42, flags, (void *)&relay, sizeof(relay));
      return r < 0 ? r : r - 42;
    }
  }
  return real_sendto(fd, buf, n, flags, a, al);
}
ssize_t recvfrom(int fd, void *buf, size_t n, int flags, struct sockaddr *a,
                 socklen_t *al) {
  pthread_once(&symbols_once, load_symbols);
  if (enabled && fd >= 0 && fd < 65536 && tracked[fd]) {
    unsigned char packet[65535];
    struct sockaddr_in peer;
    socklen_t pl = sizeof(peer);
    ssize_t r =
        real_recvfrom(fd, packet, sizeof(packet), flags, (void *)&peer, &pl);
    if (r < 0)
      return r;
    if (r < 42 || memcmp(packet, "FRU1", 4) || memcmp(packet + 4, token, 32) ||
        peer.sin_addr.s_addr != relay.sin_addr.s_addr ||
        peer.sin_port != relay.sin_port ||
        memcmp(packet + 36, &camera.s_addr, 4)) {
      errno = EAGAIN;
      return -1;
    }
    struct sockaddr_in source = {.sin_family = AF_INET};
    memcpy(&source.sin_addr.s_addr, packet + 36, 4);
    memcpy(&source.sin_port, packet + 40, 2);
    if (a && al) {
      size_t size = *al < sizeof(source) ? *al : sizeof(source);
      memcpy(a, &source, size);
      *al = sizeof(source);
    }
    size_t copied = (size_t)(r - 42) < n ? (size_t)(r - 42) : n;
    memcpy(buf, packet + 42, copied);
    return copied;
  }
  return real_recvfrom(fd, buf, n, flags, a, al);
}
int close(int fd) {
  pthread_once(&symbols_once, load_symbols);
  if (fd >= 0 && fd < 65536)
    tracked[fd] = 0;
  return real_close(fd);
}
ssize_t recv(int fd, void *buf, size_t n, int flags) {
  pthread_once(&symbols_once, load_symbols);
  if (enabled && fd >= 0 && fd < 65536 && tracked[fd])
    return recvfrom(fd, buf, n, flags, 0, 0);
  return real_recv(fd, buf, n, flags);
}
int recvmmsg(int fd, struct mmsghdr *msgs, unsigned count, int flags,
             struct timespec *timeout) {
  pthread_once(&symbols_once, load_symbols);
  if (!enabled || fd < 0 || fd >= 65536 || !tracked[fd])
    return real_recvmmsg(fd, msgs, count, flags, timeout);
  if (!count)
    return 0;
  unsigned cap = count > 32 ? 32 : count;
  struct mmsghdr *temp = calloc(cap, sizeof(*temp));
  struct iovec *iov = calloc(cap, sizeof(*iov));
  struct sockaddr_in *peers = calloc(cap, sizeof(*peers));
  unsigned char *data = malloc((size_t)cap * 65535);
  if (!temp || !iov || !peers || !data) {
    free(temp);
    free(iov);
    free(peers);
    free(data);
    errno = ENOMEM;
    return -1;
  }
  for (unsigned i = 0; i < cap; i++) {
    iov[i].iov_base = data + (size_t)i * 65535;
    iov[i].iov_len = 65535;
    temp[i].msg_hdr.msg_iov = &iov[i];
    temp[i].msg_hdr.msg_iovlen = 1;
    temp[i].msg_hdr.msg_name = &peers[i];
    temp[i].msg_hdr.msg_namelen = sizeof(peers[i]);
  }
  int r = real_recvmmsg(fd, temp, cap, flags, timeout);
  int saved = errno, valid = 0;
  for (int i = 0; i < r; i++) {
    unsigned char *p = data + (size_t)i * 65535;
    size_t length = temp[i].msg_len;
    if (length < 42 || memcmp(p, "FRU1", 4) || memcmp(p + 4, token, 32) ||
        peers[i].sin_addr.s_addr != relay.sin_addr.s_addr ||
        peers[i].sin_port != relay.sin_port ||
        memcmp(p + 36, &camera.s_addr, 4))
      continue;
    struct msghdr *out = &msgs[valid].msg_hdr;
    struct sockaddr_in source = {.sin_family = AF_INET};
    memcpy(&source.sin_addr.s_addr, p + 36, 4);
    memcpy(&source.sin_port, p + 40, 2);
    if (out->msg_name) {
      size_t size =
          out->msg_namelen < sizeof(source) ? out->msg_namelen : sizeof(source);
      memcpy(out->msg_name, &source, size);
      out->msg_namelen = sizeof(source);
    }
    size_t left = length - 42, offset = 42;
    for (size_t j = 0; j < out->msg_iovlen && left; j++) {
      size_t copy =
          left < out->msg_iov[j].iov_len ? left : out->msg_iov[j].iov_len;
      memcpy(out->msg_iov[j].iov_base, p + offset, copy);
      left -= copy;
      offset += copy;
    }
    out->msg_flags = left ? MSG_TRUNC : 0;
    msgs[valid].msg_len = offset - 42;
    valid++;
  }
  free(temp);
  free(iov);
  free(peers);
  free(data);
  if (r < 0) {
    errno = saved;
    return r;
  }
  if (!valid) {
    errno = EAGAIN;
    return -1;
  }
  return valid;
}
ssize_t __recvfrom_chk(int fd, void *buf, size_t n, size_t capacity, int flags,
                       struct sockaddr *a, socklen_t *al) {
  if (n > capacity) {
    errno = EMSGSIZE;
    return -1;
  }
  return recvfrom(fd, buf, n, flags, a, al);
}
ssize_t __recv_chk(int fd, void *buf, size_t n, size_t capacity, int flags) {
  if (n > capacity) {
    errno = EMSGSIZE;
    return -1;
  }
  return recv(fd, buf, n, flags);
}
