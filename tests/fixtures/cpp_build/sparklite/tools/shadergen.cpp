// Build-time code generator: reads a shader file and emits a header.
#include <fstream>
#include <iostream>
#include <string>

int main(int argc, char** argv) {
    if (argc != 3) {
        std::cerr << "usage: shadergen <input> <output>\n";
        return 2;
    }
    std::ifstream in(argv[1]);
    std::string name;
    std::getline(in, name);
    std::ofstream out(argv[2]);
    out << "#pragma once\n";
    out << "static const char* const kBasicShaderName = \"" << name << "\";\n";
    return out ? 0 : 1;
}
