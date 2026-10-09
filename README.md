# zetta-delta: multi-model orchestration for Delta

Run one Delta thread as an orchestrator that hands work to subagents on
different providers, keeps each provider within its limits, has every change
reviewed by a different model family, and spends per-token money only when
you allow it.

For a fresh machine with Delta and Homebrew installed, quit Delta, clone this
repository, then run:

```bash
./bootstrap.sh --dry-run
./bootstrap.sh --force
```

Bootstrap installs build prerequisites and GitHub CLI from `Brewfile`, builds the
pinned context tools, and installs the bundle with `--configure-delta`. Its defaults
match this machine's accounts: Z.AI Max, Qwen Pro, MiniMax plan, ChatGPT/Grok
subscriptions, and an 8-agent thread limit (16 overall). Override the documented
environment variables for different plans; concurrency never grants extra quota.
Set `GPT_PROVIDER=`, `GROK_PROVIDER=`, or `MINIMAX_PROVIDER=` to disable those
optional lanes. Existing files are backed up before forced replacement.

The bundled provider catalog contains only public endpoints, API modes, and
allowlisted model capabilities. Credentials, passwords, authorization headers,
account identifiers, and local paths are excluded. Existing local credentials and
unrelated settings survive configuration; settings and their backups are private
(mode 0600). Settings symlinks are saved separately as `settings.json.link`, with
an independent content snapshot at `settings.json` when the target exists; the
original target is untouched. Authenticate providers and subscription accounts in
Delta, and run `gh auth login` locally for PR targets. Start Delta and verify a real review before
treating the setup as operational; offline installation does not prove dispatch.

The installer adds:

- One custom subagent profile per model, pinned to its model and thinking
  level; the task block sets its role (worker, reviewer, or best-of-N
  candidate). The orchestrator passes no model unless you name one, and the
  install fits Delta's limit of 7 custom profiles (section 3).
- The `/orchestrate` skill, with a roster of models, lanes, budgets, and
  reviewer order generated for your accounts.
- `/adversarial` and `/isolated`: two more ways to run a thread, for any
  task (section 8).
- `/pr-review`: independent PR or commit-range review across configured model
  families, followed by cross-family vetting and explicit orchestrator adjudication
  of every finding (section 8).
- `/pr-review-batch`: one strong reviewer per PR, orchestrator adjudication, and
  explicitly authorized GitHub reviews for a PR list or all open PRs needing
  review at their current head (section 8).
- The context router and Ponytail rules, written into Delta's Personal
  AGENTS.md (section 6).
- `/find-docs`: the Context7 CLI documentation lookup skill.

`stack/install.sh` builds and installs the context tools those rules route to
(section 9), and `examples/agents-prepare.sh` gives each checkout its own
ports and test names (section 5).

## How it works

There is no orchestrator profile, because in Delta the orchestrator is not
a subagent. It is the top-level thread you are typing into.

Three pieces cooperate:

| Piece | What it is | Where it lives |
|---|---|---|
| **The thread** | The orchestrator. Its agent owns your conversation and Delta's subagent tool. | A normal Delta thread, model chosen before the first message |
| **The skill** | The orchestrator's job description: procedure, invariants, task blocks, budgets | `~/.agents/skills/orchestrate/` |
| **The profiles** | The roles it can delegate to, each pinned to a model | Built-in Scout/Worker/Reviewer, plus `<delta config>/profiles/*.toml` |

What happens when you type `/orchestrate <task>`:

1. Delta attaches `SKILL.md` to your message. The skill is marked
   `disable-model-invocation: true`, so it only ever loads when you invoke it.
2. The thread's own agent (for example `k3`) reads it, then reads
   `references/roster.md`: the models, lanes, budgets, and reviewer order the
   installer generated for your accounts.
3. It plans, then calls Delta's subagent tool once per unit of work, naming a
   profile (`worker`, `qwen-max`, `reviewer`, ...) and sending a filled-in
   task block that carries the rules.
4. Delta runs each subagent in its own conversation and worktree, on the
   profile's model, and returns its final report to the thread.
5. The thread reviews, integrates, and reports back to you.

```mermaid
flowchart TD
  U[You: /orchestrate task] --> O["Top-level thread<br/>k3 (Kimi Code)<br/>+ SKILL.md + roster.md"]
  O -->|SCOUT block| S["Scout (built-in)<br/>glm-5.3-flash (Z.AI)"]
  O -->|SCOUT block| SQ["scout-qwen<br/>qwen3.8-flash (Qwen plan)"]
  O -->|ASSIGNMENT| W["Worker (built-in)<br/>k3 (Kimi Code)"]
  O -->|ASSIGNMENT| WQ["qwen-max<br/>qwen3.8-max (Qwen plan)"]
  W -->|merges back on success| O
  WQ -->|merges back on success| O
  O -->|REVIEW block| R["Reviewer (built-in)<br/>glm-5.3 (Z.AI)"]
  O -->|REVIEW block| RD["deepseek-pro<br/>deepseek-v4-pro (Qwen plan)"]
  R -->|verdict| O
  RD -->|verdict| O
  O --> F[Report to you]
```

Delta's built-in Scout, Worker, and Reviewer keep Delta's own tuning; the installer
pins their models (section 4). Every installed profile also gets a compact shared
context-stack preamble in its additive `prompt` field. Basic routing and RTK use
therefore travel with the profile even when a parent omits task rules. The skill's
task blocks supply the assignment-specific rules: terse reports, a simplicity check in reviews,
and never reverting, discarding, stashing, or deleting uncommitted work that
was there before the task unless told to. Custom profiles add more scout
families, more model families on other lanes, and the local lane. Each custom
model's profile works, reviews, or competes in best-of-N as its task block
says; the built-in Worker and Reviewer serve as best-of-N candidates the same
way.

## Contents

| Path | Installed to | Installed when |
|---|---|---|
| `profiles/scout-qwen.toml.tmpl` | `<delta config>/profiles/` | Always |
| `profiles/scout-deepseek.toml.tmpl` | same | Always |
| `profiles/model.toml.tmpl` | same, as `<name>.toml`, one per model | `qwen-max`, `deepseek-pro` always; `minimax` when `MINIMAX_PROVIDER` is set; `gpt-sol` and `gpt-astra` when `GPT_PROVIDER` is set; `grok` when `GROK_PROVIDER` is set |
| `profiles/scout-gemini.toml.tmpl` | same | `COPILOT_PROVIDER` set |
| `profiles/scout-local.toml.tmpl` | same | `LOCAL_PROVIDER` and `LOCAL_MODEL` set |
| `skills/orchestrate/SKILL.md` | `~/.agents/skills/orchestrate/` | Always |
| `skills/orchestrate/references/best-of-n.md` | same `references/` | Always |
| `skills/orchestrate/scripts/bon.sh` | same `scripts/`, and `~/.agents/skills/isolated/scripts/` | Always; best-of-N snapshot, patch handoff, apply, cleanup |
| `skills/orchestrate/scripts/identity.py` and `references/effective-identity.md` | each installed skill's `scripts/` and `references/` | Always; resolve effective model overrides from sanitized local metadata |
| `references/identity-registry.json` | each installed skill's `references/` | Generated provider lanes, billing/budgets, and exact model-family bindings |
| `skills/adversarial/SKILL.md` | `~/.agents/skills/adversarial/` | Always |
| `skills/isolated/SKILL.md` | `~/.agents/skills/isolated/` | Always |
| `skills/pr-review/SKILL.md` | `~/.agents/skills/pr-review/` | Always; includes its own generated roster, identity registry, resolver, and identity reference |
| `skills/pr-review-batch/SKILL.md` and `references/notes.md` | `~/.agents/skills/pr-review-batch/` | Always; includes its own generated roster, identity registry, resolver, and identity reference |
| `skills/pr-review/references/{repositories,review-publication,review-voice,review-voice-sources}.md` | both PR review skills' `references/` | Always; repository resolution, shared submission contract, derived voice, and source coverage |
| `skills/find-docs/SKILL.md` | `~/.agents/skills/find-docs/` | Always; current developer documentation through Context7 |
| `tests/bon-test.sh` | Not installed | Regression check for `bon.sh`: `sh tests/bon-test.sh` |
| `tests/install-test.sh` | Not installed | Regression check for the installer's curl mode, refusals, backups, the Personal AGENTS.md update, and `--clean`: `sh tests/install-test.sh` |
| `tests/prepare-test.sh` | Not installed | Regression check for `examples/agents-prepare.sh`: `sh tests/prepare-test.sh` |
| `references/roster.md` | each skill's `references/` | Generated by the installer, the same copy in each skill |
| `rules/personal-AGENTS.md` | Not installed | Source of the router block in Personal AGENTS.md |
| `rules/subagent-context.md` | Additive `prompt` in every installed profile, including `worker.toml`, `reviewer.toml`, and `scout.toml` | Always; shared compact router/RTK preamble, refreshed idempotently |
| Router and Ponytail blocks | `~/.config/delta/AGENTS.md` (Settings > Rules > Personal AGENTS.md) | Generated on every run; replaces only those two blocks and keeps the rest of the file |
| `examples/agents-prepare.sh` | your project's `.agents/prepare` | By hand, per project |
| `stack/install.sh` | Not installed | Run by hand; builds and installs the context tools (section 9) |
| `stack/patches/<tool>/` | Not installed | Applied in order to each tool's pinned upstream release |
| `stack/context-indexes/ensure-context-indexes.py` | `~/.local/share/zetta-delta/` | By `stack/install.sh`; tests: `python3 -m unittest` in that folder |
| `stack/zvec-grep/verify-install.py` | `~/.local/share/zvec-grep/` | By `stack/install.sh`, pinned to the zvec-grep it installs; tests: `python3 -m unittest` in that folder |
| `deploy.env.example` | Not installed | Template for `deploy.env` (ignored by git), sourced before `install.sh` to repeat an install |
| `bootstrap.sh` and `Brewfile` | Not installed | Fresh-machine prerequisites, pinned context tools, and configured bundle installation |
| `settings/provider-catalog.json` and `scripts/setup.py` | Not installed | Credential-free provider metadata and local settings/built-in rendering |
| `rules/ponytail/` | Not installed | Bundled public Ponytail 4.13.0 rules, attribution, and MIT license; used when no local plugin exists |
| `install.sh` | Not installed | Run by hand (section 3) |

The Delta config directory is the folder containing `settings.json`:
`~/Library/Application Support/delta` on macOS.

## 1. Prerequisites

Use `bootstrap.sh` for a fresh setup. Direct `install.sh` requires Python 3.11+
and the runtime commands it checks (`git`, `gh`, `rtk`, and the context CLIs).
Without `--configure-delta`, initialize Delta's `settings.json` and configure
provider entries first. With that flag, the installer can create settings and
merge the bundled provider/model metadata, preserving local credentials.

Authenticate each provider in Delta, with `interleaved_reasoning` on for
every model and the output limits configured. For the Z.AI Coding Plan, use
the OpenAI-compatible base URL `https://api.z.ai/api/coding/paas/v4` and confirm
Delta is on Z.AI's supported-tools list; the plan is limited to those tools.

## 2. Find your provider ids

Profiles reference models as `<provider-id>/<model-id>`. A custom provider's
id is `custom:` followed by the SHA-256 of its base URL exactly as stored in
`settings.json`. Print every custom provider's id with:

```bash
python3 - <<'EOF'
import hashlib, json, os
d = json.load(open(os.path.expanduser("~/Library/Application Support/delta/settings.json")))
for p in d["native"]["custom_providers"]:
    print(f'{p["name"]:24} custom:{hashlib.sha256(p["base_url"].encode()).hexdigest()}')
EOF
```

Changing a provider's base URL changes its id; rerun the installer with
`--force` afterwards.

Delta's built-in subscription providers have fixed ids: `openai-subscribed`
(ChatGPT) and `x_ai-subscribed` (Grok).

## 3. Install

### With curl

Pin a commit: the URL fetches that commit's `install.sh`, and
`ZETTA_DELTA_REF` makes it fetch the same commit's files. The installer
refuses to fetch without it rather than take whatever `main` holds. The
latest commit is the first field of
`git ls-remote https://github.com/beardedeagle/zetta-delta main`.

```bash
ref=<commit>
curl -fsSL "https://raw.githubusercontent.com/beardedeagle/zetta-delta/$ref/install.sh" \
  | ZETTA_DELTA_REF=$ref KIMI_PROVIDER=<id> ZAI_PROVIDER=<id> QWEN_PROVIDER=<id> \
    ZAI_TIER=<tier> QWEN_TIER=<tier> bash -s -- --dry-run
```

Drop `--dry-run` to install, and add `--force` when reinstalling. Without
`--force`, the installer changes nothing if any file it would write exists,
and names them all. With it, each file it changes is first saved under
`~/.local/state/zetta-delta/backups/<UTC time>/`, at its full path. Settings
can also be exported first (the variables below); then the pipe needs only
`ZETTA_DELTA_REF=$ref bash -s -- <flags>`. To read the script before it
runs, save it and run the copy:

```bash
curl -fsSLo install.sh "https://raw.githubusercontent.com/beardedeagle/zetta-delta/$ref/install.sh"
less install.sh
ZETTA_DELTA_REF=$ref bash install.sh --dry-run
```

The script runs nothing if the download is cut short, and removes its
temporary copy of the repository when it exits.

### From a clone

```bash
export KIMI_PROVIDER=<id> ZAI_PROVIDER=<id> QWEN_PROVIDER=<id>
export ZAI_TIER=<tier> QWEN_TIER=<tier>          # your real tiers
# Optional lanes:
export MINIMAX_PROVIDER=<id> MINIMAX_BILLING=plan  # or metered
export GPT_PROVIDER=openai-subscribed              # ChatGPT subscription
export GROK_PROVIDER=x_ai-subscribed               # Grok subscription
export COPILOT_PROVIDER=<id>                       # GitHub Copilot
export LOCAL_PROVIDER=<id> LOCAL_MODEL=<served model id> LOCAL_FAMILY=GLM

./install.sh --dry-run          # prints every file and the full roster
./install.sh --prune-legacy     # add --force when reinstalling
```

`--configure-delta` also sets delegation to Only When Asked, model overrides on,
and concurrency to `THREAD_CAP` per thread and twice that overall. It derives
required provider IDs from the bundled public URLs when not supplied. A conflicting
endpoint override is refused before writes; custom endpoints can use direct
installation after configuration in Delta. Quit Delta before either installer runs.

Built-in effort defaults to `high` only when the exact provider/model identity in
the bundled catalog supports it; other models remain unpinned. Set
`BUILTIN_{SCOUT,WORKER,REVIEWER}_EFFORT` to a supported value (including `on`/`off`),
or to an empty string to omit the pin. Unsupported known-model values fail before
writes. Generated desktop rules retain the resolved directories of required tools
and available Node/build/Homebrew runtimes, rather than the entire installer PATH.

To repeat an install, keep the settings in a file: copy
`deploy.env.example` to `deploy.env` (git ignores it), fill it in, and run
`. ./deploy.env && ./install.sh --force`.

`--prune-legacy` renames profiles from earlier releases of this repository to
`*.toml.retired`: `scout-fast`, `scout-deep`, `worker-kimi`, and
`reviewer-glm`, which the built-ins replace, and the role profiles
`worker-*`, `reviewer-*`, and `candidate*`, which the per-model profiles
replace. Run
`./install.sh --help` for every tuning variable, including overrides if you
pin the built-ins to models other than the defaults below.

Delta's spawn tool offers at most 10 profiles: the three built-ins, then
custom profiles in alphabetical order, dropping the rest without a word.
The installer counts every other `.toml` already in the profiles folder,
installs at most the remaining of 7 custom slots, prioritizing primary model
families and optional Gemini/local scouts before duplicate models/scouts
(`./install.sh --help`; `PROFILE_PRIORITY` puts named profiles first), names
what it skipped and what takes the slots, and writes only installed profiles
into the roster. On `--force`, earlier managed selections that are no longer
chosen are backed up and retired as `.toml.retired`, so reprioritizing does not
leave active leftovers above the cap. Without force, required retirements are
reported and refused before writes. Duplicate priorities are refused.

## 4. Delta settings

**Settings > Subagents**

| Setting | Value |
|---|---|
| Enable Sub-agents | Only When Asked |
| Max Agents Per Thread | The installer's `THREAD_CAP` (default 15), which the roster uses as its all-lanes budget |
| Max Agents Overall | Twice that (30 at the default); provider-lane budgets still apply across threads |
| Allow model overrides | On (lets you name a model no profile pins; the skill passes none otherwise) |
| Scout model | `glm-5.3-flash` (Z.AI Coding Plan), effort high |
| Worker model | `k3` (Kimi Code), effort high |
| Reviewer model | `glm-5.3` (Z.AI Coding Plan), effort high |

Set these three in Settings > Subagents > Profiles (Delta saves them as
`worker.toml`, `scout.toml`, and `reviewer.toml` in the profiles folder).
The installer now pins these three files automatically, preserving unrelated
settings in supported TOML layouts and backing up changed files under `--force`.
The table is a verification reference; `--configure-delta` sets the other controls.
Left at "Same as Parent" or "Provider Default", they resolve to the thread's
model, so a Kimi thread's Reviewer would be Kimi reviewing Kimi.
The built-in models must match the roster; if you choose others, pass the
`BUILTIN_*` variables to the installer so the roster stays truthful. Each role
accepts `MODEL`, `LANE`, `FAMILY`, `PROVIDER`, `BILLING`, and `LIMIT` suffixes
(for example, `BUILTIN_WORKER_PROVIDER`). Known plan lanes infer provider,
billing, and the shared lane limit from your account settings. A metered API
lane needs its actual provider id and a positive limit; an unknown lane needs
all three explicitly. Conflicting lane metadata is refused before writes.
Metered built-ins remain available only through the named, explicit metered
opt-in; they are absent from the default flat candidate table.

For an unprofiled model override, the skills resolve the exact provider/model
from Delta's available-model metadata and the generated
`references/identity-registry.json`. The shared identity helper returns the
effective family, lane, billing, and limits used for dispatch and reviewer
selection. An unknown family needs verified or explicit user metadata; an
unknown provider needs configured lane/budget metadata. The selected profile
is not used as a substitute identity.

**LLM Providers**

- Model Preferences (LLM Providers > each provider): leave every row on its
  Global choice. Provider-specific choices take precedence over profile models
  and would silently reroute them.
- Models Shown in Picker: hide every copy of a model that the roster runs on
  another provider, so nobody picks it by accident. For example, a metered
  API copy of `deepseek-v4-pro`, which the roster runs on the Qwen plan, or
  the Qwen plan's copy of `glm-5.3`, which would spend Qwen quota on a model
  the roster runs on Z.AI.

## 5. Per-checkout test isolation

Concurrent agents get separate checkouts on one machine, so their test suites
collide on ports, Erlang node names and epmd, and test database names. Copy
`examples/agents-prepare.sh` to your project's `.agents/prepare` (check
Delta's "Prepare your project" docs for the exact contract) and adapt it. It
claims a unique slot per checkout (atomically, reclaiming slots whose
checkout is gone, one reclaimer at a time), fetches dependencies from the
local package caches when it can, and writes `.delta-env` with `DELTA_SLOT`,
`MIX_TEST_PARTITION`, `PORT`, `ERL_EPMD_PORT`, and `sccache` settings. The
skill sources `.delta-env` before every VERIFY command when it exists.

Delta has no SessionStart hook, so the script also starts the context-index
maintainer (`~/.local/share/zetta-delta/ensure-context-indexes.py`, installed
with the context tools, section 9) for the new checkout,
fail-open and in its own session, so ending prepare does not stop it. Set `DELTA_PREPARE_INDEXES=0` to skip it, for example
when many short-lived subagent copies are created at once.

The maintainer indexes Git repositories, plus the non-Git folders listed in
`~/.config/zetta-delta/index-roots`, one per line: `~/notes` makes that
folder one root, and `~/src/*` makes each folder inside it its own root.
Without the file it indexes Git repositories only. State for non-Git folders
lives in `~/Library/Caches/context-indexes/` (`~/.cache/context-indexes/`
off macOS), never in the folder. It never starts a
full build it cannot finish: codegraph above 5,000 files and zg above 4,000
are skipped with a note. Tracked submodule files contribute to the tools that
traverse them, and edits inside them affect freshness. An empty or incomplete
zg directory still follows first-build admission and background handling.
A first zg build runs detached for up to 30 minutes;
if it times out, the hook removes the partial index it created and stops
retrying until you build one by hand. Only one first zg build runs at a time
on the machine (a lock in `~/Library/Caches/context-indexes/`): a checkout
that would start a second one is queued there and reports zg as
unavailable, and its build starts when the running one ends. The maintainer's zg
runs use 2 embedding contexts, not zg's default 8, unless
`ZVEC_GREP_LLAMA_CONTEXT_PARALLELISM` is set. Before each zg build it runs the
zg guard (section 9) and skips zg when the guard fails.
Keep vendored clones or bulky folders
out of an index with a `.gitignore` entry in that root. Automatic zg builds
also receive the effective Git local/global exclusions as literal paths,
including filenames with glob characters, backslashes, or newlines. Tracked
files retain Git's tracked-file exception; explicit `.ignore` rules and native
file filters still apply.

Delta clones each checkout from your local repository under
`<repo>/.delta/`, so checkouts contain only committed files on the branch
chosen at thread start. Uncommitted work, and files kept out of git (an
excluded `AGENTS.md`, an untracked `.agents/prepare`), never reach them.
Those nested clones also show up when Semble searches the main checkout:
Semble reads only `.gitignore` and `.sembleignore`, not `.git/info/exclude`.
The index maintainer keeps them out whenever it runs on a root holding
`.delta/`: it adds `.delta/` to a local `.sembleignore` and hides that file in
`.git/info/exclude`. It never edits a committed `.sembleignore`; it notes one
that lacks `.delta/`. Before the maintainer has run in a repository, the same
by hand:

```sh
cd /abs/repo && printf '.delta/\n' >> .sembleignore && printf '/.sembleignore\n' >> .git/info/exclude
```

Semble never evicts its cache, so every checkout it searched leaves a cache
in `~/Library/Caches/semble` after Delta deletes the checkout. On the
same runs the maintainer removes the caches of that root's deleted Delta
checkouts, and only those: Semble's own `semble clear orphans` would also
remove the cache of a folder on an unmounted drive.

## 6. Context rules (Personal AGENTS.md)

Delta has no MCP servers or hooks, so the context tools reach agents through
Delta's Personal rules for the top-level thread. Delta keeps
them in `~/.config/delta/AGENTS.md` (Settings > Rules > Personal AGENTS.md) and
re-reads the file at the start of each turn. Each run of the installer writes
two blocks there: the context router from `rules/personal-AGENTS.md`
(`DELTA_CONTEXT_ROUTER`), then Ponytail's always-on text from the installed
plugin (`PONYTAIL`, labelled with the plugin's version; the newest folder under
`~/.codex/plugins/cache/ponytail/ponytail/`, or `PONYTAIL_DIR`). A fresh instance
uses the bundled, MIT-licensed Ponytail rules if no local plugin is found. It replaces
only those blocks and keeps the rest of the file after them; like every file it
changes, it needs `--force` when the file exists and saves the old one first.
It refuses while a block lacks its START or END line. Rerun it after updating
Ponytail. `./install.sh --clean` removes the `personal-AGENTS.generated.md`
that earlier releases left in `~/.local/state/zetta-delta/`, and that folder
unless it holds backups.

The router names a context tool's CLI for each kind of question, has agents
run the index maintainer once per folder per thread (`.agents/prepare`,
section 5, can also start it), and sends shell commands through `rtk`.
Ponytail's text applies at full level, and reviewers check simplicity against
it.

The generated Ponytail section changes one sentence: "Grep every caller"
becomes "Find every caller (codegraph where the language is covered, otherwise
tgrep)", so it agrees with the router. If upstream rewords that sentence, the
installer keeps the text verbatim and warns.

The tools themselves come from `stack/install.sh` (section 9).

The personal rules load into the thread's context; the installer prints their
size and warns above 10,240 bytes. Subagents receive the compact
`rules/subagent-context.md` preamble through each profile's additive `prompt`.
It covers routing, desktop PATH, RTK, bounded per-root index setup, compression,
and source/write boundaries. It refers to the full router only for details.
It does not copy the full Personal AGENTS.md or replace Delta's native base
prompt (`system_prompt`/`system_prompt_file`). Existing built-in prompt text
and unrelated TOML settings are preserved; malformed managed markers refuse
installation before bundle writes. Custom role prompts still come from their
templates. This does not establish automatic user-AGENTS propagation or
guarantee model compliance; verify a live subagent.

Smoke test (new thread, any model):

```
Using the context tool router in your rules: find where <some function> is
defined with tgrep, find code related to <some behavior> with semble, and
list the callers of <some Elixir function> with codegraph. Show each command.
```

Each command should be the routed CLI with an absolute root, and shell
commands such as `git status` should carry the `rtk` prefix.

For profile acceptance, use a read-only spawn without copying router rules:

```
Spawn the built-in scout, worker, then reviewer sequentially with this same task:
"Read-only. In SOURCE ROOT <absolute repository>, locate exact occurrences of
<known identifier> and explain the implementation of <specific behavior>.
Show your shell commands and evidence; make no changes."
```

Check the actual subagent tool calls for the profile's PATH bootstrap, RTK,
scoped tgrep (or `--no-index`), and Semble as appropriate. Check any structural
follow-up uses CodeGraph only with supported, current root coverage. Prompt
contents and the agent's self-report alone are not runtime proof.

## 7. Smoke test

Commit or stash in-progress work first if you can: isolated copies carry
every uncommitted change, and reviewers diff against `HEAD`.

New thread, select `k3` with effort high before sending anything, then:

```
/orchestrate Recon only. Spawn the built-in Scout and qwen-max in parallel.
Each reports the model it is running as and this repository's top-level
layout; qwen-max makes no changes. Then spawn deepseek-pro to report
its model with no review. Stop after reporting; do not plan or dispatch work.
```

Confirm that each spawn line in the thread names the expected profile and
model; that label is Delta's, not the subagent's self-report.

## 8. Use

### PR review

```
/pr-review <PR URL, PR number, or main...feature_branch>
```

This mode reviews one PR or commit range. Repository slugs, clone paths, and
unambiguous local shorthand resolve through the verified source remote. Multiple
PRs or a repository-only request route to batch review below.

The thread pins the comparison, plans from changed-file and hunk metadata,
and sizes independent review assignments to each model's capacity. It considers
every configured profile, selects useful perspectives across model families,
and queues work within lane and thread limits. Small changes may use fewer
models; larger changes may need several waves. Reviewers read changed hunks
and retrieve focused context as needed.

Every original finding gets three passes before deduplication: independent discovery,
vetting by another model family, and explicit adjudication by the orchestrator. The
third pass includes rejected findings, compares both evidence trails, and records a
reasoned final disposition. Missing vetting or adjudication leaves the review incomplete.
The review covers bugs, security, regressions, privacy/PII,
data governance, idiomatic code, anti-patterns, test value, and unnecessary code.
The report preserves finder/vetter/adjudicator identities, rejected and unresolved claims,
and coverage gaps. Review setup and any tests use disposable snapshots as needed;
the skill requires source and your checkout to remain unchanged.

After adjudication, both review modes use the same publication contract when
submission is authorized: actionable findings request changes with the findings
in the review body; no findings approve, normally without a body; nits only
approve with the nits in that same body, explicitly stating they do not block
merging. Incomplete reviews or changed base/head comparisons hold submission.
Each result produces one review and no separate nit comments. Publication
rechecks the target, binds the review to the frozen head, verifies the receipt,
and checks for duplicates before retrying an uncertain submission.

The shared [review voice](skills/pr-review/references/review-voice.md) governs
review bodies and ordinary review-flow messages. It captures Randy's direct,
concise cadence with professional grammar and no profanity. Its
[source coverage](skills/pr-review/references/review-voice-sources.md) records
the assessment across both machines and connected sources, including sampling
and access gaps. Private conversations and email text are not bundled.

```mermaid
flowchart TD
  U["You: /pr-review PR or range"] --> T["Pin comparison and freeze source<br/>measure changed files and hunks"]
  T --> S["Select useful configured perspectives<br/>size assignments and schedule waves"]
  S --> D["Independent discovery<br/>across selected model families"]
  D --> V["Vet every original finding<br/>with a different model family"]
  V --> A["Orchestrator adjudicates every finding<br/>record evidence and final disposition"]
  A --> R["Deduplicate adjudicated claims<br/>report findings, identities, and coverage gaps"]
```

### Batch PR review

```
/pr-review-batch owner/repo 12, 18, 23
/pr-review-batch owner/repo
```

Lists accept numbers, URLs, commas, spaces, and bullets. A repository without a
list means all open PRs needing your review: new PRs and changed heads get a full
review; a head matching your latest submitted, non-dismissed review is skipped.

Each PR gets one eligible strong reviewer within roster budgets, followed by
top-level orchestrator vetting and adjudication. This mode does not require a
separate cross-family vet. It remains read-only until you explicitly authorize
publication; test execution also requires authorization and a disposable copy.
It follows the same three publication outcomes and shared voice described above.

### Orchestrate

```
/orchestrate <task, constraints, and how you will judge it done>
```

Ask for best-of-N explicitly ("best-of-3 across Kimi, GLM, and Qwen") or let
the skill choose it for a high-risk unit. If another orchestrator thread is
running, say so; the skill halves its budgets.

In best-of-N, two to four models from different families each attempt the
same unit in their own copy and hand back a patch through `bon.sh`. The thread
judges the patches and applies only the winner, which then gets the usual
review. Export verifies restoration before a candidate finishes, and repeating
a completed export preserves its patch. The thread checks the parent's content
against the pinned tree before selection. A failed task report is distinct
from a failed runtime turn: partial worker changes are inspected before a
retry, and a comparison copy that cannot restore remains active for recovery
or Stop Subagent instead of finishing with dirty changes.

```mermaid
flowchart TD
  O["Orchestrator thread<br/>bon.sh snapshot"] --> C1["Candidate<br/>model family 1<br/>own copy"]
  O --> C2["Candidate<br/>model family 2<br/>own copy"]
  O --> C3["Candidate<br/>model family 3<br/>own copy"]
  C1 -->|report and patch| J["Judge: VERIFY, then ACCEPTANCE,<br/>scope, risk, readability"]
  C2 -->|report and patch| J
  C3 -->|report and patch| J
  J -->|clear winner| A["bon.sh apply<br/>the winner lands in your checkout"]
  J -.->|top two close| Y["You choose"]
  Y -.-> A
  A --> R["VERIFY, then review by<br/>another model family"]
```

Two more skills run a thread a different way. Each takes any task: a change,
a question, research, an investigation.

### Adversarial

```
/adversarial <task or question, and anything both sides should know>
```

A defender does the task or answers the question; a challenger from another
model family tries to break or refute it with evidence. They take turns, each
seeing every earlier turn, until the challenger accepts, the defender
concedes, they agree, or 5 rounds pass. Each turn's changes land in your
checkout as it finishes, so the next turn starts from them: a failing test
the challenger adds stays until the defender makes it pass or argues it away.
Name the sides ("defend with qwen-max, challenge with grok") or a round limit
to change the defaults. The thread reports both final positions and picks no
winner unless you ask.

```mermaid
sequenceDiagram
  actor U as You
  participant T as Thread (moderator)
  participant D as Defender
  participant C as Challenger from another family
  U->>T: /adversarial task
  loop Each round, up to the limit (5 by default)
    T->>D: Turn block with every earlier turn
    D-->>T: Turn report, its changes merged into your checkout
    Note over T,D: CONCEDE or AGREE ends the room
    T->>C: Turn block with every earlier turn
    C-->>T: Turn report, its changes merged into your checkout
    Note over T,C: ACCEPT or AGREE ends the room
  end
  T-->>U: Both final positions, no winner unless you ask
```

### Isolated

```
/isolated <task or question>
```

Two to four models from different families (three unless you say otherwise)
get the same task in their own copies and never see each other's work. What
each changes comes back as a patch through `bon.sh`, so nothing lands until
you choose. The thread shows every result word for word, side by side; you
keep one, its patch is applied, and follow-ups go only to the models you
pick. Keeping a winner promotes its code into the common parent baseline;
subsequent rounds isolate new attempts from that promoted baseline.

```mermaid
flowchart TD
  U["You: /isolated task"] --> T["Thread<br/>bon.sh snapshot"]
  T --> P1["Participant<br/>model family 1<br/>own copy"]
  T --> P2["Participant<br/>model family 2<br/>own copy"]
  T --> P3["Participant<br/>model family 3<br/>own copy"]
  P1 -->|result and patch| V["Every result shown<br/>word for word, side by side"]
  P2 -->|result and patch| V
  P3 -->|result and patch| V
  V --> K{"You keep one?"}
  K -->|yes| A["bon.sh apply<br/>its patch lands in your checkout"]
  K -->|no| N["Nothing lands"]
  A --> F["Follow-ups go only to<br/>the participants you pick"]
  N --> F
```

## 9. Context tools

The rules route questions to tgrep, Semble, CodeGraph, zvec-grep (`zg`),
Context7 (`ctx7`), GitHits, RTK, and Caveman's `toon encode`, and run the
index maintainer before indexed searches. `stack/install.sh` installs them
from a clone of this repository:

```bash
stack/install.sh --dry-run        # each tool's source, pin, and patch count
stack/install.sh                  # everything
stack/install.sh semble rtk       # only these
```

The first five are built from their upstream release, checked against the
pinned commit, with this repository's patches applied in order:

| Tool | Upstream | Patches (`stack/patches/<tool>/`) |
|---|---|---|
| tgrep | microsoft/tgrep v1.0.11 | none |
| semble | MinishLab/semble v0.6.1 | none |
| codegraph | colbymchenry/codegraph v1.6.2 | inline Rust tests count as callers; Elixir; root-only Git ignores stay anchored; source under `build/` remains eligible |
| zvec-grep | zvec-ai/zvec-grep v0.2.2 | Metal tensor opt-out; node-llama-cpp 3.22.1 (Metal tensor kernels on Apple M5); exact Git exclusions; source under `build/` remains eligible |
| rtk | rtk-ai/rtk v0.51.0 | a failed command no longer prints a TOML filter's `on_empty` "ok" |
| ctx7, githits, caveman | npm 0.5.13, 0.26.0, 2.0.1 | none |

The script needs git, curl, and tar, plus cargo, uv, node 22 or later with
npm, and python3 for the tools you pick (`stack/install.sh --help`). It
checks them before building anything, and builds in a temporary folder it
removes on exit. Where things go:

- Every command lands in `~/.local/bin` (`PREFIX=/other/prefix` changes
  it): cargo (tgrep, rtk) and npm (zvec-grep, ctx7, githits, caveman)
  install under that prefix, and uv links Semble there.
- Semble installs with its `mcp` extra, so agents that use Semble's MCP
  server keep working.
- CodeGraph: its self-contained bundle in
  `~/.codegraph/versions/`, in a directory identified by upstream version,
  patch hash, platform, and a unique build suffix, with `~/.local/bin/codegraph`
  and `~/.codegraph/current` pointing at it. Extraction and launcher validation
  finish before activation. Earlier builds stay even for a same-version patch
  update or reinstall, so rollback is re-pointing those two links.
- The index maintainer: `~/.local/share/zetta-delta/`, where the rules and
  `.agents/prepare` run it. List non-Git folders to index in
  `~/.config/zetta-delta/index-roots` (section 5).
- zvec-grep installs as version `<release>+zetta-delta.<hash>`, where the
  hash covers its patches. The installer then copies the zg guard
  (`stack/zvec-grep/verify-install.py`) to `~/.local/share/zvec-grep/`, pins
  it to the exact dist artifact set, package metadata, and zg entrypoint it
  just installed, and checks them. Added artifacts, executable redirection,
  and metadata drift are rejected. An existing
  guard and pin are first saved under
  `~/.local/share/zvec-grep/restores/<UTC time>/`. The index maintainer runs
  the guard before each zg build and skips zg when it fails, so a zvec-grep
  installed some other way stays unused until you rerun
  `stack/install.sh zvec-grep` or restore the saved pin.

At the end it warns about any command it installed that another copy earlier
on `PATH` shadows.

## Limits

- Provider budgets are enforced per orchestrator thread by instruction, not by Delta.
  A shared admission proxy (per-provider concurrency, window budgets,
  429-aware queueing) is needed to enforce provider-wide budgets across threads.
- Delta records tracked files and untracked files that Git does not ignore;
  ignored build output does not merge back from isolated workers. Untracked,
  non-ignored artifacts do, which is why every block requires a clean
  `git status`.
