#!/bin/bash
set -e
cd "$(dirname "$0")"
SRC="$(cd "$(dirname "$0")" && pwd)/spark_tiny.c"
for variant in a b; do
  bias=1; [ "$variant" = b ] && bias=7
  ( cd $variant
    clang-cl-18 --target=x86_64-pc-windows-msvc /Z7 /O1 /GS- /c -DHELPER_BIAS=$bias "/clang:-fdebug-prefix-map=$(dirname $SRC)=C:\agent\_work\3\s\src" "/clang:-fdebug-compilation-dir=C:\agent\_work\3\s" /clang:-gno-codeview-command-line /Fospark_tiny.obj -- "$SRC"
    lld-link-18 /debug /entry:main /subsystem:console /nodefaultlib spark_tiny.obj /out:spark_tiny.exe /pdb:spark_tiny.pdb "/pdbsourcepath:C:\agent\_work\3\s" "/pdbaltpath:C:\build\out\spark_tiny.pdb"
    llvm-pdbutil-18 dump -summary spark_tiny.pdb > pdbutil_summary.txt
    llvm-readobj-18 --coff-debug-directory spark_tiny.exe > readobj_debug.txt )
done
ls -la a b
