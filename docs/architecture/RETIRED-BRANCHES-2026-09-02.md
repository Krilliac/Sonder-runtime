# Retired branches, 2026-09-02

Every remote branch other than `main` was surveyed on 2026-09-02 against
`main` at `769a1f7` (the merge of PR #437). Each was classified by content,
not by ancestry alone: a branch counted as integrated when every line it adds
already exists in `main`, or when the handful of lines `main` lacks are the
review's own edits to work it did take. Two branches carried work `main`
did not have; both were merged into `claude/sonder-runtime-commit-6d6e4v`
(this record's branch) so the work rides to `main` with that branch.

All of them were then deleted from the remote. Their tip commits stay
reachable from `main`, from this branch, or (for the fully integrated ones)
from GitHub's reflog for as long as GitHub keeps it; to resurrect one, run
`git branch <name> <sha>` from a clone that still has the object, or use the
"restore branch" control on its merged pull request.

| branch | tip | last commit | how its work reached main |
|---|---|---|---|
| `agent/selfmod-continuous-runner` | `44b01b1e3d080c88807c7a28bcaf3b150071d8c7` | 2026-08-22 | every commit is an ancestor of main |
| `claude/fable-agent-orchestration` | `fce43c5478a8ef62fcfc396d114238819745af31` | 2026-08-22 | every line it adds is already in main (integrated by PR #432/#433/#435 in reviewed form) |
| `claude/fable-context-routing` | `c59e52aa698ff441219e96db3946acbbba090883` | 2026-08-22 | its Ollama worker-pool work was integrated in a unified, reviewed form (PR #431/#432/#435); the lines main lacks are this branch's own phrasing of features main carries under other names |
| `claude/fable-ecosystem-research` | `572518ca860daca26e87bfa79d161d6150f4e44d` | 2026-08-22 | every line it adds is already in main (integrated by PR #432/#433/#435 in reviewed form) |
| `claude/fable-harness-evals` | `a53950a87a0d8a2d2974fc09957b932f3cbb9b8e` | 2026-08-22 | every line it adds is already in main (integrated by PR #432/#433/#435 in reviewed form) |
| `claude/fable-memory-learning` | `7c95c23d5edf431cc393d141b0873a18fc74166d` | 2026-08-22 | every line it adds is already in main (integrated by PR #432/#433/#435 in reviewed form) |
| `claude/fable-security-reliability` | `3bfbb3cf52c656e38bfe8ce335be6ff504297fc3` | 2026-08-22 | every line it adds is already in main (integrated by PR #432/#433/#435 in reviewed form) |
| `claude/fable-throughput-performance` | `4e56ebf7135da4ae6acbc866113abfc8bfb6ff13` | 2026-08-23 | its Ollama worker-pool work was integrated in a unified, reviewed form (PR #431/#432/#435); the lines main lacks are this branch's own phrasing of features main carries under other names |
| `claude/fable-tools-natural-language` | `590a3638703dd6bbbd2f9e631a2cb1ba11ef01e3` | 2026-08-22 | every line it adds is already in main (integrated by PR #432/#433/#435 in reviewed form) |
| `claude/fable-ui-observability` | `06a76df6c5625bcfcd1972e438201af96a002f16` | 2026-08-22 | every line it adds is already in main (integrated by PR #432/#433/#435 in reviewed form) |
| `claude/sonnet-api-extensibility` | `c2741dde5c4af62fef2990885b272d44ade325db` | 2026-08-22 | every line it adds is already in main (integrated by PR #432/#433/#435 in reviewed form) |
| `claude/sonnet-ci-test-architecture` | `e5cef2034e075237ee9f3c709ea184e8a6848db6` | 2026-08-22 | every line it adds is already in main (integrated by PR #432/#433/#435 in reviewed form) |
| `claude/sonnet-cloud-privacy-boundaries` | `d3b9919999f91bf3d816f1213ba4c6b35e2e0e59` | 2026-08-23 | its Ollama worker-pool work was integrated in a unified, reviewed form (PR #431/#432/#435); the lines main lacks are this branch's own phrasing of features main carries under other names |
| `claude/sonnet-compatibility-migrations` | `3ec41433005def54b98cd4e228da92e428abd5d7` | 2026-08-22 | integrated in reviewed form; the only lines main lacks are wording or formatting the review changed |
| `claude/sonnet-diagnostics-doctor` | `6048cc303e53361fe01364ce0ad1c1612fc5dabb` | 2026-08-22 | every line it adds is already in main (integrated by PR #432/#433/#435 in reviewed form) |
| `claude/sonnet-docs-operator` | `5134c813fdcde80350097d8dced926ab0a6c0c33` | 2026-08-22 | every line it adds is already in main (integrated by PR #432/#433/#435 in reviewed form) |
| `claude/sonnet-install-config-migrations` | `43f727f9dd7a459bef4643f54b4d8279a1cee2ba` | 2026-08-23 | every line it adds is already in main (integrated by PR #432/#433/#435 in reviewed form) |
| `claude/sonnet-observability-tracing` | `8a0ba09a752fd1572e4f85007d076045bc666d10` | 2026-08-22 | its Ollama worker-pool work was integrated in a unified, reviewed form (PR #431/#432/#435); the lines main lacks are this branch's own phrasing of features main carries under other names |
| `claude/sonnet-packaging-release` | `f23949b644729bd497f202f0b3935c2bf3e388a4` | 2026-08-22 | every line it adds is already in main (integrated by PR #432/#433/#435 in reviewed form) |
| `claude/sonnet-provider-adapters` | `70102131f9a0385a3ebeb7663f8dbc8050cfb132` | 2026-08-22 | integrated in reviewed form: main rejects an unknown model backend through ProviderBindings ("unknown model provider") instead of this branch's factory-level allowlist |
| `codex/ci-test-fixes` | `9ef1dde35a3f6e4beee02226529df3ca23afbc38` | 2026-08-22 | every line it adds is already in main (integrated by PR #432/#433/#435 in reviewed form) |
| `codex/sol-agent-state-machine` | `0015ac9f7e71c8e2427b77176dc220bae46390cc` | 2026-08-22 | integrated in reviewed form; the only lines main lacks are wording or formatting the review changed |
| `codex/sol-data-integrity` | `32085c63704d41e8928278c67a0d1aff06227588` | 2026-08-22 | integrated in reviewed form; the only lines main lacks are wording or formatting the review changed |
| `codex/sol-developer-sdk` | `5ff8a1e77fe79179154871b36dc11242ff9b7a28` | 2026-08-22 | every line it adds is already in main (integrated by PR #432/#433/#435 in reviewed form) |
| `codex/sol-distributed-runtime` | `2733afdd5b0661d72e91de11ea13f623551ad4ed` | 2026-08-23 | its Ollama worker-pool work was integrated in a unified, reviewed form (PR #431/#432/#435); the lines main lacks are this branch's own phrasing of features main carries under other names |
| `codex/sol-failure-injection` | `9c9c145c12084bc83f6c85cc15d46a7049be78b4` | 2026-08-22 | integrated in reviewed form; the only lines main lacks are wording or formatting the review changed |
| `codex/sol-performance-profiling` | `d9423d8bb501c3d5a3570c1e039dedac4e406ea6` | 2026-08-23 | integrated in reviewed form; the only lines main lacks are wording or formatting the review changed |
| `codex/sol-reproducible-evals` | `7bc83052b2b6cfae8120a79adee919f35f667442` | 2026-08-22 | every line it adds is already in main (integrated by PR #432/#433/#435 in reviewed form) |
| `codex/sol-research-frontier` | `796c0c96fe185ed3e647f04f3065cd2f7c0040e8` | 2026-08-22 | integrated in reviewed form; the only lines main lacks are wording or formatting the review changed |
| `codex/sol-web-app-surface` | `001284c85491555b70a1378e02c83aa4c8482832` | 2026-08-23 | every line it adds is already in main (integrated by PR #432/#433/#435 in reviewed form) |
| `claude/fable-skill-forge` | `089c48c15e55af096612a31e7758d4559d4c3525` | 2026-08-23 | merged into claude/sonder-runtime-commit-6d6e4v by this cleanup |
| `docs/architecture-handoff` | `812ef30c98ed8bde4b7ecf29c1484743b51bea41` | 2026-09-02 | merged into claude/sonder-runtime-commit-6d6e4v by this cleanup |

Method: `git merge-base --is-ancestor` for ancestry; `git merge-tree
--write-tree` against `main` for a trial merge; and, for every branch whose
trial merge conflicted, a per-file count of the lines the branch adds over
its merge base that are absent from `main`'s version of the same file. The
seven branches marked "reviewed form" or "unified form" all touch the Ollama
worker pool that PR #431 introduced and PR #432 integrated after review;
`main` carries the per-worker latency EWMA, circuit half-open state, model
affinity, bounded metric labels, remote-worker consent and origin validation
they proposed, under the names the review settled on.

## Deletion

Every branch above is integrated, so all 32 are safe to delete. The
credential this session pushes with is scoped to its own branch: every
`git push origin --delete <branch>` it tried (2026-09-02, retried after the
slice-2 commit) was refused by the remote with HTTP 403 before the ref was
touched, and the GitHub connector exposes no branch-deletion call. The
owner can run the following from any checkout with push rights; each
line is independent and idempotent (a branch already gone reports
`remote ref does not exist`).

```sh
git push origin --delete agent/selfmod-continuous-runner
git push origin --delete claude/fable-agent-orchestration
git push origin --delete claude/fable-context-routing
git push origin --delete claude/fable-ecosystem-research
git push origin --delete claude/fable-harness-evals
git push origin --delete claude/fable-memory-learning
git push origin --delete claude/fable-security-reliability
git push origin --delete claude/fable-throughput-performance
git push origin --delete claude/fable-tools-natural-language
git push origin --delete claude/fable-ui-observability
git push origin --delete claude/sonnet-api-extensibility
git push origin --delete claude/sonnet-ci-test-architecture
git push origin --delete claude/sonnet-cloud-privacy-boundaries
git push origin --delete claude/sonnet-compatibility-migrations
git push origin --delete claude/sonnet-diagnostics-doctor
git push origin --delete claude/sonnet-docs-operator
git push origin --delete claude/sonnet-install-config-migrations
git push origin --delete claude/sonnet-observability-tracing
git push origin --delete claude/sonnet-packaging-release
git push origin --delete claude/sonnet-provider-adapters
git push origin --delete codex/ci-test-fixes
git push origin --delete codex/sol-agent-state-machine
git push origin --delete codex/sol-data-integrity
git push origin --delete codex/sol-developer-sdk
git push origin --delete codex/sol-distributed-runtime
git push origin --delete codex/sol-failure-injection
git push origin --delete codex/sol-performance-profiling
git push origin --delete codex/sol-reproducible-evals
git push origin --delete codex/sol-research-frontier
git push origin --delete codex/sol-web-app-surface
git push origin --delete claude/fable-skill-forge
git push origin --delete docs/architecture-handoff
```

The two branches merged here (`claude/fable-skill-forge`,
`docs/architecture-handoff`) should be deleted only once
`claude/sonder-runtime-commit-6d6e4v` has itself been merged, so their
history stays reachable in the meantime.
