#!/usr/bin/env python3
"""Render the installer's local files; never copy live settings into the repo."""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import re
import sys
import tomllib
from urllib.parse import urlsplit

CATALOG = Path(__file__).resolve().parents[1] / "settings/provider-catalog.json"
MODEL_FIELDS = {"id", "context_window", "max_output_tokens", "interleaved_reasoning",
                "parallel_tool_calls", "reasoning_efforts"}


def load_catalog():
    data = json.loads(CATALOG.read_text())
    if set(data) != {"version", "providers"} or data["version"] != 1:
        raise ValueError("unsupported provider catalog")
    for provider in data["providers"].values():
        if set(provider) != {"name", "base_url", "api_mode", "models"}:
            raise ValueError("catalog contains unapproved provider fields")
        if provider["api_mode"] not in {"anthropic", "open_ai_chat_completions", "open_ai_responses"}:
            raise ValueError("catalog contains unsupported API mode metadata")
        url = urlsplit(provider["base_url"])
        if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError("catalog URLs must be public HTTPS endpoints without credentials or query strings")
        for model in provider["models"]:
            if not set(model) <= MODEL_FIELDS or not re.fullmatch(r"[A-Za-z0-9._:/@+-]+", model["id"]):
                raise ValueError("catalog contains unapproved model fields")
            for field in ("context_window", "max_output_tokens"):
                if type(model[field]) is not int or model[field] <= 0:
                    raise ValueError("catalog token limits must be positive integers")
            for field in ("interleaved_reasoning", "parallel_tool_calls"):
                if field in model and type(model[field]) is not bool:
                    raise ValueError("catalog capabilities must be booleans")
            if not isinstance(model["reasoning_efforts"], list) or not all(
                    effort in {"off", "on", "low", "medium", "high", "xhigh", "max"}
                    for effort in model["reasoning_efforts"]):
                raise ValueError("catalog contains unsupported effort metadata")
    return data["providers"]


def provider_id(provider):
    return "custom:" + hashlib.sha256(provider["base_url"].encode()).hexdigest()


def settings(path, roles, identities, thread_cap):
    data = json.loads(path.read_text()) if path.exists() else {"version": 1}
    if not isinstance(data, dict) or data.get("version", 1) != 1:
        raise ValueError("unsupported Delta settings version")
    native = data.setdefault("native", {})
    portable = data.setdefault("portable", {})
    if not isinstance(native, dict) or not isinstance(portable, dict):
        raise ValueError("invalid Delta settings sections")
    providers = native.setdefault("custom_providers", [])
    if not isinstance(providers, list) or not all(isinstance(row, dict) for row in providers):
        raise ValueError("invalid Delta provider list")
    catalog = load_catalog()
    for role, identity in zip(roles, identities):
        incoming = catalog[role]
        if provider_id(incoming) != identity:
            raise ValueError("provider override differs from bundled endpoint; configure it in Delta and use install.sh without --configure-delta")
        matches = [row for row in providers if row.get("base_url") == incoming["base_url"]]
        if len(matches) > 1:
            raise ValueError("duplicate provider endpoint in Delta settings")
        if matches:
            existing = matches[0]
            # Credentials and unrelated models remain local and retain their values.
            models = existing.setdefault("models", [])
            if not isinstance(models, list) or not all(isinstance(row, dict) for row in models):
                raise ValueError("invalid existing provider model list")
            for model in incoming["models"]:
                entries = [row for row in models if row.get("id") == model["id"]]
                if len(entries) > 1:
                    raise ValueError("duplicate provider model in Delta settings")
                if entries:
                    entries[0].update(model)
                else:
                    models.append(copy.deepcopy(model))
            existing["api_mode"] = incoming["api_mode"]
        else:
            providers.append(dict(copy.deepcopy(incoming), headers=[]))
    portable["subagent_delegation"] = "on_request"
    for key, values in (("subagent_concurrency", {"maximum_per_parent": thread_cap, "maximum_total": 2 * thread_cap}),
                        ("subagent_defaults", {"allow_parent_model_override": True})):
        current = portable.setdefault(key, {})
        if not isinstance(current, dict):
            raise ValueError("invalid Delta subagent settings")
        current.update(values)
    data.setdefault("version", 1)
    return json.dumps(data, indent=2) + "\n"


def builtin(path, model, effort):
    original = path.read_text() if path.exists() else ""
    parsed = tomllib.loads(original)
    expected = copy.deepcopy(parsed)
    selected = expected.setdefault("model", {}).setdefault("any", {})
    if not isinstance(selected, dict):
        raise ValueError("invalid built-in model settings")
    selected["model"] = model
    if effort:
        selected["thinking_effort"] = effort
    else:
        selected.pop("thinking_effort", None)
    lines = original.splitlines(keepends=True)
    headers = [i for i, line in enumerate(lines) if re.fullmatch(r"\s*\[model\.any\]\s*(?:#.*)?\n?", line)]
    block = f'model = {json.dumps(model)}\n'
    if effort:
        block += f'thinking_effort = {json.dumps(effort)}\n'
    if not headers:
        rendered = original.rstrip() + "\n\n[model.any]\n" + block
    elif len(headers) == 1:
        start = headers[0] + 1
        end = next((i for i in range(start, len(lines)) if re.match(r"\s*\[", lines[i])), len(lines))
        kept = [line for line in lines[start:end] if not re.match(r"\s*(model|thinking_effort)\s*=", line)]
        rendered = "".join(lines[:start]) + block + "".join(kept + lines[end:])
    else:
        raise ValueError("unsupported built-in TOML layout; no changes made")
    # Refuse complex layouts rather than touching strings, comments, or other tables.
    if tomllib.loads(rendered) != expected:
        raise ValueError("unsupported built-in TOML layout; no changes made")
    return rendered


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    identity = commands.add_parser("provider-id")
    identity.add_argument("role", choices=("kimi", "zai", "qwen", "minimax"))
    config = commands.add_parser("settings")
    config.add_argument("path", type=Path)
    config.add_argument("--provider", action="append", default=[], help="catalog-role=exact-provider-id")
    config.add_argument("--thread-cap", type=int, required=True)
    profile = commands.add_parser("builtin")
    profile.add_argument("path", type=Path)
    profile.add_argument("--model", required=True)
    profile.add_argument("--effort", default="high", choices=("", "low", "medium", "high", "xhigh", "max"))
    args = parser.parse_args()
    try:
        if args.command == "provider-id":
            print(provider_id(load_catalog()[args.role]))
        elif args.command == "settings":
            if args.thread_cap < 1:
                raise ValueError("thread cap must be positive")
            roles, identities = [], []
            for assignment in args.provider:
                role, identity = assignment.split("=", 1)
                roles.append(role)
                identities.append(identity)
            sys.stdout.write(settings(args.path, roles, identities, args.thread_cap))
        else:
            sys.stdout.write(builtin(args.path, args.model, args.effort))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        # Never echo settings contents, parser snippets, headers, or credentials.
        print("SETUP: blocked: invalid/unreadable setup input or unsupported built-in layout", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
