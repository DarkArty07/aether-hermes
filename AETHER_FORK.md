# Aether-maintained Hermes

## Identity and authority

This is the Hermes runtime fork maintained for [Aether Agents](https://github.com/DarkArty07/Aether-Agents), following the owner's decision tracked in [Aether #348](https://github.com/DarkArty07/Aether-Agents/issues/348). It is derived from [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent); the upstream MIT license, copyright and contributor history are retained.

- Canonical repository: `DarkArty07/aether-hermes`.
- Maintained branch: `aether-main`.
- The previous repository name redirects here. The former upstream-copy branch is historical, not the Aether runtime baseline.
- Upstream adoption is deliberate and reviewed. No automatic upstream upgrade is part of this transition.
- Aether product bugs and downstream decisions remain in the Aether issue tracker. Link each runtime fix to its issue, regression and originating upstream reference when applicable.

## Initial preservation baseline

The starting point is the actual existing local source at `0b288979e2322c02ab42c05f1e183bb31cfa5aa9`, not the old remote `main` (`2163f7f8ca82f7c892c7e815dadd80a1486cc194`) and not a fresh upstream checkout. The local source consisted of that commit plus 34 modified tracked paths and five new source/test paths. All 9,348 source entries were captured and hash-verified before consolidation; the capture manifest SHA-256 is `e4b7f05d6cd7f3260b284e936ec90f1312d62474acde238f7e693e82365cda48`.

The local Git history is shallow at `411903b6fa258f81afcc3869eb615f6218e1776a`. Its original parent identity remains unchanged, and the GitHub fork already contains that historical commit and parent. No history rewrite, synthetic root, or upstream download is used to hide the shallow boundary. Original local refs, reflogs, stash, source files, build outputs and package-version inventory were preserved privately before any repository changes.

The baseline commit records existing work; it is not a claim that the consolidating agent authored each prior repair. Existing HLP records in Aether's `HERMES_LOCAL_PATCHES.md` retain the issue-level evidence. Unknown historical attribution must remain explicit rather than being invented or silently discarded.

The independent delta audit maps most changes to existing HLP-188/189/191/194/198/204/209/211/226/246/247/262/263/280/305/335 records. Five paths lack complete ledger attribution and are deliberately retained only as observed baseline state: `agent/chat_completion_helpers.py` and `tests/agent/test_codex_ttfb_watchdog.py` (wait-notice wording); `agent/turn_context.py` and `tests/agent/test_turn_context.py` (affinity persistence fail-closed); and `package-lock.json` (27 generated `peer` metadata additions, not a package version change). This preservation decision prevents silent behavioral changes during consolidation; it does not certify those changes as a new product requirement. In particular, one received stream event is not conclusive evidence that every later wait is model reasoning.

The new `hermes_cli/kanban_exit_codes.py` was previously untracked but is imported by runtime code; it and all four new tests are included. The Kanban database file contains multiple overlapping HLPs and must never be replaced wholesale to retire one repair. Ledger header/base drift, incomplete HLP-263 structured attribution and the absence of a committed HLP-335 standalone regression remain documentation/qualification debt, not reasons to discard the current source.

## Verification and remaining limits

The exact captured candidate passed the existing per-file isolated runner on all 20 changed/new test files plus storage turn-lease controls: **453 passed, 0 failed, 1 Windows-only skip**, across 21 files, with retries disabled. The runner used the already provisioned Python dependencies, not a new dependency resolution. Source parity and `git diff --check` passed. An added-diff scan found no new credential-shaped tokens, private-key blocks or operator paths; this narrow check is not a security certification.

This is a source-preservation baseline, **not** a clean-install, full-suite, cross-platform or distributable-release qualification. The already-running TUI can still retain older modules until a graceful reload. Generated frontend builds and Python environments are not committed. Packaging the fork, frontend changes and the final installer remain separate product work.

Inherited GitHub Actions are disabled for initial import: they include upstream-specific scheduled installation, review and publication operations. Disabled Actions are not passing CI. Local test evidence above is the applicable baseline evidence; an Aether-specific CI/release setup must be explicitly qualified before a release.

## HLP-305 legacy FTS holder repair

The legacy FTS repair addresses the writer side of Aether #305, in addition to the preserved lease-waiter fix. It adapts the bounded-prefix/high-water idea from [upstream 57162d0cc1875ef6307aebe6cb599b5a1d052dd2](https://github.com/NousResearch/hermes-agent/commit/57162d0cc1875ef6307aebe6cb599b5a1d052dd2), authored by fangliquanflq, without fetching or upgrading upstream.

Scope is existing legacy inline FTS only. Atomic trigger replacement records the historical maximum message ID, leaves prior indexed content untouched and limits newly indexed tool-result bodies to 8192 characters. Canonical messages remain complete. Explicit tool-role searches use the existing canonical-message fallback so terms beyond the prefix remain searchable. Unfiltered FTS searches inspect the prefix for those new tool rows; callers needing complete tool content must request `role_filter=['tool']`. Fresh external-content/CJK layouts are not migrated by this repair. Failed DDL rolls back the marker and trigger changes together.

Evidence:

- Unchanged baseline: 3 failing regression assertions, 1 passing preservation control; final focused suite: 6 passed, including transactional rollback and fresh-layout controls.
- Existing FTS/lease compatibility suite: 73 passed before the two additional controls; independent reviewer found no blockers in this bounded scope.
- Expanded persistence/TUI suite: 867 passed, 1 pre-existing failure reproduced unchanged on baseline, tracked in [Aether #349](https://github.com/DarkArty07/Aether-Agents/issues/349). No test was weakened or skipped to hide it.
- Reproduce: `PYTHONPATH=. <Hermes-python> scripts/qualify_legacy_fts_contention.py "$PWD" 384`. Disposable storage, 384 synthetic tool rows (204471936 bytes), real batch writer and a separate real lease refresher. Before: insert critical section 27.576s, lock timeout after 20.397s. After: insert 2.078s, refresh succeeds after 2.159s; all complete rows survive. This reproduces the mechanism, not the unknown exact payload of the original incident.

Activation is separate from source publication: the active database requires a verified backup and scoped schema transition. Never restore an older database snapshot over newer conversations. After new prefix-indexed rows exist, reverting search code alone is not a complete index rollback; retain the tool-search fallback until any deliberate index reconstruction is qualified. No full-index rebuild or canonical-message rewrite is part of activation.

## Maintenance rules

1. Develop from `aether-main` using short-lived branches and inspectable commits. Never use the historical upstream-copy branch as an update target.
2. Keep upstream bug fixes distinct from Aether-specific adaptations. A bug fix may retire after equivalent upstream behavior passes its tests; a deliberate Aether feature need not retire.
3. Record the source reference and reasoning for intellectually adapted upstream fixes. Do not import unrelated refactors or upgrade the runtime merely to fix one bug.
4. Before selecting upstream updates, preserve and reconcile every downstream invariant on an isolated candidate. No force push, history rewrite or automatic patch-stack replay is required.
5. Pin the exact qualified fork commit/artifact in the eventual Aether release. Keep source, built assets, dependencies, license notices, state migrations and rollback evidence attributable.
6. Never commit operator profiles, credentials, databases, sessions, local build environments or private backups. Do not delete state to make tests or migrations pass.

## Runtime reliability corrections (Aether #267/#292/#294/#295/#301/#304)

These changes land on `aether-main` from independently reviewed unit commits. They do not activate the live TUI/gateway. Inherited GitHub Actions remain disabled and are not claimed green. Local runner evidence is in Aether `specs/runtime-reliability-bugs/evidence/`.

| Issue | Disposition | Inspectable commit | Scope |
| --- | --- | --- | --- |
| [#304](https://github.com/DarkArty07/Aether-Agents/issues/304) | reproduced-and-fixed | `169572a845f30bda0231cb97035bfce5fb9e981d` | `agent/kanban_stop.py` |
| [#295](https://github.com/DarkArty07/Aether-Agents/issues/295) | reproduced-and-fixed | `b58db22b4968e837a745a42cae5cb007f3819a8c` | `agent/error_classifier.py` |
| [#292](https://github.com/DarkArty07/Aether-Agents/issues/292) | already-working-with-integrated-evidence | `2337bd2d9efbf0421ac121877936e92bb9486e70` (tests only) | `tests/agent/test_auxiliary_named_custom_providers.py` |
| [#301](https://github.com/DarkArty07/Aether-Agents/issues/301) | reproduced-and-fixed | `0f56400b1603c8195590a04da47424a0df40b145` | `agent/auxiliary_client.py` |
| [#294](https://github.com/DarkArty07/Aether-Agents/issues/294) | reproduced-and-fixed | `cf5ff5fe2f51116364a10a9941f58f280e7ff4c4` | `agent/background_review.py` |

Aether laboratory isolation for #267 is in `DarkArty07/Aether-Agents`, not this fork.

Rollback of a single behavior is the corresponding unit commit revert. Do not restore whole files that also contain unrelated downstream hunks. Retirement follows the Aether ledger entries in `HERMES_LOCAL_PATCHES.md`.

## Execute-code helper contract and search_files JSON framing (Aether #313/#353)

These changes land on `aether-main` from independently reviewed unit commits for Objective Contract `oc_fd2332ffe34aa5f7@v1`. They do not activate the live TUI/gateway. Inherited GitHub Actions remain disabled and are not claimed green. Local runner evidence is in Aether `specs/execute-code-helper-search-framing/evidence/`.

| Issue | Disposition | Inspectable commit | Scope |
| --- | --- | --- | --- |
| [#313](https://github.com/DarkArty07/Aether-Agents/issues/313) | reproduced-and-fixed | `2d89a331f6378f42fc26f7a439e8591101289ca6` | `tools/code_execution_tool.py`, `hermes_cli/tips.py` |
| [#353](https://github.com/DarkArty07/Aether-Agents/issues/353) | reproduced-and-fixed | `8a23d40eb93319ff6ea94e27c68e5643cc6d8852` | `tools/file_tools.py` (`search_tool` truncation path) |

- **#313 behavior:** Schema and CLI tip require `from hermes_tools import json_parse, shell_quote, retry`. NameError hints prescribe that import. Helper ImportError hints report stale generated-module / sys.path skew. Helpers remain generated `hermes_tools.py` exports; they are not injected into globals or builtins. Upstream adaptation: `NousResearch/hermes-agent@65f033a1a20e847b7a150fe6168cd345385c7d07` / PR `#83772`.
- **#353 behavior:** Truncated `search_tool` output stays one JSON document by placing the pagination hint in `result_dict["_hint"]` with next offset `offset + limit` before a single `json.dumps`. No trailing plain-text suffix. No generic RPC JSON-suffix tolerance. Upstream adaptation: PR `#104472` / `NousResearch/hermes-agent@7a6c3b41c33a61601132c6bbf737735bd4a07207`.
- **Rollback:** revert the corresponding unit commit. Do not restore whole files that also contain unrelated downstream hunks.
- **Retirement:** #313 retires when an adopted Hermes release requires the explicit-import contract and `tests/tools/test_execute_helper_contract.py` passes without this patch. #353 retires when an adopted Hermes release contains PR `#104472` equivalent producer framing.

## Delegated-child snapshot identity and Kanban worktree base ref (Aether #310/#354)

These changes land on `aether-main` from independently reviewed unit commits for Objective Contract `oc_0084270d940c98d9@v1`. They do not activate the live TUI/gateway. Inherited GitHub Actions remain disabled and are not claimed green. Local runner evidence is in Aether `specs/fix-310-341-345-354/evidence/`.

| Issue | Disposition | Inspectable commit | Scope |
| --- | --- | --- | --- |
| [#310](https://github.com/DarkArty07/Aether-Agents/issues/310) | reproduced-and-fixed | `25cabeb25327199a03aa3cf1613ed2f815f646cb` | `tools/environments/base.py` |
| [#354](https://github.com/DarkArty07/Aether-Agents/issues/354) | reproduced-and-fixed | `7d3173e1f3dba107f9a389d4e35c95f215775ee1` | `hermes_cli/kanban_db.py` (worktree materialization and metadata read/write path) |

- **#310 behavior:** bash session snapshot dumps (`_export_dump_excluding_session_vars`) unset `${!HERMES_KANBAN_*}` and `HERMES_DELEGATED_CHILD_CONTEXT` prior to `export -p`. Child mutation-denial markers and dispatcher-owned Kanban variables remain transient per-command identity and do not persist into reusable snapshots. Existing ContextVar/process propagation and fail-closed Kanban DB denial stay intact. No global `os.environ` clear.
- **#354 behavior:** when board metadata carries a valid `worktree_base_ref` (lowercase 40-character SHA-1 matching `^[0-9a-f]{40}$`), newly materialized linked git worktrees for new branches start at that commit instead of incidental primary `HEAD`. Invalid present refs fail closed with `ValueError`. Absent refs retain standard `HEAD` behavior for non-Aether boards. Occupied-path fallback and linked-worktree reuse remain intact. Native `default_workdir` stays the registered primary path.
- **Rollback:** revert the corresponding unit commit. Do not restore whole files that also contain unrelated downstream hunks.
- **Retirement:** #310 retires when an adopted Hermes release excludes `HERMES_DELEGATED_CHILD_CONTEXT` and `HERMES_KANBAN_*` from terminal session snapshots. #354 retires when upstream Hermes adopts board-level worktree base ref configuration with equivalent validation and fail-closed semantics.

## Current blocking incident

[Aether #305](https://github.com/DarkArty07/Aether-Agents/issues/305) tracks spontaneous TUI turn cancellation under session-store contention. The inherited HLP-305 patch bounds retry of a transient lease-refresh lock but does not establish which transaction held the original lock. Its full root-cause and reload/canary status remain tracked there; do not call the incident fully fixed merely because this baseline was preserved. The runtime-reliability corrections above do not close #305.

## Autonomous bug-remediation corrections

These changes land on `aether-main` from independently reviewed unit commits for Objective Contract `oc_ddebf175a40251f7@v1`. They do not activate the live TUI, gateway, profiles, or installation. Inherited GitHub Actions remain disabled and are not claimed green. Local evidence is in Aether `specs/autonomous-bug-remediation/evidence/`.

| Issue | Disposition | Inspectable commit(s) | Scope |
| --- | --- | --- | --- |
| [#362](https://github.com/DarkArty07/Aether-Agents/issues/362) | reproduced-and-fixed | `8afefe7e304f2b3c80cecbbb24bf9e60be72044b` | `hermes_cli/kanban_db.py`, `tools/kanban_tools.py`, review-lifecycle tests |
| [#315](https://github.com/DarkArty07/Aether-Agents/issues/315) | reproduced-and-fixed | `3bc559b6d29f5f5cb99f34c60e5163de38ae91ad`, `37d03ac30b239d990b515e88d475bed9ec714fdf` | `tools/file_tools.py`, file-write safety and SOUL-gating tests |
| [#306](https://github.com/DarkArty07/Aether-Agents/issues/306), [#293](https://github.com/DarkArty07/Aether-Agents/issues/293) | reproduced-and-fixed | `0d0fbecb54bde61e5caa1eac5d4d66923bc5e71f`, `adaa181c08321e6d7fce4b875b8d36c0998b520a`, `d68132254b088d26730134d86f8b27a7474ef460` | `agent/model_metadata.py`, gateway metadata regressions |
| [#296](https://github.com/DarkArty07/Aether-Agents/issues/296), [#303](https://github.com/DarkArty07/Aether-Agents/issues/303) | reproduced-and-fixed | `b1e3ca80a9a79cf8e0482d6621d329c7ae88e236`, `a134c9c4f4d4963c811a30bad72e4be6ae67254a`, `1ccfb08b8bb86c215c09bc8ee3e45f7c290ae6fc`, `7b75f6e83f06badc09e73c87cba78f484d2625fd` | `agent/auxiliary_client.py`, Chat/attribution regressions |
| [#349](https://github.com/DarkArty07/Aether-Agents/issues/349) | tests-only qualification; no product defect demonstrated | `59ee7d05a7b67d52dbbfa95b6b2ced57ec6df20e` | `tests/test_hermes_state.py` trace setup |

### HLP-362 / #362 — independent same-card review ownership

An initial review request without a different reviewer fails closed before the implementation claim is cleared; self-review is rejected; legacy reviewer-null review events remain parked; valid re-review provenance and explicit reviewer cycles remain supported. Evidence: `specs/autonomous-bug-remediation/evidence/ABR-362.md`. Rollback: revert `8afefe7e304f2b3c80cecbbb24bf9e60be72044b`. Retirement: an adopted Hermes release must require an explicit independent reviewer, or resolve one and fail closed when unavailable, and pass the ABR-362 predicates without this patch.

### HLP-315 / #315 — tracked package SOUL versus installed profile SOUL

Installed Hermes profile `SOUL.md` files remain protected across supported path and case variants, while ordinary tracked package or project source files named `SOUL.md` proceed without false approval prompts. Project-local `AGENTS.md`, `CLAUDE.md`, and `.cursorrules` remain protected. Evidence: `specs/autonomous-bug-remediation/evidence/ABR-315.md`. Rollback: revert `37d03ac30b239d990b515e88d475bed9ec714fdf` and `3bc559b6d29f5f5cb99f34c60e5163de38ae91ad`. Retirement: an adopted Hermes release must preserve this distinction and pass the ABR-315 suite without the downstream commits.

### ABR-META / #306 + #293 — shape-aware local gateway metadata

The LM Studio branch now requires a top-level `models` list and a non-empty native cache. An HTTP-200 `/api/v1/models` response carrying a generic OpenAI-compatible `data` list is parsed by the existing bounded generic path, preserving advertised context values and the existing explicit override/fallback behavior. No provider, inference-routing, authentication, or auxiliary-client mechanism changed. Evidence: `specs/autonomous-bug-remediation/evidence/ABR-META.md`. Rollback: revert `d68132254b088d26730134d86f8b27a7474ef460`, `adaa181c08321e6d7fce4b875b8d36c0998b520a`, and `0d0fbecb54bde61e5caa1eac5d4d66923bc5e71f` in that order. Retirement: an exact adopted release must provide equivalent shape-aware metadata parsing and pass ABR-META without these commits.

### ABR-AUX / #296 + #303 — Chat-only auxiliaries and fallback attribution

Only a clear HTTP 400 model-surface directive selects the already-existing Chat Completions path for a configured auxiliary; Responses success and non-directive, non-400, or statusless failures retain existing behavior. Configured title-generation fallback attribution remains request-scoped, and destination authentication is not copied from the primary request. No provider, protocol, or live profile was added or changed. Evidence: `specs/autonomous-bug-remediation/evidence/ABR-AUX.md`. Rollback: revert `7b75f6e83f06badc09e73c87cba78f484d2625fd`, `1ccfb08b8bb86c215c09bc8ee3e45f7c290ae6fc`, `a134c9c4f4d4963c811a30bad72e4be6ae67254a`, and `b1e3ca80a9a79cf8e0482d6621d329c7ae88e236` in that order. Retirement: an exact adopted release must provide equivalent per-model Chat/Responses negotiation and preserve configured fallback attribution with the same boundary tests.

The #349 tests-only commit corrects stale trace instrumentation to observe the checked-out connection; product FTS code is unchanged, so no downstream behavior patch or retirement gate is recorded for that issue.

## Goal-mode review readiness and bounded flow recovery (Aether #369)

This correction was developed from maintained-fork baseline
`8a6b33ae480373015178b80e87c88fe0abda3919` for Objective Contract
`oc_7a35eca6393f18a7@v1`. It does not activate the live TUI, gateway, profiles, or
installation. Inherited GitHub Actions remain disabled and are therefore NOT RUN,
not green. Portable Aether evidence is in
`specs/followup-aether-bugs/evidence/FU-369.md`.

Inspectable commits:

- `8ddb28c8eb747345254970339f0c1a67d05b454e` — separates the goal-mode review
  request from whole-objective completion and narrows controller recovery.
- `74a4902200a6754b6ec5271f500f92716da349e7` — adds the distinct
  implementation-readiness judge for tool and CLI review requests.
- `8b600f3bf508326cd8defdb9da03757b838619b7` — covers phase-aware readiness,
  incomplete rejection, and preserved completion behavior.

A complete goal-mode implementation can request independent review using truthful
summary and structured metadata without being required to supply the verdict that the
reviewer will produce. Incomplete work remains running, whole-objective completion keeps
its original goal judge, self-review remains rejected, and requested-changes/re-review
provenance remains durable. The only additional goal-mode block route is the exact
terminal flow-controller pair `kind="capability", origin_signal="recovery"` while a
durable `flow_attention` remains unresolved. Arbitrary capability or transient blocks
do not become goal-loop escape paths.

Unchanged baseline regressions failed `3` acceptance cases and passed `93` controls.
The reviewed candidate passed `97` focused tool/CLI/review/session-affinity tests,
`155` extended goal/readiness tests, and the same `97` focused tests after applying the
portable patch to the exact base. Ruff and compileall passed on changed source/tests,
and both source and patch diff checks passed. Exact upstream
`NousResearch/hermes-agent@6e07eb48387044dbcaf12490931c2b8ca7ec8653` retains the
circular review gate and lacks the bounded recovery predicate, so the correction remains
downstream.

Rollback reverts the three commits above in reverse order without restoring whole files
that also carry other Aether corrections. Retirement requires an adopted exact Hermes
release to pass complete-to-review readiness, incomplete rejection, independent
review/re-review, whole-objective completion, exact recovery-origin delivery, and
arbitrary-block negative controls without these commits.

## Cross-board Project recovery for a shared worktree (Aether #226)

This correction was developed from maintained-fork baseline
`415056fee527c5a2302370bd6dba56f84b9a4202` for Objective Contract
`oc_644c0b407d13366a@v1`. It does not activate the live TUI, gateway, profiles, or
installation. Inherited GitHub Actions remain disabled and are therefore NOT RUN,
not green. Portable Aether evidence is in
`specs/hlp-226-cross-board-project-inheritance/evidence/HLP-226C.md`.

Inspectable commits:

- `d962b73e3d5c73aa21c500c6e1c51026dfb0d686` — recovers Project/repository identity
  from the current board's own `project_id` + `default_workdir` binding when a
  project/affinity `dir` source shares `<repo>/.worktrees/<prior-board-leaf>`, and
  carries the exact board selection from `kanban_create` into native creation.
- `3f981f10924774afd4b9a72d81f525fafe64fd5c` — covers the recurrence, the
  terminal-to-Implementer E2E with real worktree materialization, and the complete
  fail-closed matrix.
- `7980bbf1f9f75efdcbee2196ae910bb77138541d` — pins the explicit
  `kanban_create(board=<target>)` selection against a conflicting process-current
  decoy board, so the board metadata read for recovery can only be the target board's.

When no worker profile registers the Project, a current-board project/affinity root
whose shared worktree leaf names no task in this board keeps its Project: recovery
requires the exact source Project and affinity, board metadata binding the same
Project to the repository that contains the shared path, and exactly one opaque leaf
under `<default_workdir>/.worktrees/`. The leaf is never looked up or trusted, no
other profile registry is read or copied, there is no cross-board task query, and
every mismatch keeps the existing native fail-closed error. Direct worktree
inheritance (HLP-226), same-board shared terminals (HLP-226b), scratch and
non-affinity behavior are unchanged.

Candidate evidence: the identical test bytes failed `3` of `21` on the unchanged
baseline with `kanban_create: session-affinity tasks require a canonical project_id`
and passed `21` on the candidate; reverting only the tool-side board propagation
leaves `20 passed / 1 failed` (the explicit-board regression); the minimum affected
run passed `132` tests with `1` Windows-only skip; the documented full fork suite
reported `31020 passed, 2392 failed, 262 skipped` against the recorded baseline
`31002 passed, 2392 failed, 262 skipped`, with the recorded failing-file set
unchanged apart from one parallel-run pytest teardown crash whose 3 tests passed
in-process and which passed 3/3 on a clean re-run. Ruff check, compileall and
`git diff --check` passed on touched scope; the pre-existing `ruff format` debt on
the two production files was preserved rather than reformatted.

Rollback reverts the three commits above in reverse order without restoring whole
files that also carry other Aether corrections. Retirement requires an adopted exact
Hermes release to perform the same conjunctive board-bound recovery — or to reject
the prior-board leaf with an equivalent safe alternative — and to pass the
recurrence, the E2E, the fail-closed matrix and the explicit-board regression without
these commits.

## Native collaboration store, tool surface, and origin-bound delivery (Aether #334)

These changes land on `aether-main` from independently reviewed unit commits for
Objective Contract `oc_a28ff9b7fa20d29d@v1`. They do not activate the live TUI,
gateway, profiles, or installation. Inherited GitHub Actions remain disabled and
are therefore NOT RUN, not green. Portable Aether evidence is in
`specs/collaborative-execution/evidence/`.

Inspectable commits on this branch:

- `31ec39a8560df24938e78d6956693bd811f04311` — adjunct `kanban_collaboration`
  table, optional `kanban_create(collaboration="advisory")` root opt-in, and
  request/respond/ack/resolve on `kanban_comment`.
- `20db06c0b8441190830aa72e3c0de6fdce6b8db4` — fixture isolation, `archive_task`
  expiry, advisory worker context, board.json contract binding, and
  `source_run_id` persistence (independently reviewed CE-HF-CORE).
- `3b569a345b`, `4942a7cfa3`, `d1d1f9e9f4` — TUI/gateway consumers and
  exclusive-claim/process-loss recovery.
- `2ec004ea54ab2401f63f1885a7c26f59bf1eb7db` — persist runtime-derived
  `origin_route` on the existing `collaboration_opted_in` event and match it
  before claim so extra notify subscribers are not recipients (independently
  reviewed CE-HF-DELIVER; Morfeo D5).

Behavior: optional collaboration is additive. Legacy roots, ordinary comments,
and the terminal-notification cursor remain unchanged. Internal collaboration
is wake-only with labeled peer evidence; it does not impersonate owner input.
Exact-one-origin delivery uses the trusted commissioning route; missing or
ambiguous origin stays unavailable. Decomposition-root `done` does not expire
descendant collaboration; archived or terminal flow does.

Candidate evidence (this tree, `HERMES_TEST_FILE_RETRIES=0`): collaboration
suites 53 passed in 8.07s; neighboring regression 671 passed in 39.66s;
`git diff --check 3b81e9d...HEAD` clean. Imports resolved to this candidate.

Rollback reverts the listed commits in reverse order without restoring whole
files that also carry other Aether corrections. Retirement requires an adopted
exact Hermes release to provide equivalent optional collaboration semantics and
to pass the focused collaboration suites without these commits.

## Supervisor same-card review-flow continuity for the RC (Aether #425/#426)

This correction was developed from maintained-fork baseline
`bb5e9a422f3135371557e75bcd1db01c5f8fc3ba` on branch
`fix/425-review-flow-continuity` for Objective Contract
`oc_3397f9f05d780f8e@v1` (Aether `1.0.0rc1`, tag `v1.0.0-rc.1`). It does not
activate the live TUI, gateway, profiles, or installation. Inherited GitHub
Actions remain disabled and are therefore NOT RUN, not green. Portable Aether
evidence is in `specs/001-aether-v1-productization/evidence/LC-FORK.md`.

Inspectable commits on this branch:

- `10a307bb22cf07dab5025120eee2eacb1424f8ca` — same-card Aether review reuses
  one existing Supervisor flow session and its canonical session workspace
  (`_review_flow_session_context`, `_reserve_review_flow_session`), separates
  session-identity cwd from the candidate tool cwd (`resolve_session_identity_cwd`)
  and keeps affinity compression in place (`_compression_in_place_for_agent`).
- `c007cea73f3cfc7106dbdffa95598e0324934a32` — recognized Aether provenance fails
  closed when it is damaged, ambiguous or mismatched, instead of silently
  creating a fresh generic reviewer; genuine generic and legacy review paths keep
  their existing behavior.

Behavior: generic Hermes review remains generic when no corroborated Aether
opt-in exists. A recognized Aether review requires one consistent collaboration
root, board/Project/contract binding and one existing same-profile affinity
session; missing, malformed, ambiguous or mismatched provenance fails through the
existing lifecycle rather than spawning a reviewer. The implementation card keeps
its candidate workspace and persistent Implementer pins, the borrowed Supervisor
lease is ephemeral and resumes the exact stored session and canonical workspace,
and supported file and terminal work stays on the candidate workspace while prompt
identity stays stable across the review cwd change. Completely erased provenance
is never guessed.

Candidate evidence (this branch, sterile `HOME`, `HERMES_TEST_FILE_RETRIES=0`):
the affected review/session/collaboration selection passed `399 passed, 0 failed,
1 skipped` across 19 files in 15.9s with 24 workers (exit 0); the blocking
`ruff check .` passed for the whole tree and `git diff --check` was clean for the
branch diff. `ruff format --check` reports the same pre-existing repository drift
at this branch and at its base, so no file changes format-check status.

Rollback reverts the two commits above in reverse order without restoring whole
files that also carry other Aether corrections. Retirement requires an adopted
exact Hermes release to distinguish generic from corroborated Aether review, reuse
one existing reviewer session and canonical workspace without persisting Supervisor
affinity onto the implementation card, keep task and file tools on the candidate,
preserve prompt identity across the review cwd change, fail closed on damaged
recognized provenance, keep legacy and generic controls, and pass the focused
review/session matrix without these commits.

## Project provenance recovery, early refusal, and bounded failure containment (Aether #494)

This correction was developed from maintained-fork baseline
`aed6591a69f453a1867b73628603e7b53ba40ffc` on branch
`fix/494-project-provenance-and-bounded-failure` for Objective Contract
`oc_bc27d75a1165818f@v1`. It does not activate the live TUI, gateway, profiles, or
installation. Inherited GitHub Actions remain disabled and are therefore NOT RUN,
not green. Portable Aether evidence is in
`specs/issue-494-project-provenance/evidence/HLP-428.md`.

Inspectable commits on this branch:

- `e97c1be0695cfcfabce4d9dfaaa9a3eff1a817e9` — recovers canonical Project
  provenance for a non-affinity child created under a project-bound `dir` parent
  on a recognized Aether board, refuses actionably before persisting any row when
  safe recovery fails, and contains the malformed same-card review-spawn failure
  in `_route_affinity_terminal` and `block_task`.
- `b0371537c0a6d02d8fd6a9cbc528ae7741507938` — scopes the cross-Project parent
  mismatch refusal to recognized Aether boards so generic board collaboration
  semantics stay constructible, and normalizes the `parents` iterable before its
  first consumption.
- `184a6d03bcab8964fa507ada4d02edc13cdcfb9f` — pins the generic cross-project
  parent-mismatch case as a constructible fail-before control in the AC3 matrix.

Behavior: when an explicit Project is supplied for a child of a direct
project-bound `dir` parent on the current board, creation recovers the board's
canonical Project and materializes a fresh `<repo>/.worktrees/<child-id>` worktree
instead of silently persisting a null-Project scratch directory. Recovery stays
conjunctive on the board binding, and traversal, symlink, absent-parent-edge and
unbound-Project cases refuse before any task, run or event row exists. Refusal is
scoped to recognized Aether boards, so ordinary non-Aether boards keep creating
cross-project children and the pre-existing collaboration fail-closed state stays
constructible. A deterministic review-spawn failure reaches the configured bounded
breaker and a durable `blocked` receipt with `consecutive_failures`, cleared worker
and claim state and `gave_up`, without emitting an unverified origin signal or
copying a notification subscription.

Candidate evidence (this branch, sterile `HOME`, `HERMES_TEST_FILE_RETRIES=0`): the
delivered regression module reproduces RED on the unchanged baseline (`4 failed,
1 passed`, including `Expected canonical project …, got None`) and passes `5/5` on
the candidate; the touched Kanban selection passed `100 passed, 0 failed` across
5 files; the wider affected surface passed `208 passed` across 11 modules; `ruff
check` passes on the touched paths and `git diff --check` is clean. `ruff format
--check` reports the same pre-existing repository drift at this branch and at its
base, so no file changes format-check status. One containment site
(`_escalate_expired_flow_attentions`) shares the guarded defect class but has no
delivered test coverage and is named by no acceptance criterion.

Rollback reverts the three commits above in reverse order without restoring whole
files that also carry other Aether corrections. Retirement requires an adopted exact
Hermes release to recover canonical Project provenance for a non-affinity direct
parent worktree child, refuse cross-project mismatches before task insertion on
recognized Aether boards while keeping generic board semantics, bound review-lane
failure containment without unverified notifications or claim retention, and pass
`tests/hermes_cli/test_kanban_project_provenance.py` without these commits.

## Auxiliary Responses reasoning usage preservation (Aether #433)

This correction was developed from maintained-fork baseline
`58f8c37a49b341f25b8fdd6310542fe932031b8d` on branch
`fix/433-reasoning-usage` for Objective Contract `oc_3fdf70ccc94e14b1@v1`. It does not
activate the live TUI, gateway, profiles, or installation. Inherited GitHub Actions
remain disabled and are therefore NOT RUN, not green. Portable Aether evidence is in
`specs/issue-433-reasoning-usage/evidence/HLP-433.md`.

Inspectable commit on this branch:

- `c31bd6ca149d6d23ebe5678b4a3d91aa17b5de91` — the auxiliary Codex/Responses adapter
  carries a completed response's provider-reported nested reasoning detail onto the
  chat-compatible `usage` object.

Behavior: when a completed auxiliary Responses response reports usage with
`output_tokens_details.reasoning_tokens` — as a typed object or a plain mapping, and
by the same rule when a `response.completed` SSE stream supplies the terminal usage —
`_CodexCompletionsAdapter` in `agent/auxiliary_client.py` preserves that nested detail
on the reconstructed chat-compatible `usage`. `normalize_usage` already reads that
shape, so the provider's reasoning subcount reaches `_validate_llm_response`, the
existing auxiliary accounting chokepoint and the existing `session_model_usage` row
without changing `prompt`/`completion`/`total` tokens and without counting reasoning a
second time into output or total. An explicit provider zero stays zero, an absent or
malformed optional detail stays absent/zero without raising or fabricating a value, and
a missing usage object still produces no accounting row and no call failure. Terminal,
phase, tool-call, output-text-fallback, `tool_choice`, timeout and cancellation
behavior are unchanged.

Candidate evidence (this branch, `HERMES_TEST_FILE_RETRIES=0`): the delivered
regression module reproduces RED on the unchanged baseline (`5 failed, 10 passed`) and
passes `15/15` on the candidate; the affected auxiliary/accounting battery passed `238
passed, 0 failed` across 5 files. The preserved consumer paths
(`agent/usage_pricing.py`, `agent/aux_accounting.py`, `agent/codex_runtime.py`,
`tests/agent/test_auxiliary_client_responses_terminal_420.py`,
`tests/hermes_state/test_aux_usage_accounting.py`) are byte-identical to the baseline.
`ruff check` passes for the whole tree and `git diff --check` is clean for the branch
diff; `ruff format --check` reports the same pre-existing repository drift at this
branch and at its base, so no file changes format-check status. The wider suite
reports only baseline failures plus one ambient-load flake that passes on re-run.

Rollback reverts the commit above. Retirement requires an adopted exact Hermes release
to preserve provider reasoning tokens across the Responses adapter boundary into
chat-compatible usage and SessionDB accounting without altering input/output/total
tokens or double-counting, and to pass
`tests/agent/test_auxiliary_client_responses_reasoning_433.py` without this commit.

## Distribution identity and release binding

The fork keeps its own distribution and version identity: `hermes-agent` `0.20.1`
is declared identically by `pyproject.toml` (`[project].name` and
`[project].version`), `hermes_cli/__init__.py` (`__version__`) and the editable
package entry in `uv.lock`, with `requires-python = ">=3.11,<3.14"`. No packaging
correction is part of this candidate, and `uv.lock` is byte-identical to its base.

Aether binds this fork by repository (`DarkArty07/aether-hermes`, branch
`aether-main`), exact commit and source-tree digest, not by an upstream tag. The
repository carries inherited upstream `v2026.*` tags as objects, but none of them
is an ancestor of `aether-main`: the preserved history starts at the original
local baseline, so a candidate's identity is its commit, its tree object and its
deterministic path-and-file-byte projection.

