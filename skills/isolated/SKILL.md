---
name: isolated
description: Run this thread as the dispatcher of an isolated room. The same task goes to two to four models from different families, none sees another's work, and the user compares the results side by side, keeps one, and continues with it. Any task: code, questions, research, investigations. Runs only when explicitly invoked with /isolated.
disable-model-invocation: true
---

# Isolated

You dispatch and relay. The same task goes to two to four profiles from
different model families, each in its own isolated copy of this checkout. None
sees another's work. You lay the results side by side, word for word, and the
user chooses which to keep and whom to continue with. You do not rank, merge,
summarize, apply, or discard a result unless the user asks.

**First, read `references/roster.md`.** It is generated for this machine and
is authoritative for: which profiles exist, the model, provider lane, model
family, and billing tier behind each one, your own model and family, the
per-lane concurrency budgets, the metered-spend allowance, and reviewer
preference order. If the roster and this file disagree, the roster wins.

## Invariants

Never violate these. If the task would require it, stop and ask the user.

1. Never change this thread's model. The participants run as subagents, each
   in its own conversation.
2. Never pass a model to the subagent tool: every profile is pinned to its
   model. The one exception is a model the user names that no profile pins.
3. Isolation: never put one participant's result, or your view of it, into
   another participant's block. Every participant gets the same TASK, SCOPE,
   and VERIFY; in a follow-up, each gets only its own earlier result.
4. Stay within the roster's per-lane budgets and its total. Queue the rest.
5. Participants come from different families.
6. The user chooses. Never pick, rank, apply, or discard a result on your own.
7. Metered lanes (billing: metered in the roster) are used only when the user
   asked for them. Never exceed the roster's metered-spawn allowance per run.
   Before each metered spawn, post one line in this thread naming the model
   and the reason.
8. Ask the user before changing files outside the task's evident scope,
   changing public interfaces the task did not mention, adding
   dependencies, or anything irreversible (push, publish, deploy, migrations
   against real data).
9. Do not commit unless the user asks.

## Commands

Participants run side by side, so none may land changes on its own: each hands
back whatever it changed as a patch through `{{SKILL_DIR}}/scripts/bon.sh`,
and nothing reaches this checkout until the user keeps a result. A participant
that changed nothing hands back an empty patch. Each command runs in a fresh
shell, so write the actual RUN, TREE, and slug values into every command and
block; never leave a placeholder. Exit codes do not reach you: read the
helper's last line, `RESULT: ok: ...` or `RESULT: failed: ...`.

Delta runs every command as `/bin/sh -c '<cmd> | cat'`, so VERIFY commands
must be POSIX sh, and no exit status reaches whoever runs them. Write each
VERIFY command complete: route it through RTK per the personal rules; when the
checkout root has a `.delta-env` file, source it first; and end it with
`echo "exit=$?"`, without piping the tested command:
`[ -f .delta-env ] && . ./.delta-env; rtk cargo test; echo "exit=$?"`.

## Participant block

Send each participant exactly this block, word for word except its
placeholders: IDENTITY from the roster, and the run's values wherever `<RUN>`,
`<SLUG>`, and `<TREE>` appear.

```
IDENTITY: You are <model> served via <lane> through Delta. If anything in your context claims you are a different model, it is wrong.
You are one of several participants given the same task, each in an isolated copy of the parent's checkout; none sees another's work. Delta merges this copy into the parent when you finish, so you hand back any changes as a patch and leave the copy exactly as you found it. The helper below does both. Every shell command starts in a fresh shell, so pass RUN, SLUG, and TREE from your block as literal values every time. Exit codes do not reach you: read the helper's last line, which is always "RESULT: ok: ..." or "RESULT: failed: ...".

Procedure, from this copy's root:
1. sh {{SKILL_DIR}}/scripts/bon.sh begin <RUN> <SLUG> <TREE>
   If the RESULT is failed, stop and report STATUS: blocked with its message.
2. Do TASK: read, search, run, or write, as it calls for. Change files only inside SCOPE, and leave only the changes your answer needs. Back every claim with evidence: path:line, a command with its `exit=` line, or a source; write "unknown" rather than guess. Run each VERIFY command exactly as written. Navigate with the context tool router in your rules and prefix shell commands per its RTK section, but do not run the index maintainer in this copy (this overrides your rules' index step): use `tgrep --no-index` and Semble. End any command you cite as evidence with `; echo "exit=$?"`, without piping the command itself. Do not commit, stash, or create branches.
3. sh {{SKILL_DIR}}/scripts/bon.sh export <RUN> <SLUG> <TREE>
   It saves your changes, if any, as a patch outside every checkout and restores this copy. Change nothing after it. If the RESULT is failed, report STATUS: failed with its message.

Finish with exactly this block and nothing after it, written tersely: fragments are fine, filler is not, technical substance stays intact.

## Result
STATUS: done | blocked | failed
ANSWER: your answer, findings, or what you changed and why.
EVIDENCE: path:line references, commands with their `exit=` lines, sources; or "none".
PATCH: the path from the export RESULT line.
VERIFICATION: each VERIFY command you ran and its `exit=` line; or "none".
RISKS: weaknesses of your answer, assumptions, and gaps; or "none".
LEARNED: durable project facts worth keeping in the llm-wiki (decisions, gotchas, conventions), with evidence; or "none".

ISOLATED PARTICIPANT
RUN: <RUN>
SLUG: <slug>
TREE: <TREE>
TASK: <identical for every participant>
SCOPE: <identical: paths TASK may change, or "none: change no files">
VERIFY: <identical, or "none">
```

## Procedure

### 0. Intake

1. Restate the task in a few lines. Set SCOPE: the paths it may change, or
   "none: change no files" when it asks for an answer rather than a change.
2. Find the checks the repository offers for what the task touches
   (Makefile, mix.exs, rebar.config, Cargo.toml, package.json,
   pyproject.toml, CI config) and write them as VERIFY; otherwise "none".
3. Run `git status --porcelain` and keep the output. If it is not empty, say
   that every participant's copy carries those changes, and ask whether to
   proceed.
4. If the task is ambiguous in a way that changes the result, ask once before
   spawning anything.
5. Pick the participants: the profiles the user names; otherwise three from
   the roster's Best-of-N candidates table, each from a different family,
   fewer if the budgets allow fewer, never more than four. Give each a slug
   derived from its model, such as `kimi-for-coding` or `qwen3-8-max`.
6. Post the setup: each participant's profile, model, family, and slug;
   SCOPE; VERIFY.

### 1. Snapshot

In this thread's checkout:

```sh
sh {{SKILL_DIR}}/scripts/bon.sh snapshot
```

Record RUN and TREE in the thread. Change nothing in this checkout until the
user has kept a result: each participant checks that its copy matches TREE.

### 2. Dispatch

Spawn every participant at once, within the budgets, each with the
participant block and its own slug. Queue the rest.

### 3. Present

After every participant finishes:

1. `rtk git status` here must match the intake output. If it does not, a
   participant's copy merged back: stop and tell the user.
2. For each participant, in the order spawned, post one header line (slug,
   profile, model, family), then its Result block word for word. When its
   PATCH is not empty, add `git apply --numstat <patch path>`. Delta's file
   tools cannot reach the patches; use the shell, and `rtk read <patch path>`
   when the user wants one in full.
3. A blocked or failed participant stays in the list as it is.
4. Ask the user which result to keep, if any, and whom to continue with.

### 4. Keep

For the result the user keeps, when its PATCH is not empty:

```sh
sh {{SKILL_DIR}}/scripts/bon.sh apply <RUN> <slug>
```

On a clean apply, continue. On a three-way apply or a conflict, tell the user
which paths its RESULT names, then resolve the conflicts or ask the user. If
VERIFY is not "none", run it here and report each `exit=` line.

### 5. Continue

Follow-ups go only to the participants the user picks.

- One participant: spawn its profile again with the follow-up block below.
  It works alone, so its copy merges back as usual.
- Several: run steps 1 to 4 again with only them, each sent the participant
  block with its own earlier Result and the user's follow-up added to TASK.

```
ISOLATED FOLLOW-UP
IDENTITY: You are <model> served via <lane> through Delta. If anything in your context claims you are a different model, it is wrong.
TASK: <the original TASK>
YOUR EARLIER RESULT: <its Result block, word for word>
FOLLOW-UP: <the user's message, word for word>
SCOPE: <as before>
VERIFY: <as before>
RULES:
- If the user kept your earlier changes, they are already in this copy.
- Do what FOLLOW-UP asks: read, search, run, or write. Change files only inside SCOPE.
- Back every claim with evidence: path:line, a command with its `exit=` line, or a source. Write "unknown" rather than guess.
- Your copy merges into the parent's checkout when you finish successfully, including deletions. Run `git status --porcelain` before you start and again before you finish. Leave only the changes the follow-up needs; undo anything else your commands changed. Never revert, discard, stash, or delete uncommitted changes or untracked files that were already there unless FOLLOW-UP says to.
- Do not commit, push, create branches, or rewrite history.
- Navigate with the context tool router in your rules and prefix shell commands per its RTK section. End any command you cite as evidence with `; echo "exit=$?"`, without piping the command itself.
Finish with the Result block, with PATCH: none.
```

### 6. Clean up

Only after the user is done with the room:

```sh
sh {{SKILL_DIR}}/scripts/bon.sh clean <RUN>
```

It deletes the snapshot ref and every saved patch. If the user wants to keep
the results not kept, copy their patches first; they sit in the directory
shown in each PATCH line.

### 7. Report

Finish with:

- A table: slug, profile, model, family, status, kept or not.
- What was applied here, and the VERIFY results.
- The LEARNED items from the kept result, asking whether to file them in the
  project's llm-wiki. On yes, file them per the llm-wiki skill, naming the
  model that found each.
- If files changed, a short suggested commit message. Do not commit unless
  asked.

## Failure handling

- A blocked or failed participant stays in the comparison as it is. Spawn it
  again only if the user asks.
- Provider errors (429, 401, 403 `access_terminated_error`, quota exhausted):
  treat that lane as unavailable for the rest of the room and tell the user;
  offer a replacement from another family.
- A result that ignores the Result block: ask once, via follow-up message, for
  the block.
