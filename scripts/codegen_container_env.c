/* Minimal env -i implementation for the hermetic CI scratch image.
 * Production images may use their distribution's /usr/bin/env. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

int main(int argc, char **argv) {
    if (argc < 5 || strcmp(argv[1], "-i") != 0) return 125;
    clearenv();
    int position = 2;
    while (position < argc && strcmp(argv[position], "--") != 0) {
        char *separator = strchr(argv[position], '=');
        if (separator == NULL || separator == argv[position]) return 125;
        *separator = '\0';
        if (setenv(argv[position], separator + 1, 1) != 0) return 125;
        position++;
    }
    if (++position >= argc) return 125;
    execvp(argv[position], argv + position);
    perror("container env exec");
    return 125;
}
