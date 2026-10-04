---
name: adversarial
description: Run this thread as the moderator of an adversarial room. A defender does the user's task or holds a position on it; a challenger from a different model family tries to break or refute it with evidence; they take turns until one gives a verdict or the round limit is reached. Any task: code, questions, research, investigations. Runs only when explicitly invoked with /adversarial.
disable-model-invocation: true
---

# Adversarial

You moderate. A defender does the user's task or holds a position on it; a
challenger from a different model family tries to break or refute that with
evidence. They take turns, each seeing every earlier turn, until one gives a
verdict or the round limit is reached. You relay, hold both sides to these
rules, and report. You do not take a side, do the task yourself, or judge the
outcome unless the user asks.

**First, read `references/roster.md`.** It is generated for this machine and
is authoritative for: which profiles exist, the model, provider lane, model
family, and billing tier behind each one, your own model and family, the
per-lane concurrency budgets, the metered-spend allowance, and reviewer
preference order. If the roster and this file disagree, the roster wins.

For a user-named model that no profile pins, read `references/effective-identity.md`
and resolve its actual provider/model/family/lane/billing/budgets before dispatch.
That effective record replaces the base profile's identity for this spawn; it does
not change the profile's role eligibility or worktree mode. Carry it into every
retry, review, and follow-up. Unknown metadata blocks dispatch until resolved.

## Invariants

Never violate these. If the task would require it, stop and ask the user.

1. Never change this thread's model. The sides run as subagents, each in its
   own conversation.
2. Never pass a model to the subagent tool: every profile is pinned to its
   model. The one exception is a model the user names that no profile pins;
   resolve its effective identity first and use its exact qualified selector.
3. One turn at a time. Never run both sides at once. Spawn every turn fresh,
   never as a follow-up message, so its copy starts from this checkout with
   every earlier turn's changes in it.
4. Stay within the roster's per-lane budgets and its total.
5. The defender and the challenger come from different families.
6. At most the round limit: 5 unless the user sets another. A round is one
   defender turn followed by one challenger turn.
7. Metered lanes (billing: metered in the roster) are used only when the user
   asked for them. Never exceed the roster's metered-spawn allowance per run.
   Before each metered spawn, post one line in this thread naming the model
   and the reason.
8. Ask the user before changing files outside the task's evident scope,
   changing public interfaces the task did not mention, adding
   dependencies, or anything irreversible (push, publish, deploy, migrations
   against real data).
9. Do not commit unless the user asks.

## Turn block

Send every turn exactly this block, with every field filled in. Copy RULES
word for word: you may add rules, never drop or reword them. Fill IDENTITY
from the effective record (the roster row when there is no override), and SIDE with the line for
that side.

Delta runs every command as `/bin/sh -c '<cmd> | cat'`, so VERIFY commands
must be POSIX sh, and no exit status reaches whoever runs them. Write each
VERIFY command complete: route it through RTK per the personal rules; when the
checkout root has a `.delta-env` file, source it first; and end it with
`echo "exit=$?"`, without piping the tested command:
`[ -f .delta-env ] && . ./.delta-env; rtk cargo test; echo "exit=$?"`.

```
ADVERSARIAL ROUND <n> <DEFENDER | CHALLENGER>
IDENTITY: You are <effective model> served via <effective provider-id> on <effective lane> through Delta. This effective identity overrides a base profile identity when the user explicitly requested the model override.
EFFECTIVE: model=<model-id>; provider=<provider-id>; family=<family>; lane=<lane>; billing=<flat|metered>; lane limit=<n>; thread limit=<n>; override=<exact selector|none>.
TASK: <the user's task, word for word>
SCOPE: <paths the task may change; or "none: change no files">
VERIFY: <commands that check the work; or "none">
SIDE: <one of the two lines below>
  DEFENDER: Do TASK, or hold your answer to it, as well as it can be done. Answer every point still standing from the challenger's last turn: fix it, refute it with evidence, or concede it.
  CHALLENGER: Break or refute the defender's work or answer, with evidence: a failing check, a counterexample, contradicting code or sources. Re-check your points still standing first; withdraw any the defender has answered.
TRANSCRIPT: <every earlier Turn report in this room, oldest first, word for word; "none" for the first turn>
RULES:
- Do whatever TASK and your side call for: read, search, run, write. Change files only inside SCOPE.
- Back every claim with evidence: path:line, a command with its `exit=` line, or a source. Write "unknown" rather than guess.
- Your copy merges into the parent's checkout when you finish successfully, including deletions. Run `git status --porcelain` before you start and again before you finish. Leave only the changes your side's work needs; undo anything else your commands changed.
- The other side's changes from earlier turns, listed under CHANGES in TRANSCRIPT, are its work or evidence: do not undo or weaken them; dispute them in your report. You may revise your own. Uncommitted changes and untracked files that predate the room are the user's work: change them only as TASK requires, and never revert, discard, stash, or delete them.
- Do not commit, push, create branches, or rewrite history.
- Navigate with the context tool router in your rules and prefix shell commands per its RTK section. End any command you cite as evidence with `; echo "exit=$?"`, without piping the command itself.

Finish with exactly this report and nothing after it, written tersely: fragments are fine, filler is not, technical substance stays intact.

## Turn
STATUS: done | blocked
SIDE: DEFENDER | CHALLENGER, round <n>
POSITION: what you did or now hold, in 2-6 sentences.
POINTS: one per line, with evidence. Defender: each point still standing from the challenger's last turn and how you answered it (fixed, refuted, conceded). Challenger: each break or refutation still standing, new or carried over, and each you withdraw. Or "none".
CHANGES: one line per path you changed and why; or "none".
VERIFICATION: each VERIFY command you ran and its `exit=` line; or "none".
VERDICT: one of
  CONTINUE: the other side answers next.
  ACCEPT (challenger only): nothing is left standing; the defender's work or answer holds.
  CONCEDE (defender only): the work or answer does not hold; POSITION says why.
  AGREE (either side): both sides now hold the same position, stated in POSITION.
LEARNED: durable project facts worth keeping in the llm-wiki (decisions, gotchas, conventions), with evidence; or "none".
```

A verdict the side may not give counts as CONTINUE.

## Procedure

### 0. Intake

1. Restate the task in a few lines. Set SCOPE: the paths it may change, or
   "none: change no files" when it asks for an answer rather than a change.
2. Find the checks the repository offers for what the task touches
   (Makefile, mix.exs, rebar.config, Cargo.toml, package.json,
   pyproject.toml, CI config) and write them as VERIFY; otherwise "none".
3. Run `git status --porcelain` and keep the output: it is the user's work
   that predates the room. If it is not empty and SCOPE is not "none", ask
   whether to proceed with those changes included.
4. If the task is ambiguous in a way that changes the result, ask once before
   spawning anything.
5. Pick the sides from the profiles whose roster roles include worker or
   reviewer; any of them may take either side. If the user names another
   profile, say it cannot take part and why. A user-named unprofiled model
   uses an eligible base profile with its resolved effective identity and exact
   selector. Take the eligible profiles or resolved models the user names; otherwise the built-in Worker defends, and the first entry in the
   roster's reviewer preference order whose family differs from the
   defender's challenges.
6. Post the setup: each side's profile, model, and family; the round limit;
   SCOPE.

### 1. Rounds

For each round, up to the limit:

1. Spawn the defender with its Turn block. CONCEDE or AGREE ends the room.
2. Spawn the challenger with its Turn block. ACCEPT or AGREE ends the room.

After each turn, post one line: round, side, verdict, changed paths. If
`rtk git status` shows a change outside SCOPE, or a turn undid the other
side's changes, stop and tell the user. STATUS blocked: read the reason, then
answer it from the task or ask the user. If the user writes in this thread
during the room, let the turn in flight finish, then stop and ask how to
proceed.

### 2. Report

Finish with:

- How it ended: the verdict and round, or the round limit.
- Each side's final POSITION, word for word.
- Points settled and points still standing, from the last two reports.
- Changes now in this checkout, from `rtk git status` against the intake
  output, and which side made each. If VERIFY is not "none", run it here and
  report each `exit=` line.
- No winner, unless the user asks for your judgment.
- The LEARNED items the outcome confirms, asking whether to file them in the
  project's llm-wiki. On yes, file them per the llm-wiki skill, naming the
  model that found each.
- If files changed, a short suggested commit message. Do not commit unless
  asked.

## Failure handling

- Task STATUS blocked differs from runtime Failed or Stopped. A normally
  completed blocked turn may have landed partial edits. After every turn inspect
  the actual parent changes and account for them in CHANGES/TRANSCRIPT before
  another turn or retry; never assume a textual status suppresses merge.
- On runtime Failed or Stopped, likewise inspect/account for parent changes first.
  Retry that turn once with the same effective model/profile and the actual
  updated transcript/baseline. If it fails again, stop and report retained work.
- Provider errors (429, 401, 403 `access_terminated_error`, quota exhausted):
  treat that lane as unavailable for the rest of the room, move that side to
  another eligible profile whose family still differs from the other side's,
  tell the user, and repeat the turn.
- A turn that ignores the Turn report: ask once, via follow-up message, for
  the report.
