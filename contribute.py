"""Export scrubbed, shareable lessons from memory.db to an outbox JSONL.

Contribution is strictly OPT-IN: nothing here uploads or opens a PR
automatically. It only writes a local file under contrib/ that YOU review,
then send home yourself (PR or file-server copy). Only distilled lesson
TEXT is considered for export — never raw interactions, code, or the model
itself. Lessons that look like they might leak private information (a
filesystem path, a secret-looking token, an email address) or that are not
a short generic sentence are excluded.

Run: ./venv/Scripts/python.exe contribute.py
"""
import io
import hashlib
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(__file__))
import memory_store  # noqa

MAX_LEN = 300

# Conservative shared rules for text that may leak private info. Each rule has
# a stable reason name and a replacement suitable for user-visible previews.
PRIVATE_RULES = [
    (
        "windows_path",
        re.compile(r"(?i)\b[A-Z]:[\\/][^\s\"']*"),
        "<windows-path>",
    ),
    (
        "unix_home_path",
        re.compile(r"(?<![\w/])/(?:home|Users)/[^\s\"']*"),
        "<home-path>",
    ),
    (
        "tilde_private_path",
        re.compile(
            r"(?<!\w)~[\\/](?:\.ssh|\.aws|\.config|\.kube)"
            r"(?:[\\/][^\s\"']*)?",
            re.I,
        ),
        "<private-home-path>",
    ),
    (
        "environment_home_path",
        re.compile(
            r"(?i)(?<!\w)(?:\$(?:HOME|USERPROFILE)|\$\{(?:HOME|USERPROFILE)\}|"
            r"%(?:HOME|USERPROFILE)%)[\\/][^\s\"']*"
        ),
        "<private-home-path>",
    ),
    (
        "file_uri",
        re.compile(
            r"(?i)\bfile:/{2,3}(?:[A-Z]:[\\/]|"
            r"(?:home|Users|root|etc|var|workspace|workspaces)/)[^\s\"']*"
        ),
        "<private-file-uri>",
    ),
    (
        "workspace_path",
        re.compile(r"(?<![\w/])/(?:workspace|workspaces)/[^\s\"']*", re.I),
        "<workspace-path>",
    ),
    (
        "relative_private_path",
        re.compile(
            r"(?i)(?<!\w)(?:(?:\.{0,2}[\\/])?(?:\.ssh|\.aws|\.kube)"
            r"(?:[\\/][^\s\"']*)?|(?:\.{0,2}[\\/])?\.config[\\/]"
            r"(?:gcloud|gh)(?:[\\/][^\s\"']*)?|(?:\.{0,2}[\\/])?"
            r"(?:secrets?|credentials?)[\\/][^\s\"']+)"
        ),
        "<private-relative-path>",
    ),
    (
        "unix_system_path",
        re.compile(
            r"(?<![\w/])/(?:root|etc/(?:ssh|ssl|pki)|var/(?:lib|log|run)|"
            r"opt|srv|mnt|media|tmp)(?:/[^\s\"']*)?",
            re.I,
        ),
        "<system-path>",
    ),
    (
        "unc_path",
        re.compile(r"\\\\[A-Za-z0-9._-]+\\[^\s\"']*"),
        "<unc-path>",
    ),
    (
        "email",
        re.compile(
            r"(?<![\w.+-])[\w.+-]{1,64}@[\w-]{1,63}"
            r"(?:\.[\w-]{1,63})+"
        ),
        "<email>",
    ),
    (
        "credential_assignment",
        re.compile(
            r"(?i)(?<![\w-])[\"']?(?:[a-z0-9]{1,24}[_-]){0,6}(?:api[_-]?key|secret|password|passwd|token|"
            r"access[_-]?key|aws[_-]?(?:access[_-]?key[_-]?id|secret[_-]?access[_-]?key)|"
            r"client[_-]?secret|private[_-]?token|auth[_-]?token|refresh[_-]?token|"
            r"session[_-]?(?:id|token)|sessionid)"
            r"[\"']?\s*[:=]\s*[\"']?[^\s,;\"']+"
        ),
        "<credential>",
    ),
    (
        "sensitive_header",
        re.compile(
            r"(?im)\b(?:x-api-key|x-auth-token|api-key|cookie|set-cookie)\b"
            r"\s*:\s*[^\r\n]+"
        ),
        "<sensitive-header>",
    ),
    (
        "authorization_header",
        re.compile(
            r"(?im)\b(?:proxy-)?authorization\b\s*:\s*[^\r\n]+"
        ),
        "<authorization>",
    ),
    (
        "known_credential",
        re.compile(
            r"(?<![A-Za-z0-9])(?:sk-(?:proj-)?[A-Za-z0-9_-]{12,}|"
            r"github_pat_[A-Za-z0-9_]{16,}|gh[pousr]_[A-Za-z0-9]{16,}|"
            r"glpat-[A-Za-z0-9_-]{12,}|hf_[A-Za-z0-9]{12,}|"
            r"npm_[A-Za-z0-9]{12,}|pypi-[A-Za-z0-9_-]{16,}|"
            r"ya29\.[A-Za-z0-9_-]{12,}|"
            r"AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}|"
            r"xox[baprs]-[A-Za-z0-9-]{10,})"
        ),
        "<known-credential>",
    ),
    (
        "url_credentials",
        re.compile(
            r"(?i)(?<![a-z0-9+.-])[a-z][a-z0-9+.-]{0,31}://"
            r"[^\s/@:]{1,128}:[^\s/@]{1,256}@"
        ),
        "<credential-url>",
    ),
    (
        "private_key",
        re.compile(
            r"-----BEGIN [A-Z0-9 ]{0,64}PRIVATE KEY-----"
        ),
        "<private-key>",
    ),
    (
        "long_hex",
        re.compile(r"\b[A-Fa-f0-9]{32,}\b"),
        "<opaque-hex>",
    ),
    (
        "long_base64",
        re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b"),
        "<opaque-token>",
    ),
    (
        "long_urlsafe_token",
        re.compile(
            r"(?<![A-Za-z0-9_-])(?=[A-Za-z0-9_-]{48,}(?![A-Za-z0-9_-]))"
            r"(?=[A-Za-z0-9_-]*[a-z])(?=[A-Za-z0-9_-]*[A-Z])"
            r"(?=[A-Za-z0-9_-]*[0-9])[A-Za-z0-9_-]+"
        ),
        "<opaque-token>",
    ),
    (
        "jwt",
        re.compile(
            r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,}\."
            r"[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
        ),
        "<jwt>",
    ),
]
PRIVATE_MARKERS = [pattern for _name, pattern, _replacement in PRIVATE_RULES]


def private_reasons(text):
    """Stable privacy finding names without returning the matching value."""
    value = text or ""
    return [name for name, pattern, _replacement in PRIVATE_RULES if pattern.search(value)]


def privacy_preview(text, max_chars=120):
    """Return only typed placeholders when any private marker is present."""
    original = text or ""
    placeholders = []
    for _name, pattern, replacement in PRIVATE_RULES:
        if pattern.search(original) and replacement not in placeholders:
            placeholders.append(replacement)
    if placeholders:
        return " ".join(placeholders)
    value = original
    value = re.sub(r"\s+", " ", value).strip()
    max_chars = max(20, min(int(max_chars or 120), 500))
    if len(value) > max_chars:
        value = value[: max_chars - 3] + "..."
    return value


def is_shareable(text):
    """True only if `text` has no private markers and is a short generic sentence."""
    if not text:
        return False
    if len(text) > MAX_LEN:
        return False
    return not private_reasons(text)


def scrubbed_lessons(conn):
    lessons = memory_store.all_lessons(conn)
    return [
        {
            "id": "lesson-" + hashlib.sha256(
                lesson["text"].encode("utf-8")
            ).hexdigest()[:24],
            "text": lesson["text"],
        }
        for lesson in lessons
        if is_shareable(lesson["text"])
    ]


def main(out="contrib/lessons_contrib.jsonl", db=None):
    db = db or os.path.join(os.path.dirname(__file__), "memory.db")
    conn = memory_store.connect(db)
    try:
        lessons = scrubbed_lessons(conn)
    finally:
        conn.close()

    out_dir = os.path.dirname(out)
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir)

    with io.open(out, "w", encoding="utf-8", newline="\n") as f:
        for lesson in sorted(lessons, key=lambda item: item["id"]):
            f.write(json.dumps(
                {"id": lesson["id"], "text": lesson["text"]},
                ensure_ascii=False,
            ) + "\n")

    print("wrote %d shareable lessons to %s" % (len(lessons), out))
    print("This is OPT-IN: nothing was sent anywhere. Review %s before sharing it." % out)
    print("To send it home base, either:")
    print("  1) open a PR adding this file under contrib/ on GitHub, or")
    print("  2) copy it to your file server / shared store.")


if __name__ == "__main__":
    main()
