"""Fleet synthesis instructions and host-owned build reports, without model effects."""
from __future__ import annotations

import sonder_runtime.adapters.fleet_creations as fleet_creations


def render_repository_result(result):
    rendered = (
        "=== HOST REPOSITORY SCOPE ===\nproject=%s\ntools=%s\n\n%s"
        % (result.project, ",".join(result.tools), result.output)
    )
    if result.produced_files or result.checks_run or result.checks_passed is not None:
        check = "not run" if not result.checks_run else (
            "passed" if result.checks_passed is True else "failed or unverified"
        )
        rendered += "\n=== HOST BUILD RECEIPT ===\nfiles=%d\nchecks=%s" % (
            len(result.produced_files), check,
        )
        if result.produced_files:
            rendered += "\nproduced:\n" + "\n".join("- " + path for path in result.produced_files)
    return rendered


def audit_prompt(task, outputs, *, repository_task, project, objective_contract="", creation_root=""):
    """Keep existing advice/objective prompts stable; build audits consume receipts."""
    if creation_root:
        lines = [
            "You are the master orchestrator. You have no filesystem or tool access. "
            "Audit the working candidates against the original task using host build "
            "receipts and guarded tool evidence below. Report each worker's folder, "
            "produced files, and checks (passed, failed, or not run). Never promote "
            "a proposal or an unverified claim into completed work. Recommend the "
            "best available candidate with a short reason; explain limitations and "
            "failed or missing checks. Do not invent new artifacts or test outcomes.",
            "", "Original task:", task, "",
            "HOST CREATION ROOT: %s" % creation_root,
            "Each child has its own host-bound worker folder under this root. "
            "Evidence is valid only for the corresponding child folder.", "",
        ]
    else:
        lines = [
            "You are the master orchestrator. You also have no filesystem or tool access. "
            "Audit the delegated outputs strictly against evidence quoted in the original "
            "task. Discard invented files, symbols, APIs, edits, test runs, and success "
            "claims. Never convert a proposal into a claim that work was completed. Resolve "
            "conflicts, separate verified findings from hypotheses. For repository tasks, "
            "end with an Evidence gaps section. For greenfield design/build tasks, "
            "implementation plans are valid outputs even when no repository evidence is "
            "provided. Return EVIDENCE_REQUIRED only when the original task explicitly "
            "requires current repository evidence and that evidence is unavailable.",
            "", "Original task:", task, "",
        ]
        if repository_task:
            lines.extend([
                "HOST REPOSITORY SCOPE: %s" % project,
                "This is repository work, not greenfield design. Use only child evidence "
                "carrying the exact host scope above. Do not substitute Sonder Runtime, "
                "the process cwd, or another repository. If scoped evidence is insufficient, "
                "return EVIDENCE_REQUIRED instead of a generic policy or architecture answer.", "",
            ])
        if objective_contract:
            lines.extend([
                objective_contract,
                "The final aggregate must include every [objective:<id>] marker. "
                "Omitting or negatively contradicting one makes aggregation fail closed.", "",
            ])
        else:
            lines.extend([
                "This task is greenfield because it did not ask to inspect an existing "
                "repository; therefore produce a concrete proposal/plan even without file "
                "evidence. For greenfield work, choose sensible defaults for unspecified "
                "libraries, mechanics, assets, and milestones; state those assumptions and "
                "turn them into implementation steps. Do not call ordinary design choices "
                "evidence gaps or ask the user to supply them. Honor explicit constraints "
                "such as no third-party libraries; if a platform API is needed, choose and "
                "name an in-house or OS-native alternative. End greenfield answers with "
                "Decisions made and Open risks, not an Evidence gaps questionnaire.", "",
            ])
    for agent_id, output in outputs:
        lines.extend(["--- %s ---" % agent_id, str(output or ""), ""])
    return "\n".join(lines)


def build_report(creation_root, lane_projects, outputs):
    """Always name every folder; rank only candidates with host-observed files.

    Checks outrank file count. Stable lane order breaks ties. A missing receipt
    cannot be treated as proof of an empty folder or of successful checks.
    """
    by_agent = dict(outputs)
    reports = []
    ranked = []
    for index, (agent_id, folder) in enumerate(lane_projects.items()):
        result = by_agent.get(agent_id)
        if result is None:
            try:
                files = fleet_creations.inventory_files(folder)
                inventory = "files=%d" % len(files)
                if files:
                    inventory += "\nproduced:\n" + "\n".join("- " + name for name in files)
            except (OSError, ValueError):
                inventory = "files=unknown (output inventory unavailable)"
            reports.append("worker=%s\nfolder=%s\n%s\nchecks=unknown (no completed host receipt)" % (
                agent_id, folder, inventory,
            ))
            continue
        reports.append(fleet_creations.host_report(agent_id, folder, result))
        files = tuple(result.produced_files)
        if files:
            check_rank = 2 if result.checks_run and result.checks_passed is True else (
                0 if result.checks_run else 1
            )
            ranked.append((check_rank, len(files), -index, agent_id, folder))
    if ranked:
        check_rank, count, _, agent_id, folder = max(ranked)
        verdict = {2: "checks passed", 1: "checks not run", 0: "checks failed or unverified"}[check_rank]
        best = "%s in %s (%s; %d host-observed file(s); ranked by checks, then files)" % (
            agent_id, folder, verdict, count,
        )
    else:
        best = "none (no completed candidate with host-observed files)"
    return "=== HOST BUILD AGGREGATION ===\ncreation_root=%s\n%s\n\nbest_candidate=%s\n\n" % (
        creation_root, "\n\n".join(reports), best,
    )
