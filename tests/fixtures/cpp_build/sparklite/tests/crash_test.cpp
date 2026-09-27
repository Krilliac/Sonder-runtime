#include <cstdio>

int main() {
    int* volatile pointer = nullptr;
    std::printf("about to crash\n");
    return *pointer;
}
