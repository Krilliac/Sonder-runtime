#pragma once
#include "math.h"

class Entity {
public:
    Vec3 position{0.0f, 0.0f, 0.0f};
    void move(const Vec3& delta);
    void tick(float dt);
};
