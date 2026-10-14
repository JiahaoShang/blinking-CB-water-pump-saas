# Retire One-off Test Scripts Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans task-by-task through the user-required local Hermes CLI. Codex owns independent verification and GitHub synchronization. Do not dispatch subagents.

**Goal:** Remove historical scenario-only probes that have been superseded by maintained regression tests, including an orphan test for a missing tool, and synchronize the current branch to GitHub.

**Architecture:** Delete only the reviewed allowlist; retain the 71 tracked regression modules, browser drivers, reusable isolation/package tools and historical result evidence. No application, production data, deployment or Git-history changes.

**Tech Stack:** Python unittest, Node browser drivers, Git, local Hermes CLI.

**Spec:** User request dated 2026-10-14 in this chat; scope and exact file list recorded in docs/历史一次性测试脚本清理记录.md.

## Global Constraints

- Work only in `/Users/jiahaoshang/.codex/worktrees/196c/b2b 售前客户管理平台`.
- Preserve preexisting dirty documents and evidence; the sole authorized untracked deletion is `test_platform_model_isolated_check.py`, whose target tool no longer exists.
- Do not execute retired live probes, use real credentials/models, send messages, deploy, merge, or rewrite Git history.
- Delete by reviewed purpose/dependencies, not filename age or milestone numbering.

## Review Focus

- A retained program imports or launches a deleted script: refuse the deletion until the dependency is resolved.
- A useful security/data regression disappears: tracked test module bytes and discovered test IDs must match before/after.
- A historical command remains presented as current usage: mark the command retired while preserving its original evidence.
- An unrelated local edit is overwritten: compare protected file hashes against the captured baseline.
- GitHub still shows the old branch tip: verify the pushed branch SHA matches local HEAD; do not force-push.

### Task 1: Establish the baseline

- [x] Inventory tracked test/support programs, check code references and replacement regression coverage.
- [x] Capture file hashes and an external backup of deletion candidates, including the untracked orphan.
- [x] Collect tracked unittest IDs in an isolated copy; require zero loader errors.

### Task 2: Remove the reviewed one-off scripts

**Files:** Exact 22 tracked files and 1 untracked file listed in the cleanup record.

- [x] Hermes deletes only the allowlist and checks remaining runtime/test references.
- [x] Hermes updates directly affected usage instructions with a retirement note, without deleting logs, screenshots, results or historical conclusions.
- [x] Preserve all reusable tests/helpers and all application/deployment source bytes.

### Task 3: Verify and synchronize

- [x] Codex independently compares tracked discovery IDs and all protected hashes.
- [x] Run affected maintained model/knowledge/backup/browser regressions offline in an isolated path without spaces.
- [x] Save results and append the actual state record; stage only this cleanup.
- [x] Commit, push `feat/mvp-foundation`, and verify GitHub/local branch SHA equality.

Verified cleanup commit: `05c3a9ff4d95ed2037e31da443414656a326efee`. The bookkeeping commit is pushed and its HEAD rechecked before final delivery.
