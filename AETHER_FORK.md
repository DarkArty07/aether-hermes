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
