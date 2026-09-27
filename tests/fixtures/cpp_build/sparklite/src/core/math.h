#pragma once
#include <cstddef>

struct Vec3 {
    float x;
    float y;
    float z;
};

float length(const Vec3& v);
float dot(const Vec3& a, const Vec3& b);
float normalized_length(const Vec3& v);
