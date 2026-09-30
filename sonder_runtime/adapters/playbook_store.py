"""Owner-editable Markdown, locked mutations and an approved index projection.

Topic files are authoritative. Index replacement follows topic replacement;
an interrupted index write is recoverable by the next approved-index read.
Only fields explicitly edited are replaced; unrelated owner bytes survive.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date
import json
import logging
import os
from pathlib import Path
import re
import stat
import tempfile

from sonder_runtime.adapters.filesystem.durable_locks import exclusive_file_lock
from sonder_runtime.adapters.playbook_search import duplicate_candidates
from sonder_runtime.domain.memory.playbooks import PlaybookPolicy, duplicate_match, make_entry, slugify, status_allowed
from sonder_runtime.domain.security.redaction import redact_structure
from sonder_runtime.platform.logging import Redactor
from sonder_runtime.platform.private_files import ensure_private_dir, prepare_private_file

_LOG = logging.getLogger(__name__)
_START = re.compile(r"(?m)^## Entry: ([a-zA-Z0-9_-]{1,128}) — ([^\r\n]+)\r?$")
_END = re.compile(r"(?m)^<!-- playbook-entry-end -->\r?$\n?")
_INDEX = re.compile(r"^- \[([^\]\r\n]+)\]\(([a-z0-9][a-z0-9_-]{0,63})\.md\) — ([a-z][a-z0-9-]*) — open when: (.+)$")
_META = re.compile(r"(?m)^- ([a-z_]+): ([^\r\n]*)\r?$")


class PlaybookError(ValueError):
    pass


def _check_path(path):
    for candidate in (path, *path.parents):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise PlaybookError("playbook paths must not contain links or reparse points")
        if candidate == path and stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
            raise PlaybookError("playbook files must not be hard linked")


def _read_text(path, limit=524288):
    _check_path(path)
    try:
        with path.open("rb") as handle:
            data = handle.read(limit + 1)
    except FileNotFoundError:
        return ""
    if len(data) > limit:
        raise PlaybookError("playbook file exceeds bounded read limit")
    return data.decode("utf-8")


def _write_atomic(path, content, *, limit, expected=None):
    data = content.encode("utf-8")
    if len(data) > limit:
        raise PlaybookError("playbook file exceeds byte limit")
    _check_path(path)
    ensure_private_dir(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=".playbook-", suffix=".tmp", dir=path.parent)
    try:
        prepare_private_file(temporary, create=False)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _check_path(path)
        if expected is not None and _read_text(path, limit) != expected:
            raise PlaybookError("playbook changed during update; owner edit preserved")
        os.replace(temporary, path)
        prepare_private_file(path, create=False)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _serialize(entry):
    metadata = [f"- {key}: {entry[key]}" for key in ("id", "date", "category", "status")]
    metadata.extend([
        "- tainted: " + str(entry["tainted"]).lower(),
        "- provenance: " + json.dumps(entry["provenance"], ensure_ascii=False, sort_keys=True),
    ])
    if entry.get("supersedes"):
        metadata.append("- supersedes: " + entry["supersedes"])
    return "\n".join([
        f"## Entry: {entry['id']} — {entry['title']}", *metadata,
        "", "### Body", entry["body"], "", "### Evidence", entry["evidence"],
        "", "### Triggers", ", ".join(entry["triggers"]), "",
        "<!-- playbook-entry-end -->", "",
    ])


def _blocks(raw):
    position = 0
    while (start := _START.search(raw, position)) is not None:
        end = _END.search(raw, start.end())
        if end is None:
            break  # Ambiguous owner edit; never manufacture a boundary.
        position = end.end()
        if _START.search(raw, start.end(), end.start()):
            continue  # Forged nested entry, not an independently approved note.
        yield start, end, raw[start.start():end.start()]


def _parse_topic(raw, slug):
    entries = []
    ids = set()
    for start, _end, block in _blocks(raw):
        markers = [re.search(r"(?m)^### " + name + r"\r?$\n", block) for name in ("Body", "Evidence", "Triggers")]
        if any(marker is None for marker in markers):
            continue
        body, evidence, triggers = markers
        if not body.start() < evidence.start() < triggers.start():
            continue
        pairs = _META.findall(block[:body.start()])
        metadata = dict(pairs)
        if len(pairs) != len(metadata) or metadata.get("id") != start[1] or start[1] in ids:
            continue
        if metadata.get("status") not in {"proposed", "approved", "rejected", "superseded"}:
            continue
        if metadata.get("tainted") not in {"true", "false"}:
            continue
        try:
            date.fromisoformat(metadata["date"])
            provenance = json.loads(metadata["provenance"])
        except (KeyError, TypeError, ValueError):
            continue
        if not isinstance(provenance, dict) or not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", metadata.get("category", "")):
            continue
        ids.add(start[1])
        entries.append({
            "id": start[1], "topic": slug, "title": start[2], "date": metadata["date"],
            "category": metadata["category"], "status": metadata["status"],
            "tainted": metadata["tainted"] == "true", "provenance": provenance,
            "supersedes": metadata.get("supersedes"),
            "body": block[body.end():evidence.start()].strip(),
            "evidence": block[evidence.end():triggers.start()].strip(),
            "triggers": [item.strip() for item in block[triggers.end():].strip().split(",") if item.strip()],
        })
        if len(entries) >= 512:
            break
    return entries


class PlaybookStore:
    def __init__(self, home, policy=None, redactor=None):
        self.home = Path(home).expanduser().absolute()
        self.root = self.home / "playbooks"
        _check_path(self.root)
        self.policy = policy or PlaybookPolicy()
        self.redactor = redactor or Redactor()

    def _redact(self, value):
        fn = self.redactor.redact if hasattr(self.redactor, "redact") else self.redactor
        cleaned = redact_structure(value, fn, sensitive_keys=True)
        if isinstance(cleaned, dict):
            return {fn(key): self._redact(item) for key, item in cleaned.items()}
        return cleaned

    @contextmanager
    def _locked(self):
        _check_path(self.root)
        ensure_private_dir(self.root)
        lock = self.root / ".store.lock"
        _check_path(lock)
        with exclusive_file_lock(lock, timeout=5, purpose="playbook"):
            yield

    def _read_raw(self, slug):
        return _read_text(self.root / (slug + ".md"), self.policy.topic_limit)

    def _slugs(self):
        _check_path(self.root)
        if not self.root.exists():
            return []
        slugs = []
        with os.scandir(self.root) as items:
            for index, item in enumerate(items):
                if index >= 512:
                    raise PlaybookError("playbook directory exceeds scan budget")
                if item.name == "index.md" or not item.name.endswith(".md"):
                    continue
                if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", item.name[:-3]):
                    _check_path(Path(item.path))
                    slugs.append(item.name[:-3])
        return sorted(slugs)

    def list_topics(self):
        result = []
        for slug in self._slugs()[:self.policy.max_topics]:
            entries = self.read(slug, approved_only=False)
            raw = self._read_raw(slug)
            result.append({"topic": slug, "title": (raw.splitlines() or [slug])[0].lstrip("# "),
                           "entries": len(entries), "approved": sum(entry["status"] == "approved" for entry in entries)})
        return result

    def read(self, topic, approved_only=True):
        slug = slugify(topic)
        entries = _parse_topic(self._read_raw(slug), slug)
        return [self._redact(entry) for entry in entries if not approved_only or entry["status"] == "approved"]

    def show(self, topic, entry_id=None, approved_only=False):
        entries = self.read(topic, approved_only)
        return entries if entry_id is None else next((entry for entry in entries if entry["id"] == entry_id), None)

    def _index_projection(self, raw, *, replacement=None):
        # Existing valid rows are owner-editable topic metadata. A new proposal
        # never changes them. Invalid and unrelated owner text stays on disk.
        slugs = self._slugs()
        if replacement and replacement[0] not in slugs:
            slugs.append(replacement[0])
        approved, additions = {}, []
        for slug in sorted(slugs)[:self.policy.max_topics]:
            text = replacement[1] if replacement and replacement[0] == slug else self._read_raw(slug)
            entries = [entry for entry in _parse_topic(text, slug) if entry["status"] == "approved"]
            if entries:
                approved[slug] = entries
        rows, seen = [], set()
        for line in raw.splitlines():
            match = _INDEX.fullmatch(line)
            if match and match[2] in approved and match[2] not in seen:
                rows.append(line)
                seen.add(match[2])
        for slug, entries in approved.items():
            if slug in seen:
                continue
            triggers = sorted({phrase for entry in entries for phrase in entry["triggers"]})
            triggers = [phrase for phrase in triggers if len(phrase) <= 128 and not any(c in phrase for c in "\r\n")][:32]
            line = f"- [{slug.replace('-', ' ').title()}]({slug}.md) — {entries[0]['category']} — open when: {', '.join(triggers) or slug}"
            rows.append(line)
            additions.append(line)
        return rows, additions

    def _commit(self, slug, original, updated):
        path = self.root / (slug + ".md")
        index_path = self.root / "index.md"
        old_index = _read_text(index_path, self.policy.index_limit)
        rows, additions = self._index_projection(old_index, replacement=(slug, updated))
        new_index = old_index + ("\n" if old_index and not old_index.endswith("\n") else "") + "\n".join(additions)
        if additions:
            new_index += "\n"
        for value, limit in ((updated, self.policy.topic_limit), (new_index, self.policy.index_limit), ("\n".join(rows), self.policy.index_limit)):
            if len(value.encode("utf-8")) > limit:
                raise PlaybookError("playbook file exceeds byte limit")
        _write_atomic(path, updated, limit=self.policy.topic_limit, expected=original)
        if new_index != old_index:
            try:
                _write_atomic(index_path, new_index, limit=self.policy.index_limit, expected=old_index)
            except (OSError, PlaybookError):
                # Topic is already durable. The read projection reconstructs
                # missing rows, so an index fault cannot hide or invent notes.
                _LOG.warning("Playbook saved; index refresh pending after owner edit or I/O failure")

    def note(self, topic, category, title, body, evidence="", triggers=None, *, provenance=None, tainted=True, owner_correction=False):
        if self._redact(topic) != topic:
            raise PlaybookError("topic contains secret-like material")
        slug = slugify(topic)
        entry = make_entry(slug, category, title, body, evidence, triggers=triggers,
                           provenance=provenance, tainted=tainted, owner_correction=owner_correction, policy=self.policy)
        entry = self._redact(entry)
        with self._locked():
            original = self._read_raw(slug)
            if not original and len(self._slugs()) >= self.policy.max_topics:
                raise PlaybookError("playbook topic limit reached")
            if not original:
                suggestions = self.suggest_topic(topic)
                if suggestions:
                    raise PlaybookError("similar topic exists; append to " + suggestions[0]["topic"])
            existing = _parse_topic(original, slug)
            if any(" ".join(item["body"].casefold().split()) == " ".join(entry["body"].casefold().split()) for item in existing):
                raise PlaybookError("exact duplicate playbook entry")
            candidates = duplicate_candidates(title + " " + body, existing)
            near = duplicate_match(body, [dict(item, search_text=item["body"]) for item in candidates], self.policy.near_duplicate_threshold)
            if near:
                entry["near_duplicate"] = near["id"]
                entry["status"] = "proposed"
            updated = original or f"# {slug.replace('-', ' ').title()}\n\n"
            updated += ("\n" if not updated.endswith("\n") else "") + _serialize(entry)
            if len(_serialize(entry).encode("utf-8")) > self.policy.entry_limit:
                raise PlaybookError("playbook entry exceeds byte limit")
            self._commit(slug, original, updated)
        return entry

    def review(self, topic, entry_id, status, *, expected_digest=None):
        from sonder_runtime.adapters.playbook_review import content_digest
        if not status_allowed(status):
            raise PlaybookError("invalid playbook status")
        slug = slugify(topic)
        with self._locked():
            raw = self._read_raw(slug)
            entry = next((item for item in _parse_topic(raw, slug) if item["id"] == entry_id), None)
            if entry is None:
                raise KeyError(entry_id)
            if expected_digest is not None and content_digest(self._redact(entry)) != expected_digest:
                raise PlaybookError("playbook changed before review")
            for start, _end, block in _blocks(raw):
                if start[1] == entry_id:
                    meta_end = block.index("### Body")
                    changed = re.sub(r"(?m)^- status: [^\r\n]*", "- status: " + status, block[:meta_end], count=1) + block[meta_end:]
                    updated = raw[:start.start()] + changed + raw[start.start() + len(block):]
                    self._commit(slug, raw, updated)
                    entry["status"] = status
                    return self._redact(entry)
        raise KeyError(entry_id)

    def edit(self, topic, entry_id, **changes):
        allowed = {"title", "body", "evidence", "triggers", "category", "supersedes"}
        if not changes or set(changes) - allowed:
            raise PlaybookError("unsupported or empty playbook edit")
        slug = slugify(topic)
        with self._locked():
            raw = self._read_raw(slug)
            entry = next((item for item in _parse_topic(raw, slug) if item["id"] == entry_id), None)
            if entry is None:
                raise KeyError(entry_id)
            candidate = self._redact(dict(entry, **changes))
            validated = make_entry(slug, candidate["category"], candidate["title"], candidate["body"], candidate["evidence"],
                                   triggers=candidate["triggers"], provenance=candidate["provenance"], policy=self.policy,
                                   entry_id=entry_id, created=entry["date"])
            supersedes = candidate.get("supersedes")
            if supersedes is not None and not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", supersedes):
                raise PlaybookError("invalid supersedes id")
            validated["supersedes"] = supersedes
            for start, end, block in _blocks(raw):
                if start[1] != entry_id:
                    continue
                newline = "\r\n" if "\r\n" in block else "\n"
                for key in changes:
                    value = validated.get(key)
                    if key == "title":
                        block = re.sub(r"\A[^\r\n]*", f"## Entry: {entry_id} — {value}", block, count=1)
                    elif key in {"body", "evidence", "triggers"}:
                        value = ", ".join(value) if key == "triggers" else value
                        marker = re.search(r"(?m)^### " + key.title() + r"\r?$\n", block)
                        following = re.search(r"(?m)^### (?:Evidence|Triggers)\r?$", block[marker.end():])
                        stop = marker.end() + following.start() if following else len(block)
                        block = block[:marker.end()] + str(value).replace("\r\n", "\n").replace("\n", newline) + newline * 2 + block[stop:]
                    else:
                        pattern = r"(?m)^- " + key + r": [^\r\n]*"
                        if re.search(pattern, block):
                            block = re.sub(pattern, "- " + key + ": " + str(value), block, count=1)
                        else:
                            block = block.replace("### Body", "- " + key + ": " + str(value) + newline * 2 + "### Body", 1)
                if len(block.encode("utf-8")) > self.policy.entry_limit:
                    raise PlaybookError("playbook entry exceeds byte limit")
                updated = raw[:start.start()] + block + raw[end.start():]
                self._commit(slug, raw, updated)
                return self.show(slug, entry_id)
        raise KeyError(entry_id)

    def remove(self, topic, entry_id):
        slug = slugify(topic)
        with self._locked():
            raw = self._read_raw(slug)
            for start, end, _block in _blocks(raw):
                if start[1] == entry_id:
                    self._commit(slug, raw, raw[:start.start()] + raw[end.end():])
                    return True
        return False

    def approved_index(self):
        raw = _read_text(self.root / "index.md", self.policy.index_limit)
        rows, _ = self._index_projection(raw)
        result = ""
        for row in rows:
            row = self._redact(row) + "\n"
            if len((result + row).encode("utf-8")) > self.policy.index_limit:
                break
            result += row
        return result

    def match(self, query):
        words = set(re.findall(r"\w+", query.casefold()))
        return [match[2] for line in self.approved_index().splitlines() if (match := _INDEX.fullmatch(line))
                and words.intersection(re.findall(r"\w+", match[4].casefold()))][:self.policy.max_topics_per_turn]

    def suggest_topic(self, topic, title=""):
        from difflib import SequenceMatcher
        slug = slugify(topic)
        return [item for item in self.list_topics() if item["topic"] != slug and
                (SequenceMatcher(None, slug, item["topic"]).ratio() >= 0.86 or
                 duplicate_match(title or topic, [{"title": item["title"]}], .8))]

    def merge_duplicates(self, topic, *, apply=False):
        from sonder_runtime.domain.memory.playbooks import content_digest
        entries = [item for item in self.read(topic, approved_only=False) if item["status"] not in {"rejected", "superseded"}]
        seen, plans = {}, []
        for entry in entries:
            key = (entry["category"], " ".join(entry["body"].casefold().split()))
            if key in seen:
                plans.append({"keep": seen[key], "supersede": entry["id"], "similarity": 1.0})
            else:
                seen[key] = entry["id"]
        if apply:
            for plan in plans:
                original = next(entry for entry in entries if entry["id"] == plan["supersede"])
                self.review(topic, plan["supersede"], "superseded", expected_digest=content_digest(original))
        return plans
