/* Tiny freestanding PE for the PE/PDB identity fixtures (lane A).
 * Built on Linux with clang-cl-18 + lld-link-18 (see README.md). */
__declspec(noinline) int helper(int value) {
    return value * 3 + HELPER_BIAS;
}

int main(void) {
    volatile int *pointer = 0;
    return helper(*pointer);
}
