"""Mine a codebase into a LOCAL-ONLY personal training dataset.

Domain-agnostic: reads git history (commit-message rationale) and
documented functions/methods (doc-comment + signature + body) out of a
project and turns them into chat-format ({"messages": [...]}) JSONL
examples that teach a local model the project's private APIs, patterns,
and history.

CRITICAL: the output is built from PRIVATE code. It is local-only —
never commit it, never push it to a public repo, never send it to a
cloud-tier model. `main()` prints a loud reminder every run, and the
default output filename is expected to be gitignored by the caller's repo.

Usage:
    ./venv/Scripts/python.exe build_personal_dataset.py <root> [--lang auto|cpp|cs|py]
        [--max-commits 1500] [--max-files 400] [--out personal_dataset.jsonl]
        [--project NAME]
"""
import argparse
import json
import os
import re
import subprocess
import sys

RS = "\x1e"  # git log record separator
FS = "\x1f"  # git log field separator
GIT_LOG_FORMAT = "%H" + FS + "%s" + FS + "%b" + RS

SKIP_DIRS = {".git", "build", "bin", "obj", "node_modules", ".vs"}

LANGS = {
    "cpp": {
        "exts": (".cpp", ".cc", ".cxx", ".h", ".hpp", ".hxx"),
        "doc_style": "c",  # /** */ or // or ///
    },
    "cs": {
        "exts": (".cs",),
        "doc_style": "xml",  # /// <summary> or /** */
    },
    "py": {
        "exts": (".py",),
        "doc_style": "docstring",  # """ ... """ as first statement
    },
}

# Matches a doc-comment (either /** ... */ or one-or-more //(/) lines)
# immediately followed by a C-family function signature ending in '{'.
# Used for both cpp and cs — both are brace-delimited with C-style comments.
_C_FAMILY_UNIT_RE = re.compile(
    r"(?P<doc>/\*\*.*?\*/|(?:^[ \t]*//[^\n]*\n)+)"
    r"[ \t\n]*"
    r"(?P<rettype>[A-Za-z_][\w:<>,&*\[\]\.\? \t]*?)\s*"
    r"(?P<name>(?:[A-Za-z_]\w*::)?~?[A-Za-z_]\w*)"
    r"\s*\((?P<args>[^;{}]*)\)"
    r"[ \t]*(?:const)?[ \t]*(?:override)?[ \t]*"
    r"\{",
    re.DOTALL | re.MULTILINE,
)

_PY_DEF_RE = re.compile(r"^(?P<indent>[ \t]*)def\s+(?P<name>[A-Za-z_]\w*)\s*(?P<sig>\(.*\))\s*:\s*$")


def parse_git_log(log_text):
    """Parse `git log --format=%H%x1f%s%x1f%b%x1e` output into dicts."""
    commits = []
    if not log_text:
        return commits
    for rec in log_text.split(RS):
        rec = rec.strip("\n")
        if not rec.strip():
            continue
        parts = rec.split(FS, 2)
        if not parts or not parts[0].strip():
            continue
        h = parts[0].strip()
        subject = parts[1].strip() if len(parts) > 1 else ""
        body = parts[2].strip() if len(parts) > 2 else ""
        commits.append({"hash": h, "subject": subject, "body": body})
    return commits


def commit_pairs(commits, project):
    """Turn commits with a non-trivial body into rationale training pairs."""
    pairs = []
    for c in commits:
        body = (c.get("body") or "").strip()
        if len(body) < 30:
            continue
        subject = (c.get("subject") or "").strip()
        user = 'In the %s project, why was this change made: "%s"?' % (project, subject)
        pairs.append({"messages": [
            {"role": "user", "content": user},
            {"role": "assistant", "content": body},
        ]})
    return pairs


def _scan_balanced(source, open_pos):
    """Return the index of the '}' matching the '{' at open_pos, or -1."""
    depth = 0
    i = open_pos
    n = len(source)
    while i < n:
        ch = source[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _clean_c_family_doc(doc_raw):
    text = doc_raw.strip("\n").strip()
    if text.startswith("/**") or text.startswith("/*"):
        t = text[2:] if not text.startswith("/**") else text[3:]
        if t.endswith("*/"):
            t = t[:-2]
        lines = [re.sub(r"^[ \t]*\*[ \t]?", "", l) for l in t.splitlines()]
        return "\n".join(l.strip() for l in lines).strip()
    lines = []
    for l in text.splitlines():
        l2 = re.sub(r"^[ \t]*//+[ \t]?", "", l)
        lines.append(l2.strip())
    return "\n".join(lines).strip()


def _extract_c_family(source):
    units = []
    for m in _C_FAMILY_UNIT_RE.finditer(source):
        open_pos = m.end() - 1  # position of the '{' just consumed
        close_pos = _scan_balanced(source, open_pos)
        if close_pos == -1:
            continue
        name = m.group("name")
        rettype = re.sub(r"\s+", " ", m.group("rettype")).strip()
        args = re.sub(r"\s+", " ", m.group("args")).strip()
        signature = ("%s %s(%s)" % (rettype, name, args)).strip()
        signature = re.sub(r"\s+", " ", signature)
        doc = _clean_c_family_doc(m.group("doc"))
        sig_start = m.start("rettype")
        body = source[sig_start:close_pos + 1]
        units.append({"name": name, "signature": signature, "doc": doc, "body": body})
    return units


def _extract_py(source):
    lines = source.split("\n")
    n = len(lines)
    units = []
    i = 0
    while i < n:
        m = _PY_DEF_RE.match(lines[i])
        if not m:
            i += 1
            continue
        indent = m.group("indent")
        name = m.group("name")
        sig = m.group("sig")
        def_line_idx = i
        j = i + 1
        if j >= n:
            i += 1
            continue
        dline = lines[j]
        dstripped = dline.strip()
        if dstripped[:3] in ('"""', "'''"):
            quote = dstripped[:3]
            rest = dstripped[3:]
            if rest.endswith(quote) and len(dstripped) >= 6:
                doc = rest[:-3].strip()
                body_start_idx = j + 1
            else:
                doc_lines = [rest]
                k = j + 1
                while k < n:
                    if quote in lines[k]:
                        doc_lines.append(lines[k].split(quote)[0])
                        k += 1
                        break
                    doc_lines.append(lines[k])
                    k += 1
                doc = "\n".join(l.strip() for l in doc_lines).strip()
                body_start_idx = k
        else:
            i += 1
            continue

        end_idx = body_start_idx
        def_indent_len = len(indent)
        while end_idx < n:
            line = lines[end_idx]
            if line.strip() == "":
                end_idx += 1
                continue
            cur_indent = len(line) - len(line.lstrip(" \t"))
            if cur_indent > def_indent_len:
                end_idx += 1
            else:
                break
        body = "\n".join(lines[def_line_idx:end_idx]).rstrip("\n")
        signature = "def %s%s:" % (name, sig)
        units.append({"name": name, "signature": signature, "doc": doc, "body": body})
        i = max(end_idx, def_line_idx + 1)
    return units


def extract_units(source, lang):
    """Extract documented functions/methods from source text for `lang`."""
    if lang in ("cpp", "cs"):
        return _extract_c_family(source)
    if lang == "py":
        return _extract_py(source)
    return []


def doc_pairs(units, project, lang):
    """Build 'what does X do' training pairs from documented units."""
    pairs = []
    for u in units:
        doc = (u.get("doc") or "").strip()
        if not doc:
            continue
        sig = u.get("signature") or u.get("name")
        user = "In %s, what does `%s` do?" % (project, sig)
        pairs.append({"messages": [
            {"role": "user", "content": user},
            {"role": "assistant", "content": doc},
        ]})
    return pairs


def impl_pairs(units, project, lang):
    """Build 'show the implementation' training pairs from documented units."""
    pairs = []
    for u in units:
        body = (u.get("body") or "").strip()
        if not body:
            continue
        name = u.get("name")
        user = "Show the implementation of `%s` from the %s project." % (name, project)
        assistant = "```%s\n%s\n```" % (lang, body)
        pairs.append({"messages": [
            {"role": "user", "content": user},
            {"role": "assistant", "content": assistant},
        ]})
    return pairs


def iter_source_files(root, lang, max_files=400):
    """Stream up to max_files source file paths under root matching lang's extensions."""
    exts = LANGS.get(lang, {}).get("exts", ())
    if not exts:
        return
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if count >= max_files:
                return
            if fn.lower().endswith(exts):
                yield os.path.join(dirpath, fn)
                count += 1


def _detect_lang(root, sample_limit=300):
    counts = {lang: 0 for lang in LANGS}
    n = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            low = fn.lower()
            for lang, info in LANGS.items():
                if low.endswith(info["exts"]):
                    counts[lang] += 1
                    n += 1
                    break
            if n >= sample_limit:
                break
        if n >= sample_limit:
            break
    if not any(counts.values()):
        return "cpp"
    return max(counts, key=counts.get)


def _run_git_log(root, max_commits):
    try:
        proc = subprocess.run(
            ["git", "-C", root, "log", "--no-merges", "-n", str(max_commits),
             "--format=" + GIT_LOG_FORMAT],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if proc.returncode != 0:
            return ""
        return proc.stdout
    except Exception:
        return ""


def main(root, project=None, lang="auto", max_commits=1500, max_files=400,
         out="personal_dataset.jsonl"):
    root = os.path.abspath(root)
    project = project or os.path.basename(root.rstrip("/\\")) or root

    log_text = _run_git_log(root, max_commits)
    commits = parse_git_log(log_text)
    c_pairs = commit_pairs(commits, project)

    if lang == "auto":
        lang = _detect_lang(root)

    d_pairs = []
    i_pairs = []
    for path in iter_source_files(root, lang, max_files):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                source = f.read()
        except OSError:
            continue
        units = extract_units(source, lang)
        d_pairs.extend(doc_pairs(units, project, lang))
        i_pairs.extend(impl_pairs(units, project, lang))

    seen = set()
    deduped = []
    for p in c_pairs + d_pairs + i_pairs:
        key = p["messages"][0]["content"]
        if key in seen:
            continue
        seen.add(key)
        deduped.append(p)

    with open(out, "w", encoding="utf-8") as f:
        for p in deduped:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")

    print("commits: %d  doc: %d  impl: %d  total(deduped): %d written to %s"
          % (len(c_pairs), len(d_pairs), len(i_pairs), len(deduped), out))
    for sample in deduped[:2]:
        print(json.dumps(sample, ensure_ascii=False))
    print("=" * 70)
    print("LOCAL-ONLY DATA: %r is mined from PRIVATE source code." % out)
    print("Do NOT commit it. Do NOT push it. Do NOT send it to a cloud-tier model.")
    print("Keep it on local training tiers only.")
    print("=" * 70)
    return deduped


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Mine a codebase into a local-only personal training dataset.")
    ap.add_argument("root", help="path to the codebase root")
    ap.add_argument("--lang", default="auto", choices=["auto", "cpp", "cs", "py"])
    ap.add_argument("--max-commits", type=int, default=1500)
    ap.add_argument("--max-files", type=int, default=400)
    ap.add_argument("--out", default="personal_dataset.jsonl")
    ap.add_argument("--project", default=None)
    args = ap.parse_args()
    main(args.root, project=args.project, lang=args.lang,
         max_commits=args.max_commits, max_files=args.max_files, out=args.out)
