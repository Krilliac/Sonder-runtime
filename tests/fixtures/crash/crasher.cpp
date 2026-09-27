// Tiny crasher for the pure-reader and host-debugger tests (lane A).
// Modes: null (SIGSEGV at 0x0), uaf / overflow / ubsan / leak (sanitizer
// builds), abort, hang (sleeps so a core can be taken without a fault).
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <unistd.h>

struct Renderer {
    int frame_count;
    virtual ~Renderer() {}
    virtual void Submit(int value);
};

void Renderer::Submit(int value) {
    frame_count += value;  // CRASH_LINE_NULL
}

__attribute__((noinline)) void run_frame(Renderer* renderer, int value) {
    renderer->Submit(value);  // CRASH_LINE_CALL
    std::printf("submitted %d\n", renderer->frame_count);
}

__attribute__((noinline)) int use_after_free() {
    int* data = new int[4];
    data[0] = 7;
    delete[] data;
    return data[1];  // CRASH_LINE_UAF
}

__attribute__((noinline)) int heap_overflow(int index) {
    int* data = new int[4];
    int value = data[index];  // CRASH_LINE_OVERFLOW
    delete[] data;
    return value;
}

__attribute__((noinline)) int int_overflow(int base) {
    int big = 2147483647;
    return big + base;  // CRASH_LINE_UBSAN
}

int main(int argc, char** argv) {
    const char* mode = argc > 1 ? argv[1] : "null";
    if (std::strcmp(mode, "null") == 0) {
        Renderer* renderer = nullptr;
        run_frame(renderer, argc);
    } else if (std::strcmp(mode, "uaf") == 0) {
        return use_after_free();
    } else if (std::strcmp(mode, "overflow") == 0) {
        return heap_overflow(argc + 3);
    } else if (std::strcmp(mode, "ubsan") == 0) {
        return int_overflow(argc) > 0;
    } else if (std::strcmp(mode, "abort") == 0) {
        std::abort();
    } else if (std::strcmp(mode, "leak") == 0) {
        int* leak = static_cast<int*>(std::malloc(64));
        leak[0] = argc;
        leak = nullptr;
        return 0;
    } else if (std::strcmp(mode, "hang") == 0) {
        sleep(30);
    }
    return 0;
}
