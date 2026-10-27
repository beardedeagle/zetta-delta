"""Lossless private task transport and reviewer-side report binding."""

import copy
import fnmatch
import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
import uuid
from pathlib import Path


def author_template(template: dict) -> dict:
    """Project transport bindings into the reviewer-authored judgment schema.

    Args:
        template (dict): Complete immutable transport template.

    Returns:
        dict: Independent copy containing only author-supplied fields.
    """
    result = copy.deepcopy(template)
    for name in ("assignment_id", "target", "reviewer", "related_targets"):
        result.pop(name, None)
    for name, reserved in (("findings", "finder"), ("verdicts", "assignment_id"), ("adjudications", "assignment_id")):
        for row in result.get(name, []):
            row.pop(reserved, None)
    return result


def task_view(task: dict, command: str, *, section: str | None = None, offset: int = 0, length: int = 8192) -> str:
    """Render controls first or a bounded portion of verified task evidence.

    Args:
        task (dict): Envelope already verified against its journal-bound artifact.
        command (str): Trusted task-read command for this run and assignment.
        section (str | None): Slash-separated payload keys or list indexes;
            escape key tildes as ~0 and slashes as ~1. None selects controls.
        offset (int): Zero-based byte offset in the section's ASCII JSON.
        length (int): Maximum evidence bytes, from 1 through 32768.

    Returns:
        str: Assignment controls and evidence directory, or a page with its
            total size and next offset. Full sealed evidence is never modified.

    Raises:
        ValueError: The task, section or page range is invalid.
    """
    heading, raw = task["task"].split("\n", 1)
    payload, end = json.JSONDecoder().raw_decode(raw)
    if not isinstance(payload, dict) or type(offset) is not int or type(length) is not int or offset < 0 or not 1 <= length <= 32768:
        raise ValueError("invalid task evidence range")
    if section is not None:
        value = payload
        for part in section.split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if isinstance(value, dict) and part in value:
                value = value[part]
            elif isinstance(value, list) and part.isascii() and part.isdigit() and int(part) < len(value):
                value = value[int(part)]
            else:
                raise ValueError("unknown task evidence section")
        encoded = json.dumps(value, ensure_ascii=True, separators=(",", ":"))
        if offset >= len(encoded):
            raise ValueError("task evidence offset is outside the section")
        stop = min(offset + length, len(encoded))
        next_offset = str(stop) if stop < len(encoded) else "done"
        return f"SECTION {section} bytes={len(encoded)} offset={offset} next_offset={next_offset}\n" + encoded[offset:stop]
    if offset or length != 8192:
        raise ValueError("task evidence ranges require --section")
    kind = heading.split(" ", 1)[0]
    finding_key = {"VET": "findings_to_vet", "ADJUDICATE": "findings_to_adjudicate"}.get(kind)
    ids = [row["id"] for row in payload[finding_key]] if finding_key else []
    primary = ("Evaluate exactly the assigned raw IDs. Historical PR comments are context, not substitute assignments. "
               "Return one decision per assigned ID." if finding_key else "Discover independently within the assigned scope; no finding quota.")
    tests = payload["test_plan"]
    rules, marker, receipt = raw[end:].partition("\nFILE REPORT HANDOFF:")
    template = author_template(task["report_template"])
    controls = [heading, "PRIMARY ASSIGNMENT: " + primary, "ASSIGNED RAW IDS: " + json.dumps(ids),
                "TEST AUTHORIZATION: " + json.dumps(tests, ensure_ascii=True),
                "Execute only these commands in the recorded test root." if tests["commands"] else "No test commands are authorized. Use static evidence and the recorded omission reason."]
    if kind != "ADJUDICATE":
        controls += ["EXPECTED COVERAGE PATHS: " + json.dumps(sorted({path for unit in payload["scope"].values() for path in unit["paths"]})),
                     "EXPECTED COVERAGE UNITS: " + json.dumps(list(payload["scope"])),
                     "EXPECTED COVERAGE INTERACTIONS: " + json.dumps(list(payload["interactions"])),
                     ("These are allowed checked/checked_units/checked_interactions attestations, not raw finding IDs. "
                      "Record only completed review; related files inspected are evidence, not additional assigned coverage.")]
    if marker:
        controls.append("FILE REPORT HANDOFF:" + receipt)
        controls.append("The file handoff supplies reserved bindings; write only the author fields shown below.")
    controls += ["SOURCE ROOT: " + payload["source_root"], "RUNTIME IDENTITY: " + json.dumps(payload["identity"]),
                 "JUDGMENT TEMPLATE: " + json.dumps(template, ensure_ascii=True), rules.strip(),
                 ("SEALED EVIDENCE: Read assigned scope/claims and relevant context/history before authoring. "
                 "Use --section <key/subkey/index> --offset <next_offset> --length <1..32768>; default length is 8192. "
                 "Each page rechecks the original task and source integrity. Continue until next_offset=done for complete selected evidence.")]
    if "source_search_command" in payload:
        controls.append("SOURCE SEARCH: " + payload["source_search_command"] + " --pattern='<literal>' [--regex] [--path '<relative>'] [--retry-of '<search-id>']")
    if "source_read_command" in payload:
        controls.append("SOURCE READ: " + payload["source_read_command"] + " --path '<relative-file>' [--offset <next_offset>] [--length <1..32768>] [--source-target '<related-target-id>']")
        controls.append("Use this terminal command for external snapshot paths; native read_file does not accept their absolute paths. Continue until next_offset is null. A checked project file requires complete byte-delivery receipts; incomplete inspection remains partial with a material gap. Delivery does not prove analytical completeness.")
    controls.append("READ COMMAND: " + command + " --section <key/subkey/index>")
    if finding_key:
        controls.append("ASSIGNED CLAIM READ: " + command + " --section " + finding_key)
    for name, value in payload.items():
        size = len(json.dumps(value, ensure_ascii=True, separators=(",", ":")))
        controls.append(f"- {name}: {size} bytes")
    return "\n".join(controls)


def prepare_task(task: dict, command: str, target_id: str) -> None:
    """Add the native bootstrap and measure all file-backed task input once.

    Args:
        task (dict): Generated task and patch floor, updated in place.
        command (str): Trusted script/run command prefix, never reviewer text.
        target_id (str): Exact frozen target's routing key.

    Raises:
        ValueError: The generated task cannot render its control view.
    """
    suffix = f" --target {shlex.quote(target_id)} --assignment {shlex.quote(task['assignment_id'])}"
    read_command = command + " task-read" + suffix
    instructions = ("\nFILE REPORT HANDOFF: Author your judgments in a UTF-8 JSON file, omitting top-level assignment_id, target and reviewer, "
                    "finder in findings, and assignment_id in verdicts/adjudications. All other report fields and judgments remain required. "
                    "Do not copy hashes or identity dictionaries. This reviewer-side command supplies only reserved bindings and preserves your input bytes:\n"
                    + command + " author-report" + suffix + " --input '<your-judgments.json>'\n"
                    "Run it yourself after authoring your observations. Invalid drafts return all independently detectable errors and remain preserved; "
                    "correct your own file and rerun before final sealing. A valid final seals once; return its artifact receipt without retyping the report. "
                    "The parent must use receive-report with native handle/completion evidence. "
                    "Never ask the parent to reconstruct, enrich or rewrite your report. Strict validation and submitted recovery limits still apply.")
    if "related_targets" in task["report_template"]:
        instructions += " Omit related_targets too; the verified template supplies every cross-project snapshot binding."
    task["bootstrap"] = (f"{task['assignment_id']}: Read-only review. Startup is complete; do not run SessionStart, index maintenance, cache creation or Git writes. "
                         "First run only rtk proxy pwd -P in your native terminal, without cd. Immediately send the result to your parent through native agent messaging. "
                         "Wait for the parent to confirm started registration; do not inspect sources, finish, or return a final report while waiting. "
                         "Then read your assignment controls using this command. Follow its bounded evidence reads; historical PR comments do not replace assigned raw IDs:\n"
                         + read_command + "\nIf runtime_registration is returned, notify the parent and wait for confirmation before retrying. "
                         "Do not retype task metadata. Author and return the file-backed report as instructed.")
    task["task"] += instructions
    task["input_floor_bytes"] += len((instructions + task["bootstrap"] + task_view(task, read_command)).encode("utf-8"))


def validate(proof: dict) -> None:
    """Validate a journal-bound artifact reference without reading its contents.

    Args:
        proof (dict): Relative artifact path and SHA-256 digest.

    Raises:
        ValueError: The reference is malformed or escapes the private directory.
    """
    if (not isinstance(proof, dict) or set(proof) != {"artifact", "sha256"}
            or not isinstance(proof["artifact"], str)
            or not re.fullmatch(r"handoff-[0-9a-f]{32}/(task.json|input.json|report.json|search.json|source.json|stdout.txt|stderr.txt)", proof["artifact"])
            or not isinstance(proof["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", proof["sha256"])):
        raise ValueError("invalid handoff artifact reference")


def store(directory: Path, contents: dict[str, bytes]) -> dict[str, dict]:
    """Persist new immutable private artifacts before recording their digests.

    Args:
        directory (Path): Locked canonical run directory.
        contents (dict[str, bytes]): Named task or report/input documents.

    Returns:
        dict[str, dict]: Artifact references suitable for the locked journal.

    Raises:
        OSError: Exclusive creation or durable persistence failed.
        ValueError: A document name is unsupported.
    """
    if not contents or set(contents) - {"task.json", "input.json", "report.json", "search.json", "source.json", "stdout.txt", "stderr.txt"}:
        raise ValueError("invalid handoff document names")
    root = directory / ("handoff-" + uuid.uuid4().hex)
    root.mkdir(mode=0o700)
    proofs = {}
    for name, raw in contents.items():
        path = root / name
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        proofs[name] = {"artifact": str(path.relative_to(directory)), "sha256": hashlib.sha256(raw).hexdigest()}
    for path in (root, directory):
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return proofs


def read(directory: Path, proof: dict, *, limit: int | None = None) -> bytes:
    """Read exact recorded bytes, rejecting changed or redirected artifacts.

    Args:
        directory (Path): Canonical private run directory.
        proof (dict): Validated journal reference.
        limit (int | None): Maximum bytes to return; zero verifies without loading
            output into memory. Every call still checks the complete digest.

    Returns:
        bytes: Original sealed document.

    Raises:
        ValueError: Ownership, permissions, path or digest no longer matches.
        OSError: The document is unavailable.
    """
    validate(proof)
    root, name = proof["artifact"].split("/")
    descriptor = os.open(directory / root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError("handoff directory ownership or permissions changed")
        file_descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
        with os.fdopen(file_descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                raise ValueError("handoff file ownership or permissions changed")
            if limit is not None and (type(limit) is not int or limit < 0):
                raise ValueError("invalid handoff read limit")
            raw = source.read() if limit is None else source.read(limit)
            if limit is None:
                checksum = hashlib.sha256(raw).hexdigest()
            else:
                source.seek(0)
                checksum = hashlib.file_digest(source, "sha256").hexdigest()
    finally:
        os.close(descriptor)
    if checksum != proof["sha256"]:
        raise ValueError("handoff artifact changed")
    return raw


def cache_tool_allowed(root: Path) -> bool:
    """Check canonical roots before selecting any cache-capable executable.

    Args:
        root (Path): Absolute frozen source directory.

    Returns:
        bool: Whether a configured root pattern matches it or an ancestor.
            Missing or unreadable policy selects the non-cache search route.
    """
    try:
        lines = (Path.home() / ".config/context-roots").read_text(encoding="utf-8").splitlines()
        canonical = root.resolve()
        candidates = [str(path) for path in (canonical, *canonical.parents)]
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            pattern = Path(line).expanduser()
            if pattern.is_absolute() and any(fnmatch.fnmatchcase(path, str(pattern)) for path in candidates):
                return True
    except (OSError, ValueError, RuntimeError, UnicodeError):
        return False
    return False


def search(directory: Path, root: Path, pattern: str, paths: list[str], regex: bool) -> dict:
    """Execute a non-writing search, retaining full output and bounded previews.

    Args:
        directory (Path): Locked private run directory.
        root (Path): Frozen source root, never a caller-selected checkout.
        pattern (str): Literal text, or a regular expression when regex is true.
        paths (list[str]): Relative paths within the frozen root; empty searches it.
        regex (bool): Interpret the pattern as a regular expression.

    Returns:
        dict: Actual exit code (None for execution failure), immutable receipt and
            previews. Only codes zero and one establish matches or no matches.

    Raises:
        OSError: Private evidence cannot be durably stored; the pending intent remains.
    """
    outputs = store(directory, {"stdout.txt": b"", "stderr.txt": b""})
    route = ["tgrep", "--no-index"] if cache_tool_allowed(root) else ["rg", "--no-config"]
    command = ["rtk", "proxy", *route, "--hidden", "--no-ignore", "-g", "!.git", "-n", *([] if regex else ["-F"])]
    code, error = None, None
    streams = []
    try:
        for name in ("stdout.txt", "stderr.txt"):
            descriptor = os.open(directory / outputs[name]["artifact"], os.O_WRONLY | os.O_NOFOLLOW | os.O_TRUNC)
            streams.append(os.fdopen(descriptor, "wb"))
        try:
            selected = [root / name for name in paths] or [root]
            if root.resolve(strict=True) != root or any(not path.resolve(strict=True).is_relative_to(root) for path in selected):
                raise ValueError("search paths must stay within the frozen source")
            if any(part.casefold() == ".git" for path in selected for part in path.resolve().relative_to(root).parts):
                raise ValueError("search paths cannot address Git administration")
            command += ["--", pattern, *(str(path) for path in selected)]
            code = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=streams[0], stderr=streams[1],
                                  timeout=30, check=False).returncode
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as failure:
            error = str(failure)
        for stream in streams:
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        for stream in streams:
            stream.close()
    previews, sizes = {}, {}
    for name, proof in outputs.items():
        with (directory / proof["artifact"]).open("rb") as stream:
            sizes[name] = os.fstat(stream.fileno()).st_size
            proof["sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
            stream.seek(0)
            previews[name] = stream.read(8192).decode("utf-8", errors="replace")
    receipt = {"root": str(root), "command": command, "exit_code": code, "error": error,
               "stdout": outputs["stdout.txt"], "stderr": outputs["stderr.txt"],
               "stdout_bytes": sizes["stdout.txt"], "stderr_bytes": sizes["stderr.txt"]}
    proof = store(directory, {"search.json": (json.dumps(receipt, ensure_ascii=True) + "\n").encode()})["search.json"]
    return {"exit_code": code, "receipt": proof, "stdout": previews["stdout.txt"],
            "stderr": previews["stderr.txt"], "error": error,
            "stdout_bytes": sizes["stdout.txt"], "stderr_bytes": sizes["stderr.txt"]}


def assemble(template: dict, judgments: dict, *, originals: list[dict] | None = None) -> dict:
    """Bind reviewer-authored judgments without altering their content.

    Args:
        template (dict): Exact generated assignment, target and reviewer bindings.
        judgments (dict): Reviewer observations with binding fields omitted.
        originals (list[dict] | None): Retained findings from the verified task;
            their recorded finder identity stays immutable on provider retries.

    Returns:
        dict: Full report for subsequent strict workflow validation.

    Raises:
        ValueError: Input is not a judgment object or supplies reserved bindings.
    """
    reserved = {"assignment_id", "target", "reviewer"}
    if "related_targets" in template:
        reserved.add("related_targets")
    if not isinstance(judgments, dict) or reserved & judgments.keys():
        raise ValueError("author judgments must omit assignment_id, target and reviewer")
    report = copy.deepcopy(judgments)
    finders = {row["id"]: row["finder"] for row in originals or []}
    report.update({key: copy.deepcopy(template[key]) for key in reserved})
    for key, field, value in (("findings", "finder", template["reviewer"]),
                              ("verdicts", "assignment_id", template["assignment_id"]),
                              ("adjudications", "assignment_id", template["assignment_id"])):
        if key not in report:
            continue
        if not isinstance(report[key], list) or any(not isinstance(row, dict) or field in row for row in report[key]):
            raise ValueError(f"author {key} must be objects omitting {field}")
        for row in report[key]:
            bound = finders.get(row["id"], value) if field == "finder" and isinstance(row.get("id"), str) else value
            row[field] = copy.deepcopy(bound)
    return report
