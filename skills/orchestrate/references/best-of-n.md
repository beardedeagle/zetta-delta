# Best-of-N procedure

Run two to four candidates on the same task, each on a model from a different
family: profiles from the roster's Best-of-N table, each sent the BEST-OF-N
CANDIDATE block below. Each candidate works in its own isolated copy, saves its
attempt as a patch outside every checkout, and restores its copy, so nothing
reaches this checkout until you apply the winner. Do not edit this checkout
while candidates run, and start no unit whose FILES IN SCOPE overlap the
files the task may touch: its merge would force a three-way apply.

Every step uses `{{SKILL_DIR}}/scripts/bon.sh`. Each command runs in a fresh
shell, so write the actual RUN, TREE, and slug values into every command and
assignment; never leave a placeholder. Exit codes do not reach you: read the
helper's last line, `RESULT: ok: ...` or `RESULT: failed: ...`.

## 1. Snapshot

In this thread's checkout:

```sh
sh {{BON_SH}} snapshot
```

It records tracked and untracked (non-ignored) files without touching the
real index or working tree, pins them under `refs/orchestrate/<RUN>`, and
prints RUN and TREE. Record both in the thread. Spawn the candidates before
anything changes this checkout: each one checks that its copy matches TREE.

## 2. Dispatch

Pick two to four profiles from different families in the roster's Best-of-N
candidates table. Those are all flat; a metered model may be a candidate only
when the user names it, under SKILL.md invariants 2 and 7, and it counts
against the metered-spawn allowance. Candidates count
against their lane's budget like any other spawn.

Spawn each chosen candidate-capable isolated profile once. Omit model overrides
for pinned models. For a user-named unprofiled model, follow
`references/effective-identity.md`, then use its exact selector with a
candidate-capable isolated base profile; all family, lane, billing, and review
checks use that effective record. Use a unique slug derived from the model, such as `kimi-for-coding`,
`qwen3-8-max`, `glm-5-3`, or `deepseek-v4-pro`.

Never send a candidate an ASSIGNMENT: its copy would merge back on success.
Send each candidate exactly this block, word for word except its placeholders:
IDENTITY and EFFECTIVE from the resolved record, and the run's values wherever `<RUN>`, `<SLUG>`, and
`<TREE>` appear. The procedure in it is what keeps the attempt out of this
checkout.

```
IDENTITY: You are <effective model> served via <effective provider-id> on <effective lane> through Delta. This effective identity overrides a base profile identity when the user explicitly requested the model override.
EFFECTIVE: model=<model-id>; provider=<provider-id>; family=<family>; lane=<lane>; billing=<flat|metered>; lane limit=<n>; thread limit=<n>; override=<exact selector|none>.
You work in an isolated copy of the parent's checkout. Delta merges this copy into the parent when you finish, so you hand back a patch and leave the copy exactly as you found it. The helper below does both. Every shell command starts in a fresh shell, so pass RUN, SLUG, and TREE from your assignment as literal values every time. Exit codes do not reach you: read the helper's last line, which is always "RESULT: ok: ..." or "RESULT: failed: ...".

Procedure, from this copy's root:
1. sh {{BON_SH}} begin <RUN> <SLUG> <TREE>
   If the RESULT is failed, make no task edits and follow step 4; restoration is unverified.
2. Implement TASK to satisfy ACCEPTANCE, editing this copy with your normal tools. Do not run the index maintainer in this copy (this overrides your rules' index step): use `tgrep --no-index` and Semble. Run each VERIFY command exactly as written; its closing `echo "exit=$?"` is the only way you see its status. Fix failures. Do not commit, stash, or create branches.
   Ordinary staging of modified/new/deleted working files (including `git add`)
   is supported. A working-content patch cannot represent pure index-only intent
   or untracking a file retained on disk. The helper refuses those states while
   preserving data: notify the parent and use the recovery path; do not reset the
   index or delete a retained file merely to pass export.
3. sh {{BON_SH}} export <RUN> <SLUG> <TREE>
   It saves your patch outside every checkout and restores this copy. Change no files after it.
   Before any normal report, require a final
   `sh {{BON_SH}} begin <RUN> <SLUG> <TREE>` RESULT: ok.
   For STATUS: done, also require export RESULT: ok and RESTORED_TREE equal to TREE.
   An export failure is not proof of restoration: run begin to check this copy.
   If it fails, retry export once and begin again; preserve the saved patch and
   user data. If export still fails but begin succeeds, finish with STATUS: failed
   and describe the failed export, even though restoration is verified.
4. If the initial begin or final restoration check fails, do not send a normal
   final report. Call Delta's `send_agent_message` with recipient: "parent" and
   message containing RUN, SLUG, TREE, the failed helper RESULT, this copy's path,
   and "restore unverified: stop this child with Stop Subagent and check parent
   content before continuing". This notifies the parent; it does not fail the runtime.
   Keep this child active with a terminal hold
   (`sh -c 'while :; do sleep 30; done'`) until the user stops it or restoration
   is successfully verified. If messaging is unavailable, show that notification
   in terminal output and hold. The parent pauses selection, uses the actual
   Stop Subagent control, then checks/accounts for parent content. Neither the
   hold nor stopping proves no merge. Do not reset/delete user data to make begin pass.

Finish with exactly this block and nothing after it:

## Report
STATUS: done | blocked | failed
SUMMARY: 2-5 sentences on your approach and the trade-offs you weighed.
PATCH: the path from the export RESULT line, or none if export failed.
RESTORED_TREE: <assigned TREE, only after the final begin RESULT is ok>.
VERIFICATION: each VERIFY command you ran and its `exit=` line.
RISKS: weaknesses of your attempt, assumptions, and gaps; or "none".
LEARNED: durable project facts worth keeping in the llm-wiki (decisions, gotchas, conventions), with evidence; or "none".

BEST-OF-N CANDIDATE
RUN: <RUN>
SLUG: <slug>
TREE: <TREE>
TASK: <identical for every candidate>
ACCEPTANCE: <identical for every candidate>
VERIFY: <identical for every candidate>
```

## Comparison completion decisions

The restoration check is the final begin command, including content and original
index verification. These decisions govern both task success and task failure.
Normal final completion is allowed only after restoration is positively verified.

| Export RESULT | Restoration check | Participant action | Parent action |
|---|---|---|---|
| ok | ok | finish-with-restored-tree | check-before-selection |
| failed | ok | finish-failed | check-and-exclude |
| failed | failed | notify-parent-and-hold | stop-check-and-hold |

A failed initial begin follows notify-parent-and-hold as well. On the parent's
notification, hold all selection/application/retry work and tell the user to use
Delta's actual **Stop Subagent** control for that child. After it settles, run
`sh {{BON_SH}} check <RUN> <TREE>`. Stopping is not evidence that no changes landed.
If check fails, preserve the snapshots and patches, inspect/account for the actual
partial parent changes with the user, and establish a verified baseline before
retrying or selecting. Never erase dirty user bytes automatically.

## 3. Judge

For each candidate, read its Report block, then check the patch named in its
PATCH line (Delta's file tools cannot reach it; use the shell):

```sh
git apply --numstat <patch path>   # size and spread
rtk read <patch path>              # full text, for the top two
```

After each candidate settles and again before ranking/applying, run this in the
parent checkout and require RESULT: ok:

```sh
sh {{BON_SH}} check <RUN> <TREE>
```

A failed content/index check holds selection: preserve the evidence and account
for actual changes before any further step. Status labels cannot prove unchanged
bytes in an already-dirty file. A candidate is eligible only with STATUS: done,
a PATCH line, and RESTORED_TREE exactly equal to TREE. A failed/blocked report
remains visible but is excluded; runtime completion alone is not task success.

Score in this order:

1. VERIFY passed in the candidate's worktree: every `exit=` line is 0. A failing candidate is out
   unless every candidate failed.
2. ACCEPTANCE coverage, from reading the patch.
3. Scope discipline: no unrelated edits, no gratuitous churn.
4. Risk: error handling, failure modes, concurrency, security.
5. Readability and fit with the surrounding code.

Read the top two patches in full. Post a ranking table with one line of
reasoning per candidate. If the top two are close on substance, ask the user
to choose rather than deciding on taste.

## 4. Apply the winner

```sh
sh {{BON_SH}} apply <RUN> <winner-slug>
```

The normal case is a clean apply, because nothing in this checkout should
change during a run. Otherwise it stages the patched paths and applies
three-way, using the snapshot's blobs pinned in this repository; on conflict
its RESULT lists the conflicted files. Tell the user which paths were staged,
then resolve the conflicts or ask the user.
Then run VERIFY here and continue with SKILL.md step 4, using a reviewer from
a different family than the winner's effective model; carry that record into
the unit's author ledger and reviewer block.

## 5. Clean up

Only after the user is satisfied with the result:

```sh
sh {{BON_SH}} clean <RUN>
```

It deletes the snapshot ref and every saved patch, and sweeps abandoned runs:
this checkout's runs untouched for 30 days, and any whose checkout is gone.
If the user wants to keep the losing attempts, copy the patches first; they
sit in the directory shown in each PATCH line.
