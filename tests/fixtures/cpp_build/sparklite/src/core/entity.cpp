#include "entity.h"

void Entity::move(const Vec3& delta) {
    position.x += delta.x;
    position.y += delta.y;
    position.z += delta.z;
}
