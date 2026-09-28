"""Export scrubbed, shareable lessons from memory.db to an outbox JSONL.

Contribution is strictly OPT-IN: nothing here uploads or opens a PR
automatically. It only writes a local file under contrib/ that YOU review.
By default the export is empty. An owner-reviewed --approved-file JSONL row
must map the SHA-256 digest of exact source text to generic export text.
The rewrite must also pass the length and known-private-marker screen.

Review the generated file before sharing it.
"""
import io
import argparse
import hashlib
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(__file__))
import sonder_runtime.adapters.memory_store as memory_store  # noqa
import sonder_paths  # noqa
from sonder_runtime.domain.security import credential_formats  # noqa

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
            r"account[_-]?key|shared[_-]?access[_-]?key|"
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
            r"(?im)\b(?:proxy-)?authorization\b\s*:\s*[^\r\n]+|"
            # A bare bearer credential outside a header line; the digit
            # lookahead keeps prose such as "bearer token" shareable.
            r"\bbearer\s+(?=[A-Za-z0-9._~+/-]*[0-9])[A-Za-z0-9._~+/-]{16,}=*"
        ),
        "<authorization>",
    ),
    (
        "known_credential",
        # Shared with the log/session Redactor; one list for both boundaries.
        credential_formats.KNOWN_CREDENTIAL,
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
        credential_formats.JWT,
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
    """Syntactic screen for already approved export text, not an approval."""
    if not text:
        return False
    if len(text) > MAX_LEN:
        return False
    return not private_reasons(text)


def load_approved_rewrites(path):
    """Load owner-reviewed source digest to generic export text mappings."""
    if path is None:
        return {}
    with io.open(path, encoding="utf-8") as stream:
        rows = (json.loads(line) for line in stream if line.strip())
        approved = {}
        for row in rows:
            digest, text = row["source_sha256"], row["text"]
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError("invalid approved source digest")
            if not isinstance(text, str) or not is_shareable(text):
                raise ValueError("approved rewrite failed privacy screen")
            approved[digest] = text
        return approved


def scrubbed_lessons(conn, approved_rewrites=None):
    # An unmarked lesson is still private until its exact content digest has an
    # owner-reviewed rewrite. Marker screening remains a second boundary.
    approved_rewrites = approved_rewrites or {}
    lessons = memory_store.all_lessons(conn)
    result = []
    for lesson in lessons:
        source = lesson["text"]
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
        rewrite = approved_rewrites.get(digest)
        if not isinstance(rewrite, str) or not is_shareable(rewrite):
            continue
        result.append({
            "id": "lesson-" + hashlib.sha256(rewrite.encode("utf-8")).hexdigest()[:24],
            "text": rewrite,
        })
    return result


def main(out="contrib/lessons_contrib.jsonl", db=None, approved_rewrites=None):
    # The state home's store (SONDER_DB/SONDER_HOME), never a checkout-relative
    # file: that default silently created an empty database beside this module
    # and "exported" nothing while the real lessons sat in the state home.
    db = db or sonder_paths.memory_db_path()
    conn = memory_store.connect(db)
    try:
        lessons = scrubbed_lessons(conn, approved_rewrites)
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approved-file", help="JSONL rows with source_sha256 and reviewed generic text")
    args = parser.parse_args()
    main(approved_rewrites=load_approved_rewrites(args.approved_file))
