---
name: GitHub Sandbox Branch Sync
description: Keeps the local github-sandbox git clone in sync with GitHub before creating a new branch. Use whenever a task calls create_branch on the github-sandbox MCP tools, especially if a pull request may have been merged since the last sync.
---

# GitHub Sandbox Branch Sync

## Why this matters

The `github-sandbox` MCP server wraps a local git clone. `merge_pull_request`
merges a pull request directly through the GitHub REST API — it never
touches this local clone. If a branch was merged into `main` since the last
sync, the local `main` is stale, even though `git_status` on it looks
perfectly clean.

Creating a new branch from a stale `main` silently forks it from an
outdated point in history. The problem does not surface immediately — it
shows up later as a merge conflict on a pull request that has nothing to do
with the actual change being made.

## Rule

Before calling `create_branch` on the `github-sandbox` MCP tools, always do
both of the following, in order:

1. `switch_branch("main")`
2. `pull()`

Do this every time a new branch is created from `main`, not only when a
merge is suspected — the point of this rule is to not have to guess. If
`pull()` fails because history has diverged, stop and report the failure
instead of forcing a merge or continuing to branch anyway.

## When this does not apply

- Creating a branch from a branch other than `main` (e.g. branching off an
  existing feature branch): sync that specific branch first, using the
  same two-step pattern, instead of `main`.
- A second `create_branch` call later in the same task, if `main` was
  already synced this way and no merge happened in between.
