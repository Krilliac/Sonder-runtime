/* Minimal env -i implementation for the hermetic CI scratch image.
 * Production images may use their distribution's /usr/bin/env.
 *
 * Contract (matches build_runtime_argv): env -i -- NAME=VALUE... COMMAND [ARG...]
 * One or more NAME=VALUE pairs with a non-empty name, then the first argument
 * without '=' is the command. Anything else fails closed with status 125. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static int refuse(const char *why) {
    fprintf(stderr, "container env: %s\n", why);
    return 125;
}

int main(int argc, char **argv) {
    if (argc < 5 || strcmp(argv[1], "-i") != 0 || strcmp(argv[2], "--") != 0)
        return refuse("expected: -i -- NAME=VALUE... COMMAND");
    clearenv();
    int position = 3;
    while (position < argc) {
        char *separator = strchr(argv[position], '=');
        if (separator == NULL) break;
        if (separator == argv[position]) return refuse("empty variable name");
        *separator = '\0';
        if (setenv(argv[position], separator + 1, 1) != 0) return refuse("setenv failed");
        position++;
    }
    if (position == 3) return refuse("no NAME=VALUE pair");
    if (position >= argc) return refuse("no command");
    execvp(argv[position], argv + position);
    perror("container env exec");
    return 125;
}
