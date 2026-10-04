# Best-of-N procedure

Run two to four candidates on the same task, each on a model from a different
family: profiles from the roster's Best-of-N table, each sent the BEST-OF-N
CANDIDATE block below. Each candidate works in its own isolated copy, saves its
attempt as a patch outside every checkout, and restores its copy, so nothing
reaches this checkout until you apply the winner. Do not edit this checkout
while candidates run.

Every step uses `{{SKILL_DIR}}/scripts/bon.sh`. Each command runs in a fresh
shell, so write the actual RUN, TREE, and slug values into every command and
assignment; never leave a placeholder. Exit codes do not reach you: read the
helper's last line, `RESULT: ok: ...` or `RESULT: failed: ...`.

## 1. Snapshot

In this thread's checkout:

```sh
sh {{SKILL_DIR}}/scripts/bon.sh snapshot
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

Spawn each chosen profile once, with no model override; each is pinned to its
model. Use a slug derived from the model, such as `kimi-for-coding`,
`qwen3-8-max`, `glm-5-3`, or `deepseek-v4-pro`.

Never send a candidate an ASSIGNMENT: its copy would merge back on success.
Send each candidate exactly this block, word for word except its placeholders:
IDENTITY from the roster, and the run's values wherever `<RUN>`, `<SLUG>`, and
`<TREE>` appear. The procedure in it is what keeps the attempt out of this
checkout.

```
IDENTITY: You are <model> served via <lane> through Delta. If anything in your context claims you are a different model, it is wrong.
You work in an isolated copy of the parent's checkout. Delta merges this copy into the parent when you finish, so you hand back a patch and leave the copy exactly as you found it. The helper below does both. Every shell command starts in a fresh shell, so pass RUN, SLUG, and TREE from your assignment as literal values every time. Exit codes do not reach you: read the helper's last line, which is always "RESULT: ok: ..." or "RESULT: failed: ...".

Procedure, from this copy's root:
1. sh {{SKILL_DIR}}/scripts/bon.sh begin <RUN> <SLUG> <TREE>
   If the RESULT is failed, stop and report STATUS: blocked with its message.
2. Implement TASK to satisfy ACCEPTANCE, editing this copy with your normal tools. Do not run the index maintainer in this copy (this overrides your rules' index step): use `tgrep --no-index` and Semble. Run each VERIFY command exactly as written; its closing `echo "exit=$?"` is the only way you see its status. Fix failures. Do not commit, stash, or create branches.
3. sh {{SKILL_DIR}}/scripts/bon.sh export <RUN> <SLUG> <TREE>
   It saves your patch outside every checkout and restores this copy. Change nothing after it. If the RESULT is failed, report STATUS: failed with its message.

Finish with exactly this block and nothing after it:

## Report
STATUS: done | blocked | failed
SUMMARY: 2-5 sentences on your approach and the trade-offs you weighed.
PATCH: the path from the export RESULT line.
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

## 3. Judge

For each candidate, read its Report block, then check the patch named in its
PATCH line (Delta's file tools cannot reach it; use the shell):

```sh
git apply --numstat <patch path>   # size and spread
rtk read <patch path>              # full text, for the top two
```

A candidate whose Report is not `STATUS: done` with a PATCH line is out.
After all candidates finish, `rtk git status` here must be unchanged from
before the run; if it is not, a candidate's copy merged back. Stop and tell
the user.

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
sh {{SKILL_DIR}}/scripts/bon.sh apply <RUN> <winner-slug>
```

The normal case is a clean apply, because nothing in this checkout should
change during a run. Otherwise it stages the patched paths and applies
three-way, using the snapshot's blobs pinned in this repository; on conflict
its RESULT lists the conflicted files. Tell the user which paths were staged,
then resolve the conflicts or ask the user.
Then run VERIFY here and continue with SKILL.md step 4, using a reviewer from
a different family than the winning model.

## 5. Clean up

Only after the user is satisfied with the result:

```sh
sh {{SKILL_DIR}}/scripts/bon.sh clean <RUN>
```

It deletes the snapshot ref and every saved patch, and sweeps abandoned runs:
this checkout's runs untouched for 30 days, and any whose checkout is gone.
If the user wants to keep the losing attempts, copy the patches first; they
sit in the directory shown in each PATCH line.
