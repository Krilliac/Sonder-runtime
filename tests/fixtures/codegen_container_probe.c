/* Native fixture compiled into a scratch image; print outcomes, never secrets. */
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

static int denied(const char *path) {
    int fd = open(path, O_RDONLY | O_NONBLOCK);
    if (fd >= 0) { close(fd); return 0; }
    return 1;
}

static int loopback_only_network_namespace(void) {
    FILE *interfaces = fopen("/proc/net/dev", "r");
    if (!interfaces) return 0;
    char line[512];
    int saw_loopback = 0, safe = 1;
    /* The first two lines are headers; every later line must name one link. */
    if (!fgets(line, sizeof line, interfaces) || !fgets(line, sizeof line, interfaces)) {
        fclose(interfaces);
        return 0;
    }
    while (fgets(line, sizeof line, interfaces)) {
        char *separator = strchr(line, ':');
        if (!separator) { safe = 0; break; }
        *separator = '\0';
        char *name = line;
        while (*name == ' ' || *name == '\t') ++name;
        char *end = separator;
        while (end > name && (end[-1] == ' ' || end[-1] == '\t')) --end;
        *end = '\0';
        if (!strcmp(name, "lo")) saw_loopback = 1;
        else safe = 0;
    }
    if (ferror(interfaces)) safe = 0;
    if (fclose(interfaces) != 0) safe = 0;
    return safe && saw_loopback;
}

int main(int argc, char **argv) {
    if (argc < 2) return 125;
    if (!strcmp(argv[1], "hang")) { for (;;) sleep(1); }
    if (!strcmp(argv[1], "staging") && argc >= 3) {
        int input = open(argv[2], O_RDONLY);
        if (input < 0) { puts("STAGING_SOURCE_MISSING"); return 1; }
        char source[1024] = {0};
        ssize_t count = read(input, source, sizeof source - 1);
        close(input);
        if (count < 0) return 125;
        if (strstr(source, "BROKEN")) { puts("STAGING_SOURCE_REJECTED"); return 1; }
        int output = open("probe.out", O_WRONLY | O_CREAT | O_TRUNC, 0600);
        if (output < 0 || write(output, "ok", 2) != 2) return 125;
        close(output);
        if (!denied("/workspace/probe.out")) return 125;
        puts("STAGING_TMPFS_OK");
        return 0;
    }
    if (!strcmp(argv[1], "attack") && argc >= 8) {
        int ok = 1;
        for (int index = 2; index < argc - 1; ++index) {
            int result = denied(argv[index]);
            printf("HOST_FILE_%d_%s\n", index, result ? "DENIED" : "EXPOSED");
            ok &= result;
        }
        int private_file = denied("/workspace/private.txt") && denied("private.txt");
        printf("UNDECLARED_%s\n", private_file ? "DENIED" : "EXPOSED");
        ok &= private_file;
        char process_path[64];
        snprintf(process_path, sizeof process_path, "/proc/%s/mem", argv[argc - 1]);
        int process_denied = denied(process_path);
        printf("HOST_PROCESS_%s\n", process_denied ? "DENIED" : "EXPOSED");
        ok &= process_denied;
        int writable = open("/workspace/main.c", O_WRONLY);
        if (writable >= 0) close(writable);
        printf("SOURCE_WRITE_%s\n", writable < 0 ? "DENIED" : "ALLOWED");
        ok &= writable < 0;
        printf("ENV_%s\n", getenv("SONDER_TEST_DUMMY_SECRET") ? "EXPOSED" : "DENIED");
        ok &= getenv("SONDER_TEST_DUMMY_SECRET") == NULL;
        int socket_exposed = !denied("/var/run/docker.sock") || !denied("/run/podman/podman.sock");
        printf("ENGINE_SOCKET_%s\n", socket_exposed ? "EXPOSED" : "DENIED");
        ok &= !socket_exposed;
        int unprivileged = getuid() == 65534 && getgid() == 65534;
        printf("USER_%s\n", unprivileged ? "UNPRIVILEGED" : "UNSAFE");
        ok &= unprivileged;
        FILE *status = fopen("/proc/self/status", "r");
        char line[256];
        int no_new_privileges = 0, no_caps = 0;
        if (status) {
            while (fgets(line, sizeof line, status)) {
                if (!strncmp(line, "NoNewPrivs:", 11)) no_new_privileges = strtoul(line + 11, NULL, 10) == 1;
                if (!strncmp(line, "CapEff:", 7)) no_caps = strtoull(line + 7, NULL, 16) == 0;
            }
            fclose(status);
        }
        printf("NO_NEW_PRIVILEGES_%s\n", no_new_privileges ? "YES" : "NO");
        printf("CAPABILITIES_%s\n", no_caps ? "NONE" : "UNSAFE");
        ok &= no_new_privileges && no_caps;
        int sock = socket(AF_INET, SOCK_STREAM, 0);
        struct sockaddr_in remote = {0};
        remote.sin_family = AF_INET;
        remote.sin_port = htons(80);
        inet_pton(AF_INET, "1.1.1.1", &remote.sin_addr);
        int connected = sock >= 0 && connect(sock, (struct sockaddr *)&remote, sizeof remote) == 0;
        if (sock >= 0) close(sock);
        printf("NETWORK_%s\n", connected ? "EXPOSED" : "DENIED");
        ok &= !connected;
        int only_loopback = loopback_only_network_namespace();
        printf("NETWORK_NAMESPACE_%s\n", only_loopback ? "LOOPBACK_ONLY" : "UNSAFE");
        ok &= only_loopback;
        return ok ? 0 : 1;
    }
    return 125;
}
