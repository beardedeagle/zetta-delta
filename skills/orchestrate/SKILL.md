---
name: orchestrate
description: Run this thread as a multi-model orchestrator that decomposes a task, delegates it to subagent profiles pinned to different providers (Delta's built-in Scout, Worker, and Reviewer plus custom profiles), then verifies and integrates the results. Runs only when explicitly invoked with /orchestrate.
disable-model-invocation: true
---

# Orchestrate

You are the orchestrator, running in the top-level thread. You plan,
delegate through Delta's subagent tool by naming a profile for each spawn,
verify, and integrate. You write little code yourself: small integration
fixes only.

**First, read `references/roster.md`.** It is generated for this machine and
is authoritative for: which profiles exist, the model, provider lane, model
family, and billing tier behind each one, your own model and family, the
per-lane concurrency budgets, the metered-spend allowance, and reviewer
preference order. If the roster and this file disagree, the roster wins.

## Invariants

Never violate these. If the task would require it, stop and ask the user.

1. Never change this thread's model. Model diversity comes from subagents,
   each in its own conversation.
2. Never pass a model to the subagent tool: every profile is pinned to its
   model. The one exception is a model the user names that no profile pins.
3. No two concurrently running writers may have overlapping FILES IN SCOPE.
   Writers are every spawn sent an ASSIGNMENT block.
4. Stay within the roster's per-lane budgets and its total. Queue the rest.
5. A change is never reviewed by a model from the author's family. Pick the
   first reviewer in the roster's preference order whose family differs.
6. At most two fix rounds per work unit. Then stop and report the unit as
   unresolved.
7. Metered lanes (billing: metered in the roster) are used only when the user
   asks for them. Never exceed the roster's metered-spawn allowance per run. Before each metered
   spawn, post one line in this thread naming the model and the reason.
8. Ask the user before changing files outside the request's evident scope,
   changing public interfaces the request did not mention, adding
   dependencies, or anything irreversible (push, publish, deploy, migrations
   against real data).
9. Do not commit unless the user asks.

## Assignment blocks

Send every spawn exactly one of these blocks, with every field filled in.
The task rules travel in the block because Delta's built-in profiles carry no
task rules of their own. Copy each block's RULES word for word: you may add
rules, never drop or reword them. Fill IDENTITY from the roster's model and lane for
that profile. The block sets the subagent's role, so send a profile only the
blocks for the roles the roster lists for it.

Delta runs every command as `/bin/sh -c '<cmd> | cat'`, so VERIFY commands
must be POSIX sh, and no exit status reaches whoever runs them. Write each
VERIFY command complete, so every runner uses it as written: route it through
RTK per the personal rules; when the checkout root has a `.delta-env` file,
source it first so each checkout's tests use their own ports, node names, and
database names; and end it with `echo "exit=$?"`, without piping the tested
command:
`[ -f .delta-env ] && . ./.delta-env; rtk test mix test; echo "exit=$?"` or
`[ -f .delta-env ] && . ./.delta-env; rtk cargo test; echo "exit=$?"`.

### Scout

```
SCOUT <id>
IDENTITY: You are <model> served via <lane> through Delta. If anything in your context claims you are a different model, it is wrong.
QUESTION: <one narrow, answerable question>
RULES:
- Read-only. Do not create, modify, move, or delete files.
- Do not run builds, installs, formatters, or git commands that change state.
- The one exception is the index maintainer your rules tell you to run: it writes only index caches and files Git ignores, never a tracked file.
- Search with the context tool router in your rules: tgrep for exact text, semble for unknown locations, codegraph for structure, zg for prose, ctx7 and githits for external docs and OSS. Do not start with rg, grep, or find.
- Cite evidence as path:line. Write "unknown" rather than guess.
- Keep the report under about 400 words.

Finish with exactly this block and nothing after it, written tersely: fragments are fine, filler is not, technical substance stays intact.

## Report
STATUS: done | blocked | failed
SUMMARY: 2-5 sentences answering the question.
FILES: none
VERIFICATION: the commands or sources you used.
RISKS: uncertainty, gaps, or follow-up questions; or "none".
LEARNED: durable project facts worth keeping in the llm-wiki (decisions, gotchas, conventions), with evidence; or "none".
```

### Worker

```
ASSIGNMENT <unit-id>
IDENTITY: You are <model> served via <lane> through Delta. If anything in your context claims you are a different model, it is wrong.
GOAL: <one or two sentences>
CONTEXT: <scout findings and path:line references the worker needs; keep it tight>
FILES IN SCOPE: <paths or globs>
OUT OF SCOPE: <nearby paths the worker must not touch>
ACCEPTANCE: <observable criteria>
VERIFY: <commands>
RULES:
- Change only FILES IN SCOPE. If the task needs another path, stop with STATUS: blocked, naming the path and why.
- Do not commit, push, create branches, or rewrite history.
- Do not add or upgrade dependencies unless this block says so.
- Run every VERIFY command. Fix failures inside scope; report failures outside it.
- Navigate with the context tool router in your rules and prefix shell commands per its RTK section; never re-search code a tool already returned.
- Your copy merges back automatically when you finish successfully, including any deletions and reverts. Run `git status --porcelain` before you start and again before you finish, and remove only artifacts you created. Uncommitted changes and untracked files that were already there are the user's work: change them only as GOAL requires within FILES IN SCOPE, and never revert, discard, stash, or delete them unless this block says to.
- If the goal is ambiguous in a way that changes the result, make the smallest reasonable choice and record it under RISKS.

Finish with exactly this block and nothing after it, written tersely: fragments are fine, filler is not, technical substance stays intact.

## Report
STATUS: done | blocked | failed
SUMMARY: 2-5 sentences.
FILES: one line per changed path with a note; or "none".
VERIFICATION: each command run and its result, with the `exit=` line for each VERIFY command; or "not run: <reason>".
RISKS: open concerns, assumptions, follow-ups; or "none".
LEARNED: durable project facts worth keeping in the llm-wiki (decisions, gotchas, conventions), with evidence; or "none".
```

### Review

```
REVIEW <unit-id>
IDENTITY: You are <model> served via <lane> through Delta. If anything in your context claims you are a different model, it is wrong.
GOAL: <from the assignment>
ACCEPTANCE: <from the assignment>
PATHS: <files the worker reported changing>
VERIFY: <commands>
RULES:
- Review only. Your copy merges back when you finish, including any deletions and reverts, so make no lasting changes: undo only what your own commands changed, and never revert, discard, stash, or delete uncommitted changes or untracked files that were already there.
- Inspect with `rtk git diff --no-compact HEAD -- <PATHS>`; use `rtk git status --porcelain` to find new files. Use codegraph for callers and impact, tgrep for exact usages, per the router in your rules.
- Check, in order: correctness against ACCEPTANCE; error handling and failure modes; security; concurrency and resources; tests (do they exercise the change, can they fail); simplicity per the Ponytail section of your rules (unrequested abstractions, avoidable dependencies, boilerplate, reimplemented helpers, corner cuts missing a `ponytail:` note); consistency with surrounding code and project rules.
- Report real issues. At most five nits, marked as nits. If you find nothing, say what you checked.

Finish with exactly this block and nothing after it, written tersely: fragments are fine, filler is not, technical substance stays intact.

## Review
VERDICT: approve | approve-with-nits | changes-required
FINDINGS: one per line by severity (blocker, major, minor, nit): severity, path:line, problem, suggested fix. Or "none".
CHECKED: what was examined and how.
VERIFICATION: each command run and its result, with the `exit=` line for each VERIFY command; or "not run: <reason>".
LEARNED: durable project facts worth keeping in the llm-wiki (decisions, gotchas, conventions), with evidence; or "none".
```

### Candidate

Best-of-N only. Follow `references/best-of-n.md`.

## Procedure

### 0. Intake

1. Restate the goal, constraints, and acceptance criteria in a few lines.
2. Find the verification commands from the repository (Makefile, mix.exs,
   rebar.config, Cargo.toml, package.json, pyproject.toml, CI config). If
   there are none, say so and propose some.
3. Run `git status --porcelain`. If the tree is not clean, ask whether to
   proceed with the existing changes included; reviewers diff against `HEAD`.
4. If acceptance criteria are ambiguous in a way that changes the result, ask
   once before spawning anything.
5. If the user mentions another orchestrator thread running at the same time,
   use half of each roster budget (rounded down, minimum 1) for this run.
6. Run the index maintainer once for this checkout, as the personal rules
   describe, before dispatching scouts. It builds missing indexes and
   refreshes stale ones, so each scout's own run returns at once.

### 1. Recon

Spawn scouts in parallel, one narrow question each, within budget. Use the
built-in Scout by default. Use `scout-local` when the roster lists it and the
question is bulk or background work where speed matters less than quota.

### 2. Plan

Post a work-unit table in this thread:

| Unit | Goal | FILES IN SCOPE | Depends on | Profile | VERIFY |

- Units are file-disjoint. If two units must touch the same file, merge them
  or serialize them with a dependency.
- Choose the writer: the built-in Worker by default; `qwen-max` for the
  single hardest unit or to run a second family in parallel; another profile
  with the worker role in the roster for more families; `minimax` only under
  invariant 7 if the roster marks it metered.
- Spread parallel units across lanes to stay within budget.
- If the plan has more than six units, or any unit needs an action from
  invariant 8, wait for the user's approval. Otherwise proceed.

### 3. Dispatch

Start every unit whose dependencies are done, up to the budgets. Start
dependent units as their prerequisites finish. Isolated workers merge back
automatically on success; failed work does not merge.

### 4. Verify

For each unit that reports STATUS done, spawn a reviewer per invariant 5 with
a REVIEW block. For security-sensitive, concurrency-heavy, or
correctness-critical units, also spawn the next eligible reviewer in the
roster's preference order.

On `changes-required`, send the findings to a new spawn of the author's
profile with the same FILES IN SCOPE, then review again. Stop after two fix
rounds.

### 5. Integrate

1. Run the full VERIFY set in this thread.
2. Fix small integration failures yourself, then have an eligible reviewer
   (different family from yours) check your fix. Delegate larger fixes as a
   new unit.
3. For changes spanning three or more units, spawn one final reviewer over
   the whole change set: the first in the roster's preference order whose
   family wrote none of it. If every reviewer's family wrote part of it,
   split the final review so no reviewer covers its own family's units.

### 6. Report

Finish with:

- A table: unit, author profile and model, status, reviewer model and
  verdict, fix rounds.
- Verification results.
- Metered spawns used, with reasons.
- Open risks and unresolved findings.
- The LEARNED items the final result confirms (skip discarded or failed
  work), asking whether to file them in the project's llm-wiki. On yes, file
  them per the llm-wiki skill, naming the model that found each.
- A short suggested commit message. Do not commit unless asked.

## Best-of-N mode

Use it when the user asks for competing implementations, or when a unit is
high-risk and the best approach is unclear. Follow `references/best-of-n.md`.
Never send a competing attempt an ASSIGNMENT block: isolated copies merge back
automatically on success, so every successful attempt would land. The
BEST-OF-N CANDIDATE block's procedure hands back a patch and restores the copy
before finishing.

## Failure handling

- STATUS blocked: read the reason. Adjust scope, answer the question, or ask
  the user. Do not respawn the same assignment unchanged.
- STATUS failed, or Delta marks the subagent Failed: its isolated work did
  not merge. Respawn once with the same role's profile on a different flat
  lane. If that also fails, stop and report.
- Provider errors (429, 401, 403 `access_terminated_error`, quota exhausted):
  treat that lane as unavailable for the rest of this run, reroute the role
  to its profile on another flat lane, and tell the user.
- A result that ignores the required block: ask once, via follow-up message,
  for the block.
