#include "entity.h"
#include "math.h"
#include "shaders.h"
#include <cstdio>

int main() {
    Entity player;
    player.move(Vec3{1.0f, 2.0f, 3.0f});
    player.tick(0.016f);
    std::printf("%s %f\n", kBasicShaderName, length(player.pos));
    return 0;
}
