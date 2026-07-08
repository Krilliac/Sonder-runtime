import os
from pathlib import Path


START = "<!-- ci-artifact-badges:start -->"
END = "<!-- ci-artifact-badges:end -->"


def env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit("missing required env var: %s" % name)
    return value


def badge(label, message, color, url, logo=None, logo_color="white"):
    label_q = label.replace(" ", "%20")
    message_q = message.replace(" ", "%20")
    logo_part = ""
    if logo:
        logo_part = "&logo=%s&logoColor=%s" % (logo, logo_color)
    image = "https://img.shields.io/badge/%s-%s-%s?style=for-the-badge%s" % (
        label_q,
        message_q,
        color,
        logo_part,
    )
    alt = "%s %s" % (label, message)
    return "[![%s](%s)](%s)" % (alt, image, url)


def build_block():
    repo = env("GITHUB_REPOSITORY")
    run_id = env("GITHUB_RUN_ID")
    run_url = "https://github.com/%s/actions/runs/%s" % (repo, run_id)
    lines = [
        START,
        badge("Latest CI artifacts", "4 files", "2088FF", run_url, "githubactions"),
        badge("Android", "download", "3DDC84", env("ANDROID_ARTIFACT_URL"), "android"),
        badge("Linux", "download", "FCC624", env("LINUX_ARTIFACT_URL"), "linux", "black"),
        badge("Windows", "download", "0078D4", env("WINDOWS_ARTIFACT_URL"), "windows"),
        badge("macOS", "download", "000000", env("MACOS_ARTIFACT_URL"), "apple"),
        END,
    ]
    return "\n".join(lines)


def main():
    path = Path("README.md")
    text = path.read_text(encoding="utf-8")
    block = build_block()
    if START in text and END in text:
        before, rest = text.split(START, 1)
        _, after = rest.split(END, 1)
        updated = before.rstrip() + "\n\n" + block + after
    else:
        title, rest = text.split("\n", 1)
        updated = title + "\n\n" + block + "\n" + rest
    path.write_text(updated, encoding="utf-8")


if __name__ == "__main__":
    main()
