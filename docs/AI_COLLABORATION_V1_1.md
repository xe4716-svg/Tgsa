# TGMate AI Collaboration Policy V1.1

> Canonical decision record: [TGMate 多 AI 协作开发规范 V1.1](https://app.notion.com/p/40d5effb97b081bcbe5af0dd07500b9f?pvs=204)  
> Project tracking: [ASD-20 — AI-001](https://linear.app/asdal/issue/ASD-20/ai-001%E6%8E%A5%E5%85%A5%E5%B9%B6%E9%AA%8C%E8%AF%81-gemini2julescodex-%E5%8D%8F%E4%BD%9C) · [ASD-22 — AI-003](https://linear.app/asdal/issue/ASD-22/ai-003%E5%BB%BA%E7%AB%8B-ai-%E6%B4%BE%E5%B7%A5pr-%E5%AE%A1%E6%A0%B8%E5%92%8C%E5%90%88%E5%B9%B6%E9%97%A8%E7%A6%81)

This file is the repository-facing operating summary. The Notion V1.1 document remains the full decision record. This policy does not imply that every AI seat, check, or external integration is already enabled.

## 1. Ownership model

Four business workgroups own requirements and module boundaries. AI seats are execution/review tools and are assigned dynamically; they are not permanently mapped one-to-one to workgroups.

| Workgroup | Responsibility |
| --- | --- |
| Platform | Architecture, database, authentication, tenancy, shared contracts, membership foundations, module controls, deployment foundations |
| Bot | Bot management, webhooks, Bot broadcast, group management, auto-replies, two-way support |
| Account | Telegram user login, proxy, MTProto Gateway, account jobs, channel cloning, stickers |
| Frontend & Commercial | Customer/admin UI, orders, plans, payment flows, wallet, referrals |

ChatGPT is the project coordinator and architecture reviewer. It decomposes work, freezes shared contracts, assigns file ownership, checks cross-module compatibility, and reports evidence. The repository owner or explicitly authorized release owner retains merge and production-release authority.

## 2. AI execution seats

- **Gemini-A:** isolated feature implementation.
- **Gemini-B:** isolated tests, edge cases, regression work, or a non-overlapping implementation slice.
- **Google Jules:** executes one assigned Linear issue, runs its tests, and opens its own PR.
- **Codex:** independent code review by default; it may implement only when review duties are separated from authorship.
- **ChatGPT:** coordination, architecture and product-rule review, integration review, and acceptance tracking.

A seat is not write-enabled until its current repository access and task execution have been verified. Historical PRs prove historical activity only; they do not prove current authorization or availability.

## 3. Required task-to-PR workflow

1. Create or update one Linear issue with one accountable executor.
2. Record workgroup, dependencies, allowed paths, forbidden paths, contract versions, test commands, acceptance criteria, risk, and rollback.
3. Assign exactly one executor and one isolated branch to the issue. Never let multiple AI agents share a branch or edit the same owned files concurrently.
4. Use a branch such as `ai/<executor>/<issue-id>-<short-name>`. Preserve an existing branch convention when the issue is already part of a documented stacked-PR chain.
5. Open a Draft PR and link the Linear issue. Default target is `main`; use a non-default base only for an explicitly documented stacked dependency.
6. Run the checks that actually exist for the changed paths. Report unconfigured checks as unconfigured and tests that were not run as not run.
7. Obtain an independent non-author review. The author must never approve their own PR. Codex reviews only when it is not the PR author; ChatGPT checks requirements, architecture, product constraints, and cross-module compatibility.
8. Resolve review threads, rerun affected checks, and record the final commit, PR, CI, review, and rollback evidence in Linear.
9. Merge only after repository rules, required checks, review, dependencies, and explicit merge authorization are satisfied.

## 4. Shared contracts and database ownership

- Freeze OpenAPI, event payloads, and database schema/migration baselines before parallel implementation.
- Record shared contract changes in an ADR or interface-change note before dependent work proceeds.
- The Platform workgroup is the technical owner of shared database migrations. Other groups submit migration requests and do not independently rewrite shared migration history.
- Use expand/contract migrations and preserve compatibility during rollout.
- Include `schema_version` in asynchronous payloads where versioned payload compatibility is required.
- Do not merge a change with unresolved migration ordering, compatibility, or rollback risks.

## 5. Safety and product constraints

- Grant least-privilege repository access. Never share personal tokens, Telegram sessions, production credentials, or production database secrets with AI tools.
- Treat all AI-generated code as unreviewed until CI and an independent review have completed.
- Bound concurrency, execution time, and model spend. Stop and record repeated failures rather than retrying indefinitely.
- Do not change approved product requirements during implementation.
- Channel cloning should prefer Telegram-native forwarding or media-reference reuse; do not default to downloading media to the TGMate server and re-uploading it.
- Do not bypass protected-content restrictions or Telegram rate limits.

## 6. Reusable templates

- [Linear AI task template](templates/LINEAR_AI_TASK_TEMPLATE.md)
- Pull request template: [\.github/pull_request_template.md](../.github/pull_request_template.md)

## 7. Repository baseline audit — 2026-11-04

This is an evidence snapshot, not a permanent guarantee. Recheck it when repository settings or workflows change.

| Item | Observed state |
| --- | --- |
| Default branch | `main` |
| Main ruleset | Active repository ruleset “TGMate main PR protection” |
| Main PR rule | At least 1 approving review; stale approvals dismissed on new pushes; review threads must be resolved; deletion and non-fast-forward updates blocked |
| Required Code Owner review | Disabled in the ruleset |
| Required CI status checks | No required status-check rule was present in the retrieved ruleset |
| CODEOWNERS file | Not present at repository root or `.github/CODEOWNERS` at the time of audit |
| Workflows on `main` | `billing-unit.yml` |
| Additional workflows on `work/v2-takeover` | `root-integration.yml` and `tgmate-v2-static.yml`, in addition to `billing-unit.yml` |

The GitHub ruleset is active, but its current configuration does **not** enforce CI status checks or Code Owner approval. A passing workflow run is evidence that a workflow passed for that commit; it is not proof that the ruleset requires that workflow before merge.

The GitHub integration could read repository rulesets but did not have permission to read the legacy branch-protection endpoint. No claim is made about settings that were not returned.

## 8. Initial AI / PR evidence snapshot — 2026-11-04

- Jules has historical PR [#26](https://github.com/xe4716-svg/Tgsa/pull/26). No submitted PR review was present in the review list; current Jules authorization must still be verified.
- PR [#27](https://github.com/xe4716-svg/Tgsa/pull/27) and PR [#28](https://github.com/xe4716-svg/Tgsa/pull/28) are Draft and unmerged. Their inspected workflow runs passed, but neither had a submitted PR review in the review list.
- Gemini-A and Gemini-B do not yet have independently verifiable execution evidence.
- Codex's current repository access and execution capability have not been verified in this audit.
- PR #27 and #28 are product-engineering delivery work, not proof that the Gemini/Jules/Codex collaboration PoC has passed.

## 9. Definition of done

A collaboration workflow is accepted only when the assigned executor's permissions are verified, two non-overlapping isolated PRs have actually run CI, at least one PR completes review → changes → CI rerun, review threads are resolved, and the merge-candidate path is traceable. Tool sign-in, a task card, a branch, or a Draft PR alone is not completion.
