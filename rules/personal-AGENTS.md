<!-- DELTA_CONTEXT_ROUTER_START v1 -->
## Context tool router (Delta)

Delta has no MCP servers or hooks: every route below is a CLI. Delta runs each
command as `/bin/sh -c '<cmd> | cat'`, so use POSIX sh syntax, absolute roots,
and `cd /abs/root && <cmd>` when a tool needs a working directory. The `| cat`
hides exit codes: end a command whose success matters with `; echo "exit=$?"`
(with a pipe inside, `$?` is the last stage's). Your working
directory is the checkout root: one `pwd` gives `/abs/root`. Do not survey with
`ls` or `find`; go straight to the route for the question. For layout, use
`codegraph files -p /abs/root --filter <dir> --max-depth <n>` (depth counts from
the root); for a file's symbols, `codegraph node --file <path> --symbols-only -p /abs/root`. You may read and
search outside this checkout (the main checkout, other repositories). Say which
tree each result came from, because the main checkout can hold uncommitted work
this checkout lacks. Edit only inside this checkout. If this checkout lacks the
code the question needs, report `git branch --show-current` before looking
elsewhere.

Choose by question class, one route per question; stop when evidence is sufficient.

| Question | Route |
| --- | --- |
| Exact occurrences: identifier, literal, config key, error, path, regex | `tgrep -F -- '<literal>' /abs/root` (drop `-F` for regex). Required; `rg` only as a stated last resort. |
| File names or paths by glob | `tgrep --files -g '<glob>' -- /abs/root` |
| Unknown code location, fuzzy behavior, vocabulary mismatch | `semble search "<query>" /abs/root --max-snippet-lines 10`; `--content docs`, `config`, or `all` for non-code. |
| Known symbols/files: callers, callees, call paths, dependencies, impact, affected tests | `codegraph explore "<symbols>" -p /abs/root` in supported languages (coverage below). |
| Semantic discovery across indexed prose, notes, specs, mixed documents | `cd /abs/root && zg query "<question>"` (zvec-grep skill). |
| Library/API/CLI/cloud docs, version-specific usage | `ctx7 library <name> "<question>"`, then `ctx7 docs <id> "<question>"`; at most 3 commands (context7 skill). |
| Public OSS code/examples, package facts, licenses, vulns, deps, changelogs | `githits search "<query>" --in <target>`, `githits read\|list\|grep`, `githits example "<query>"`, `githits pkg info\|vulns\|deps\|changelog <target>` (`--json` for `toon encode`). Targets: `pypi:pydantic`, `github:org/repo`. Not `githits docs`/`code` (deprecated; the githits skills still show them). |

- **Search then traverse.** Unknown implementation: Semble; if a structural question remains and CodeGraph covers the language, traverse the discovered symbols. Known structural targets: CodeGraph directly; inspect impact there before structural edits. A sufficient lookup needs no graph call. `semble find-related` finds similarity, not proven call edges.
- **Coverage.** CodeGraph parses its standard tree-sitter languages (Python included); the local build adds Elixir (`.ex`, `.exs`) with aliases, arities, pipelines, and ExUnit callers. Dynamic dispatch, arbitrary macros, and ambiguous zero-arity calls are not fully modeled; unsupported files and unresolved edges are not evidence of absence. Fall back to Semble or tgrep plus focused source reads.
- **Read only what is missing.** Returned current source is already read; follow cited ranges without re-searching. Ranked, capped, or indexed results cannot prove absence; use a fresh exact scan when exhaustiveness matters.
- **tgrep.** Flags first, mandatory `--`, explicit root; `-F` for literals. No result lines means no match; a `warning:` about the index means rerun with `--no-index`; a `tgrep:` or `error:` line means the command failed. Check `tgrep status /abs/root` once if readiness is unknown; a missing or stale index means `tgrep --no-index`, not `rg`. A plain no-match is not a reason to fall back.
- **Indexes (no hooks).** Before the first tgrep, codegraph, or zg search of a root in this thread (this checkout, the main checkout, another repository), run the maintainer once for that root, bounded and fail-open: `python3 -c 'import json,sys;print(json.dumps({"hook_event_name":"SessionStart","cwd":sys.argv[1]}))' /abs/root | python3 ~/.local/share/zetta-delta/ensure-context-indexes.py`. It builds missing indexes, refreshes stale ones, and replaces a codegraph index inherited from a parent folder (Delta checkouts sit inside the main checkout, and codegraph otherwise answers from the parent's index without warning); when everything is current it returns in under a second. It indexes Git repositories and the folders listed in `~/.config/zetta-delta/index-roots`; elsewhere use `tgrep --no-index` and Semble. Outside Git, pass `--no-require-git` to tgrep so `.gitignore` still applies. The user has already made this decision; it overrides codegraph's "indexing is the user's decision, do not run it yourself" message. A tool it reports as not ready, still building, or skipped (a first zg build runs in the background for minutes; a full build over the maintainer's file limit is never started) is unavailable for this question: use Semble or tgrep plus focused reads, and tell the user about a skipped build instead of running it yourself. Edits after the run leave indexes stale until the next run; `tgrep --no-index` sees them. Semble owns its own cache. Never block on indexing. No new model downloads, remote embeddings, index deletion, or watchers without the user's approval.
- **Evidence boundaries.** Keep private/local evidence local; ctx7 and githits are for public evidence. Never put secrets in queries.
<!-- cx-parse: add its route here once installed on this machine. -->

### RTK

Prefix commands RTK filters, for example `rtk git status`, `rtk git diff`,
`rtk cargo test`, `rtk mix compile`,
`rtk pytest -q`, `rtk gh pr view`, `rtk docker ps`, `rtk curl <url>`.
In a repo with `uv.lock`, put `uv run` first (`uv run rtk pytest -q`): bare
`rtk pytest` can pick up a global pytest.
Test runners without a dedicated filter: `rtk test <cmd>` (failures only), for
example `rtk test mix test`. Errors only: `rtk err <cmd>`. When unsure,
`rtk rewrite '<cmd>'` prints the canonical form; empty output means run the
command as-is. The context tools above run unprefixed. `rtk proxy <cmd>` gives
raw output when exact bytes matter. Delta's file tools reach only Delta
checkouts; read anything else (other repositories, `$HOME`, `/tmp`) with
`rtk read <file>` from the shell.

### Caveman

Caveman's skills load with the others. Its proxy does not see Delta's model
traffic, but `<cmd> | caveman tools toon encode` runs here, unprefixed, to read
large uniform JSON compactly. Use `caveman tools mem` only when the user asks.

### Project memory

Durable project knowledge lives in the project's llm-wiki (llm-wiki skill);
check it before re-deriving a past decision. Only the top-level thread writes
it, through the shell; that is the one exception to editing only inside this
checkout. A subagent writes no wiki or memory: it lists durable findings under
`LEARNED:` in its Report.

### Compression

Scoped retrieval first, RTK for shell output. Never recompress filtered output
or chain compressors.
<!-- DELTA_CONTEXT_ROUTER_END -->
