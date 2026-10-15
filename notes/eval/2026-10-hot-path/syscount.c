// DYLD interposer counting socket/event syscalls (macOS). Load with
// DYLD_INSERT_LIBRARIES=./libsyscount.dylib; read counters with syscount_get(i).
#include <stdint.h>
#include <sys/event.h>
#include <sys/socket.h>
#include <sys/uio.h>
#include <unistd.h>

#define DYLD_INTERPOSE(_replacement, _replacee)                                     \
    __attribute__((used)) static struct {                                           \
        const void *replacement;                                                    \
        const void *replacee;                                                       \
    } _interpose_##_replacee __attribute__((section("__DATA,__interpose"))) = {     \
        (const void *)(unsigned long)&_replacement, (const void *)(unsigned long)&_replacee};

enum { C_READ, C_RECV, C_RECVFROM, C_WRITE, C_SEND, C_SENDTO, C_WRITEV, C_KEVENT, C_KEVENT64, C_N };
static uint64_t counts[C_N];

uint64_t syscount_get(int i) { return (i >= 0 && i < C_N) ? counts[i] : 0; }

static ssize_t my_read(int fd, void *buf, size_t n) { counts[C_READ]++; return read(fd, buf, n); }
static ssize_t my_recv(int fd, void *buf, size_t n, int f) { counts[C_RECV]++; return recv(fd, buf, n, f); }
static ssize_t my_recvfrom(int fd, void *buf, size_t n, int f, struct sockaddr *a, socklen_t *l) {
    counts[C_RECVFROM]++; return recvfrom(fd, buf, n, f, a, l);
}
static ssize_t my_write(int fd, const void *buf, size_t n) { counts[C_WRITE]++; return write(fd, buf, n); }
static ssize_t my_send(int fd, const void *buf, size_t n, int f) { counts[C_SEND]++; return send(fd, buf, n, f); }
static ssize_t my_sendto(int fd, const void *buf, size_t n, int f, const struct sockaddr *a, socklen_t l) {
    counts[C_SENDTO]++; return sendto(fd, buf, n, f, a, l);
}
static ssize_t my_writev(int fd, const struct iovec *v, int c) { counts[C_WRITEV]++; return writev(fd, v, c); }
static int my_kevent(int kq, const struct kevent *cl, int nc, struct kevent *el, int ne, const struct timespec *t) {
    counts[C_KEVENT]++; return kevent(kq, cl, nc, el, ne, t);
}
static int my_kevent64(int kq, const struct kevent64_s *cl, int nc, struct kevent64_s *el, int ne, unsigned int fl, const struct timespec *t) {
    counts[C_KEVENT64]++; return kevent64(kq, cl, nc, el, ne, fl, t);
}

DYLD_INTERPOSE(my_read, read)
DYLD_INTERPOSE(my_recv, recv)
DYLD_INTERPOSE(my_recvfrom, recvfrom)
DYLD_INTERPOSE(my_write, write)
DYLD_INTERPOSE(my_send, send)
DYLD_INTERPOSE(my_sendto, sendto)
DYLD_INTERPOSE(my_writev, writev)
DYLD_INTERPOSE(my_kevent, kevent)
DYLD_INTERPOSE(my_kevent64, kevent64)
