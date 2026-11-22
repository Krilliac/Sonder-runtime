"""Native regression for the launcher's checked-source handoff."""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux openat launcher")


def test_launcher_copies_the_opened_file_when_its_name_is_replaced(tmp_path):
    compiler = shutil.which("gcc")
    if compiler is None:
        pytest.skip("gcc unavailable")

    source = tmp_path / "source"
    source.mkdir()
    destination = tmp_path / "destination"
    destination.mkdir()
    (source / "input.c").write_text("granted source\n")
    secret = tmp_path / "private.key"
    secret.write_text("ungranted bytes\n")

    # The two wrappers make the same replacement immediately after either
    # the old pathname check or the new descriptor open, respectively.
    harness = tmp_path / "launcher_harness.c"
    harness.write_text(r'''
#include <fcntl.h>
#include <dirent.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

static const char *source_path, *saved_path, *secret_path;
static int swapped;
static int listed;

static struct dirent *one_entry(DIR *directory) {
    if (listed) return NULL;
    struct dirent *entry;
    do {
        entry = readdir(directory);
    } while (entry && (!strcmp(entry->d_name, ".") || !strcmp(entry->d_name, "..")));
    if (entry) listed = 1;
    return entry;
}

static int replace_name(void) {
    if (rename(source_path, saved_path) != 0) return -1;
    if (link(secret_path, source_path) != 0) return -1;
    swapped = 1;
    return 0;
}

static __attribute__((unused)) int checked_lstat(const char *path, struct stat *info) {
    int result = fstatat(AT_FDCWD, path, info, AT_SYMLINK_NOFOLLOW);
    if (result == 0 && strcmp(path, source_path) == 0 && replace_name() != 0) return -1;
    return result;
}

static __attribute__((unused)) int checked_openat(int dir, const char *name, int flags) {
    int fd = openat(dir, name, flags);
    if (fd >= 0 && strcmp(name, "input.c") == 0 && replace_name() != 0) {
        close(fd);
        return -1;
    }
    return fd;
}

#define lstat checked_lstat
#define openat checked_openat
#define readdir one_entry
#define main launcher_main
#include "scripts/codegen_container_launch.c"
#undef main
#undef readdir
#undef openat
#undef lstat

int main(int argc, char **argv) {
    if (argc != 6) return 2;
    source_path = argv[3];
    saved_path = argv[4];
    secret_path = argv[5];
    int result = copy_tree(argv[1], argv[2], 0);
    printf("swapped=%d\n", swapped);
    return result == 0 ? 0 : 1;
}
''')
    root = Path(__file__).resolve().parents[1]
    binary = tmp_path / "launcher_harness"
    compile_result = subprocess.run(
        [compiler, "-O0", "-Wall", "-Wextra", "-Werror", "-I", str(root),
         str(harness), "-o", str(binary)],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert compile_result.returncode == 0, compile_result.stderr
    run = subprocess.run(
        [str(binary), str(source), str(destination), str(source / "input.c"),
         str(tmp_path / "moved.c"), str(secret)],
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout == "swapped=1\n"
    assert (destination / "input.c").read_text() == "granted source\n"
