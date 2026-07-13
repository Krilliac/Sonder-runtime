"""Conservative self-healing checks and safe repairs for sonder."""
import collections
import os
import time

import emotion_vectors
import live_reload
import memory_store
import store_integrity
import system_profile
import workflow_store


Issue = collections.namedtuple("Issue", ["code", "target", "detail", "repairable"])


def workspace_root():
    return os.path.abspath(os.path.dirname(__file__))


def _backup_file(path):
    if not os.path.exists(path):
        return None
    backup = "%s.bak-%s" % (path, time.strftime("%Y%m%d-%H%M%S"))
    with open(path, "rb") as src, open(backup, "wb") as dst:
        dst.write(src.read())
    return backup


def _check_file_backed_config():
    issues = []
    for name, module, default_obj in (
        ("system_profile", system_profile, None),
        ("emotion_vectors", emotion_vectors, emotion_vectors.DEFAULT_VECTORS),
        ("workflows", workflow_store, workflow_store.DEFAULT_WORKFLOWS),
    ):
        try:
            if name == "system_profile":
                module.read_profile()
            elif name == "emotion_vectors":
                module.read_vectors()
            else:
                module.read_workflows()
        except Exception as exc:
            issues.append(Issue(
                "%s_invalid" % name,
                module.default_path(),
                "%s: %s" % (exc.__class__.__name__, exc),
                default_obj is not None,
            ))
    return issues


def _check_live_reload(module_names):
    issues = []
    for row in live_reload.snapshot(module_names):
        if row.get("error"):
            issues.append(Issue("reload_error", row["name"], row["error"], False))
    return issues


def _check_venv():
    issues = []
    cfg = os.path.join(workspace_root(), "venv", "pyvenv.cfg")
    if not os.path.exists(cfg):
        return issues
    executable = None
    home = None
    with open(cfg, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.startswith("executable ="):
                executable = line.split("=", 1)[1].strip()
            elif line.startswith("home ="):
                home = line.split("=", 1)[1].strip()
    if executable and not os.path.exists(executable):
        issues.append(Issue(
            "broken_venv_python",
            executable,
            "venv points at a Python executable that no longer exists",
            False,
        ))
    elif home and not os.path.exists(home):
        issues.append(Issue(
            "broken_venv_home",
            home,
            "venv points at a Python home that no longer exists",
            False,
        ))
    return issues


def check(db_path, module_names=None):
    issues = []
    issues.extend(_check_file_backed_config())
    issues.extend(_check_venv())
    if module_names:
        issues.extend(_check_live_reload(module_names))
    try:
        conn = memory_store.connect(db_path)
        try:
            ok, store_issues = store_integrity.check_store(conn)
        finally:
            conn.close()
        for issue in store_issues:
            repairable = issue.code in ("orphan_fts", "missing_fts", "bad_embedding")
            issues.append(Issue(
                "store_%s" % issue.code,
                issue.lesson_id,
                issue.detail,
                repairable,
            ))
    except Exception as exc:
        issues.append(Issue(
            "memory_db_error",
            db_path,
            "%s: %s" % (exc.__class__.__name__, exc),
            False,
        ))
    return issues


def _repair_file_backed_config(issue):
    if issue.code == "emotion_vectors_invalid":
        path = emotion_vectors.default_path()
        backup = _backup_file(path)
        emotion_vectors.write_vectors(emotion_vectors.DEFAULT_VECTORS)
        return "rewrote emotion vectors defaults%s" % ((" after backup %s" % backup) if backup else "")
    if issue.code == "workflows_invalid":
        path = workflow_store.default_path()
        backup = _backup_file(path)
        workflow_store.write_workflows(workflow_store.DEFAULT_WORKFLOWS)
        return "rewrote workflow defaults%s" % ((" after backup %s" % backup) if backup else "")
    return None


def repair(db_path, module_names=None, apply=False):
    apply = apply is True
    issues = check(db_path, module_names=module_names)
    actions = []
    if not apply:
        return issues, ["dry run: no repairs applied"]

    for issue in issues:
        action = _repair_file_backed_config(issue)
        if action:
            actions.append("%s: %s" % (issue.code, action))

    conn = memory_store.connect(db_path)
    try:
        for issue in issues:
            if issue.code == "store_orphan_fts":
                conn.execute("DELETE FROM lessons_fts WHERE lesson_id=?", (issue.target,))
                actions.append("removed orphan FTS row for lesson %s" % issue.target)
            elif issue.code == "store_missing_fts":
                row = conn.execute(
                    "SELECT text FROM lessons WHERE id=?", (issue.target,)
                ).fetchone()
                if row and (row[0] or "").strip():
                    conn.execute(
                        "INSERT INTO lessons_fts(lesson_id, text) VALUES(?, ?)",
                        (issue.target, row[0]),
                    )
                    actions.append("rebuilt FTS row for lesson %s" % issue.target)
            elif issue.code == "store_bad_embedding":
                conn.execute(
                    "UPDATE lessons SET embedding=NULL, embedding_model=NULL, "
                    "embedding_revision=NULL, embedding_dim=NULL WHERE id=?",
                    (issue.target,),
                )
                actions.append("cleared bad embedding for lesson %s" % issue.target)
        conn.commit()
    finally:
        conn.close()
    if not actions:
        actions.append("no repairable issues found")
    return check(db_path, module_names=module_names), actions


def format_report(issues, actions=None):
    if not issues:
        lines = ["self-heal check: OK"]
    else:
        by_code = collections.Counter(i.code for i in issues)
        lines = [
            "self-heal check: %d issue(s) found (%s)" % (
                len(issues),
                ", ".join("%s=%d" % (k, v) for k, v in sorted(by_code.items())),
            )
        ]
        for issue in issues:
            flag = "repairable" if issue.repairable else "manual"
            lines.append("  [%s/%s] %s: %s" % (
                issue.code, flag, issue.target, issue.detail))
    if actions is not None:
        lines.append("repair actions:")
        lines.extend("  - %s" % action for action in actions)
    return "\n".join(lines)
