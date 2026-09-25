// Seeded leak and allocator hotspot for the real heaptrack test.
// leak_buffer leaks 10 x 4096 bytes; churn makes 2000 temporary allocations.
#include <cstdio>
#include <cstdlib>
#include <vector>

__attribute__((noinline)) void* leak_buffer(size_t n) { return malloc(n); }

__attribute__((noinline)) void churn() {
  for (int i = 0; i < 2000; ++i) {
    std::vector<int> v(64);
    v[0] = i;
    asm volatile("" : : "r"(v.data()) : "memory");
  }
}

int main() {
  void* keep = nullptr;
  for (int i = 0; i < 10; ++i) keep = leak_buffer(4096);
  churn();
  printf("%p\n", keep);
  return 0;
}
