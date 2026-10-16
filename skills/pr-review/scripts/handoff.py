"""Lossless private task transport and reviewer-side report binding."""

import copy
import hashlib
import json
import os
import re
import shlex
import stat
import uuid
from pathlib import Path


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
    template = copy.deepcopy(task["report_template"])
    for name in ("assignment_id", "target", "reviewer"):
        template.pop(name, None)
    for name, reserved in (("findings", "finder"), ("verdicts", "assignment_id"), ("adjudications", "assignment_id")):
        for row in template.get(name, []):
            row.pop(reserved, None)
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
        controls.append("The file handoff supplies reserved bindings; do not copy them from the historical report schema below.")
    controls += ["SOURCE ROOT: " + payload["source_root"], "RUNTIME IDENTITY: " + json.dumps(payload["identity"]),
                 "JUDGMENT TEMPLATE: " + json.dumps(template, ensure_ascii=True), rules.strip(),
                 ("SEALED EVIDENCE: Read assigned scope/claims and relevant context/history before authoring. "
                 "Use --section <key/subkey/index> --offset <next_offset> --length <1..32768>; default length is 8192. "
                 "Each page rechecks the original task and source integrity. Continue until next_offset=done for complete selected evidence.")]
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
            or not re.fullmatch(r"handoff-[0-9a-f]{32}/(task.json|input.json|report.json)", proof["artifact"])
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
    if not contents or set(contents) - {"task.json", "input.json", "report.json"}:
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


def read(directory: Path, proof: dict) -> bytes:
    """Read exact recorded bytes, rejecting changed or redirected artifacts.

    Args:
        directory (Path): Canonical private run directory.
        proof (dict): Validated journal reference.

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
            raw = source.read()
    finally:
        os.close(descriptor)
    if hashlib.sha256(raw).hexdigest() != proof["sha256"]:
        raise ValueError("handoff artifact changed")
    return raw


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
