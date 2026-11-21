/* Fixed Codegen image launcher: copy only the mounted snapshot to capped tmpfs.
 * Compile statically for a FROM scratch fixture, or include it in a reviewed
 * production image alongside its language toolchain. No host bind is writable.
 */
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

/* Takes ownership of source_fd, including on failure. */
static int copy_tree_at(int source_fd, const char *source, const char *destination, int depth) {
    if (source_fd < 0) return -1;
    if (depth > 32) { close(source_fd); return -1; }
    DIR *directory = fdopendir(source_fd);
    if (!directory) { close(source_fd); return -1; }
    struct dirent *entry;
    while ((entry = readdir(directory)) != NULL) {
        if (!strcmp(entry->d_name, ".") || !strcmp(entry->d_name, "..")) continue;
        char from[4096], to[4096];
        int src_length = snprintf(from, sizeof from, "%s/%s", source, entry->d_name);
        int dst_length = snprintf(to, sizeof to, "%s/%s", destination, entry->d_name);
        if (src_length < 0 || dst_length < 0 || src_length >= (int)sizeof from
            || dst_length >= (int)sizeof to) { closedir(directory); return -1; }
        int entry_fd = openat(dirfd(directory), entry->d_name,
                              O_RDONLY | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK);
        struct stat info;
        if (entry_fd < 0) { closedir(directory); return -1; }
        if (fstat(entry_fd, &info) != 0) {
            close(entry_fd); closedir(directory); return -1;
        }
        if (S_ISDIR(info.st_mode)) {
            if (mkdir(to, 0700) != 0) {
                close(entry_fd); closedir(directory); return -1;
            }
            if (copy_tree_at(entry_fd, from, to, depth + 1) != 0) {
                closedir(directory); return -1;
            }
        } else if (S_ISREG(info.st_mode) && info.st_nlink == 1) {
            int dest_fd = open(to, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0600);
            if (dest_fd < 0) {
                close(entry_fd);
                closedir(directory); return -1;
            }
            char buffer[16384];
            ssize_t amount;
            while ((amount = read(entry_fd, buffer, sizeof buffer)) > 0) {
                ssize_t sent = 0;
                while (sent < amount) {
                    ssize_t n = write(dest_fd, buffer + sent, (size_t)(amount - sent));
                    if (n <= 0) { close(entry_fd); close(dest_fd); closedir(directory); return -1; }
                    sent += n;
                }
            }
            int source_close = close(entry_fd);
            int dest_close = close(dest_fd);
            if (amount < 0 || source_close != 0 || dest_close != 0) {
                closedir(directory); return -1;
            }
        } else { close(entry_fd); closedir(directory); return -1; }
    }
    return closedir(directory);
}

static int copy_tree(const char *source, const char *destination, int depth) {
    int source_fd = open(source, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    return copy_tree_at(source_fd, source, destination, depth);
}

int main(int argc, char **argv) {
    if (argc < 2) return 125;
    if (mkdir("/build/home", 0700) != 0 || mkdir("/build/tmp", 0700) != 0
        || mkdir("/build/project", 0700) != 0
        || copy_tree("/workspace", "/build/project", 0) != 0
        || chdir("/build/project") != 0) {
        perror("codegen source staging");
        return 125;
    }
    execvp(argv[1], argv + 1);
    perror("codegen build exec");
    return 125;
}
