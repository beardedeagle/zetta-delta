<!-- DELTA_SUBAGENT_CONTEXT_START v1 -->
Context/token stack: apply these rules in every role, even if the task omits RULES.
Use the task's absolute SOURCE ROOT, otherwise determine your checkout with pwd.
Keep each lookup scoped; read cited code once, then only missing context.

Routes: exact text/identifiers/globs -> tgrep (flags first, --, explicit root);
unknown implementation -> semble search; known symbols/callers/impact ->
codegraph explore in supported languages; indexed prose -> zg query;
library/API/CLI docs -> ctx7 library then ctx7 docs; public OSS/package evidence
-> githits. Keep private evidence out of public queries. Do not start with broad
ls/find/grep/rg or whole-file dumps. Missing/stale tgrep index -> --no-index,
not rg. Missing graph/vector coverage -> Semble or focused exact scan/source reads;
state the gap. Current returned source is already read; similarity is not a call edge.

Every shell command starts with `export PATH={{TOOL_PATH_SH}}:"$PATH";`.
Use rtk for supported shell filters, rtk proxy for context CLIs and exact bytes.
Use POSIX sh; print `; echo "exit=$?"` when success matters (pipes can hide failure).
Before the first indexed lookup in each root, run the bounded index maintainer:
`rtk proxy python3 -c 'import json,sys;print(json.dumps({"hook_event_name":"SessionStart","cwd":sys.argv[1]}))' /abs/root | rtk proxy python3 ~/.local/share/zetta-delta/ensure-context-indexes.py`.
Do not wait for background builds or download models/start services/watchers.

Scoped retrieval and RTK come first; caveman tools toon encode is only for raw
bulky uniform JSON. Never recompress filtered output or chain compressors.
For details consult only the DELTA_CONTEXT_ROUTER block in ~/.config/delta/AGENTS.md,
not the whole file. Keep your role, report, source/test isolation and write limits;
this preamble grants no additional writes. Report durable facts under LEARNED;
only the parent writes wiki/memory. Follow project simplicity/Ponytail rules.
<!-- DELTA_SUBAGENT_CONTEXT_END -->
