"""Extract the real public surface of generated C# files.

The first generation run failed mainly on cross-file drift: files disagreed
about each other's API even though a hand-written contract was in every prompt.
That contract described what the API was *supposed* to be. This extracts what it
*actually is*, so a regenerated file is told the truth about its dependencies.

Brace-depth scanning rather than a real parser, on purpose: the input is
frequently not compilable, and a parser would refuse it exactly when a signature
is most needed.
"""
from __future__ import annotations

import re

TYPE_RE = re.compile(
    r"^\s*(?:public\s+)(?:sealed\s+|static\s+|abstract\s+|partial\s+)*"
    r"(?P<kind>class|struct|enum|interface)\s+(?P<name>\w+)"
)
ENUM_BODY_RE = re.compile(r"\{(?P<body>[^}]*)\}", re.S)
SKIP = re.compile(r"^\s*(//|/\*|\*|#|\[)")


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _strip_comment(line: str) -> str:
    i = line.find("//")
    return line[:i] if i >= 0 else line


def extract_api(path: str, max_members: int = 80) -> str:
    """Return a compact declaration of every public type and member in a file."""
    try:
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
    except OSError:
        return ""

    lines = raw.split("\n")
    out: list[str] = []
    depth = 0          # brace depth overall
    type_depth = -1    # depth at which the current type's body lives
    in_type = False
    members = 0

    for idx, line in enumerate(lines):
        code = _strip_comment(line)
        stripped = code.strip()

        # A type declaration at any depth (nested types are rare here).
        t = TYPE_RE.match(code)
        if t and not in_type or (t and depth <= type_depth):
            kind, name = t.group("kind"), t.group("name")
            if kind == "enum":
                blob = "\n".join(lines[idx:idx + 8])
                m = ENUM_BODY_RE.search(blob)
                out.append(f"  public enum {name} {{ {_clean(m.group('body')) if m else ''} }}")
                # Enums contribute no members worth listing.
                depth += code.count("{") - code.count("}")
                continue
            out.append(f"  public {kind} {name}")
            in_type = True
            type_depth = depth
            members = 0
            depth += code.count("{") - code.count("}")
            continue

        open_before = depth
        depth += code.count("{") - code.count("}")

        # Only mine declarations sitting directly in a type body, never inside
        # a method body (which is one level deeper).
        if not in_type or open_before != type_depth + 1:
            continue
        if not stripped or SKIP.match(stripped) or members >= max_members:
            continue
        if not stripped.startswith("public"):
            continue

        # One line can carry several declarations: `public int A; public int B;`
        for part in stripped.split(";"):
            part = part.strip()
            if not part.startswith("public"):
                continue
            # Cut a body/initialiser/expression-body off the signature.
            for token in ("=>", "{", "="):
                if token in part:
                    part = part.split(token, 1)[0]
                    break
            sig = _clean(part)
            if not sig or sig == "public":
                continue
            is_method = "(" in sig
            # An auto-property looks like a field here; mark it, because the
            # difference decides whether a caller may assign to it.
            is_prop = not is_method and "{ get;" in stripped and sig in stripped
            out.append(f"      {sig}{'' if is_method else (' { get; }' if is_prop else ';')}")
            members += 1
            if members >= max_members:
                break

    return "\n".join(out)


def api_for(paths: dict[str, str]) -> str:
    """Build a dependency brief from {filename: path}."""
    chunks = []
    for name, path in paths.items():
        api = extract_api(path)
        if api.strip():
            chunks.append(f"  // --- {name} (ACTUAL generated API) ---\n{api}")
    return "\n\n".join(chunks)


if __name__ == "__main__":
    import sys
    for p in sys.argv[1:]:
        print(f"=== {p} ===")
        print(extract_api(p))
