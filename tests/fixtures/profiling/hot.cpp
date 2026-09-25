// Seeded hot loop for the real-process profiling tests.
// integrate_physics is the dominant self-time function by construction.
#include <cstdio>
#include <cstdlib>

__attribute__((noinline)) double integrate_physics(int n) {
  double acc = 0.0;
  for (int i = 0; i < n; ++i) acc += (i % 7) * 0.5 + acc * 1e-9;
  return acc;
}

__attribute__((noinline)) double light_work(int n) {
  double acc = 0.0;
  for (int i = 0; i < n; ++i) acc += i;
  return acc;
}

__attribute__((noinline)) int fib(int n) { return n < 2 ? n : fib(n - 1) + fib(n - 2); }

int main(int argc, char** argv) {
  int scale = argc > 1 ? atoi(argv[1]) : 2000000;
  double t = 0;
  for (int f = 0; f < 10; ++f) {
    t += integrate_physics(scale);
    t += light_work(scale / 10);
  }
  t += fib(18);
  printf("%f\n", t);
  return 0;
}
