// Cost of an EAGAIN recv vs a 200-byte send/recv on a loopback TCP pair.
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

static double now(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec + ts.tv_nsec * 1e-9;
}

int main(void) {
    int l = socket(AF_INET, SOCK_STREAM, 0);
    struct sockaddr_in a = {0};
    a.sin_family = AF_INET;
    a.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    a.sin_port = 0;
    bind(l, (struct sockaddr *)&a, sizeof a);
    listen(l, 1);
    socklen_t al = sizeof a;
    getsockname(l, (struct sockaddr *)&a, &al);
    int c = socket(AF_INET, SOCK_STREAM, 0);
    connect(c, (struct sockaddr *)&a, sizeof a);
    int s = accept(l, NULL, NULL);
    int one = 1;
    setsockopt(c, IPPROTO_TCP, TCP_NODELAY, &one, sizeof one);
    setsockopt(s, IPPROTO_TCP, TCP_NODELAY, &one, sizeof one);
    fcntl(c, F_SETFL, O_NONBLOCK);
    char buf[4096], msg[200];
    memset(msg, 'x', sizeof msg);
    int n = 200000;
    for (int round = 0; round < 3; round++) {
        double t0 = now();
        for (int i = 0; i < n; i++) {
            ssize_t r = recv(c, buf, sizeof buf, 0);
            if (r != -1 || errno != EAGAIN) abort();
        }
        double t1 = now();
        double send_t = 0, recv_t = 0;
        for (int i = 0; i < n / 4; i++) {
            double a0 = now();
            send(s, msg, sizeof msg, 0);
            double a1 = now();
            ssize_t r;
            while ((r = recv(c, buf, sizeof buf, 0)) < 0) {}
            double a2 = now();
            send_t += a1 - a0;
            recv_t += a2 - a1;
        }
        printf("EAGAIN recv: %.0f ns   send(200B): %.0f ns   recv(200B): %.0f ns\n",
               (t1 - t0) / n * 1e9, send_t / (n / 4) * 1e9, recv_t / (n / 4) * 1e9);
    }
    return 0;
}
