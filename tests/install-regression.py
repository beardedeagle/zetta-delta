#!/usr/bin/env python3
"""Offline installer regressions; all writes stay under fresh temporary fixtures."""
import os
import re
import json
import hashlib
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[1]
BASH = os.environ.get("BASH_BIN", "/bin/bash")

class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="install-regression.")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.bundle = self.base / "bundle"
        self.bundle.mkdir()
        shutil.copy2(ROOT / "install.sh", self.bundle / "install.sh")
        for filename in ("bootstrap.sh", "Brewfile"):
            shutil.copy2(ROOT / filename, self.bundle / filename)
        for directory in ("profiles", "rules", "skills", "scripts", "settings"):
            shutil.copytree(ROOT / directory, self.bundle / directory)
        self.home = self.base / "home"
        self.config = self.base / "delta"
        for directory in (self.home, self.config, self.base / "tmp", self.base / "state", self.base / "bin"):
            directory.mkdir()
        (self.config / "settings.json").write_text("{}\n")
        curl = self.base / "bin/curl"
        curl.write_text("#!/bin/sh\nexit 77\n")
        curl.chmod(0o755)
        for name in ("gh", "rtk", "tgrep", "semble", "codegraph", "zg", "ctx7", "githits", "caveman", "node", "pgrep"):
            tool = self.base / "bin" / name
            tool.write_text("#!/bin/sh\nexit " + ("1" if name == "pgrep" else "0") + "\n")
            tool.chmod(0o755)
        python = self.base / "bin/python3"
        python.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + ' "$@"\n')
        python.chmod(0o755)
        self.env = {
            "PATH": str(self.base / "bin") + ":/usr/bin:/bin",
            "HOME": str(self.home), "TMPDIR": str(self.base / "tmp"),
            "XDG_CONFIG_HOME": str(self.base / "config"),
            "XDG_STATE_HOME": str(self.base / "state"),
            "DELTA_CONFIG_DIR": str(self.config), "PONYTAIL_DIR": str(self.base / "absent"),
            "KIMI_PROVIDER": "custom:k", "ZAI_PROVIDER": "custom:z", "QWEN_PROVIDER": "custom:q",
        }

    def install(self, *args, cwd=None, **extra):
        env = dict(self.env, **extra)
        return subprocess.run([BASH, str(self.bundle / "install.sh"), *args],
                              cwd=cwd or self.base, env=env, text=True, capture_output=True)

    def assertNoWrites(self):
        self.assertFalse((self.config / "profiles").exists())
        self.assertFalse((self.home / ".agents").exists())
        self.assertEqual(list((self.base / "state").iterdir()), [])

    def roster(self):
        return (self.home / ".agents/skills/orchestrate/references/roster.md").read_text()

    def catalog_id(self, role):
        providers = json.loads((self.bundle / "settings/provider-catalog.json").read_text())["providers"]
        return "custom:" + hashlib.sha256(providers[role]["base_url"].encode()).hexdigest()

    def test_malformed_markers_refuse_before_any_writes(self):
        cases = [
            "<!-- DELTA_CONTEXT_ROUTER_END -->\nUSER_BEFORE\n<!-- DELTA_CONTEXT_ROUTER_START v0 -->\nUSER_AFTER\n",
            "<!-- DELTA_CONTEXT_ROUTER_START v0 -->\n<!-- DELTA_CONTEXT_ROUTER_START v0 -->\n<!-- DELTA_CONTEXT_ROUTER_END -->\nUSER_RULE\n<!-- DELTA_CONTEXT_ROUTER_END -->\n",
            "<!-- DELTA_CONTEXT_ROUTER_START v0 -->\n<!-- PONYTAIL_START old -->\n<!-- DELTA_CONTEXT_ROUTER_END -->\nUSER_RULE\n<!-- PONYTAIL_END -->\n",
        ]
        rules = self.home / ".config/delta/AGENTS.md"
        rules.parent.mkdir(parents=True)
        for content in cases:
            with self.subTest(content=content):
                rules.write_text(content)
                result = self.install("--force")
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertEqual(rules.read_text(), content)
                self.assertNoWrites()

    def test_metered_builtin_has_explicit_budget_and_no_default_candidate(self):
        result = self.install(BUILTIN_WORKER_MODEL="fake-api", BUILTIN_WORKER_LANE="DeepSeek API",
                              BUILTIN_WORKER_FAMILY="DeepSeek", BUILTIN_WORKER_PROVIDER="custom:api", BUILTIN_WORKER_LIMIT="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        roster = self.roster()
        worker = next(line for line in roster.splitlines() if line.startswith("| Worker (built-in)"))
        self.assertIn("| metered |", worker)
        self.assertIn("| DeepSeek API | 1 |", roster)
        candidates = roster.split("## Best-of-N candidates (flat lanes)")[1]
        self.assertNotIn("| Worker (built-in)", candidates)
        self.assertIn("explicit", worker)

    def test_unbudgeted_or_unknown_builtin_refuses_then_explicit_metadata_works(self):
        for extra in ({"BUILTIN_WORKER_LANE": "DeepSeek API"},
                      {"BUILTIN_WORKER_LANE": "DeepSeek API", "BUILTIN_WORKER_LIMIT": "1"},
                      {"BUILTIN_WORKER_LANE": "Private lane"},
                      {"BUILTIN_WORKER_LANE": "Private lane", "BUILTIN_WORKER_BILLING": "flat"}):
            with self.subTest(extra=extra):
                result = self.install(**extra)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertNoWrites()
        result = self.install(BUILTIN_WORKER_LANE="Private lane", BUILTIN_WORKER_BILLING="flat",
                              BUILTIN_WORKER_LIMIT="2", BUILTIN_WORKER_PROVIDER="custom:private")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("| Private lane | 2 |", self.roster())
        self.assertIn("| Worker (built-in)", self.roster().split("## Best-of-N candidates (flat lanes)")[1])

    def test_shared_builtin_lane_rejects_conflicting_metadata(self):
        result = self.install(BUILTIN_WORKER_LANE="Private lane", BUILTIN_WORKER_BILLING="flat",
                              BUILTIN_WORKER_LIMIT="2", BUILTIN_REVIEWER_LANE="Private lane",
                              BUILTIN_REVIEWER_BILLING="metered", BUILTIN_REVIEWER_LIMIT="1",
                              BUILTIN_WORKER_PROVIDER="custom:private", BUILTIN_REVIEWER_PROVIDER="custom:private")
        self.assertNotEqual(result.returncode, 0)
        self.assertNoWrites()

    def test_known_builtin_lane_adds_budget_without_optional_custom_provider(self):
        result = self.install(BUILTIN_WORKER_LANE="ChatGPT subscription", BUILTIN_WORKER_MODEL="gpt-6.1-sol", BUILTIN_WORKER_FAMILY="OpenAI")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("| ChatGPT subscription | 2 |", self.roster())

    def test_identity_registry_and_helpers_are_installed_for_each_skill(self):
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        contents = []
        for name in ("orchestrate", "adversarial", "isolated", "pr-review", "pr-review-batch"):
            skill = self.home / ".agents/skills" / name
            self.assertTrue((skill / "references/identity-registry.json").is_file())
            registry = (skill / "references/identity-registry.json").read_bytes()
            contents.append(registry)
            data = json.loads(registry)
            self.assertEqual(data["thread_limit"], 6)
            self.assertEqual(data["metered_max_spawns"], 2)
            self.assertIn({"provider_id": "custom:k", "lane": "Kimi Code", "billing": "flat", "limit": 3}, data["providers"])
            self.assertIn({"provider_id": "custom:k", "model_id": "k3", "family": "Kimi"}, data["models"])
            self.assertTrue((skill / "scripts/identity.py").is_file())
            self.assertTrue((skill / "references/effective-identity.md").is_file())
            self.assertNotIn("{{", (skill / "references/effective-identity.md").read_text())
        for registry in contents[1:]:
            self.assertEqual(contents[0], registry)
        models = self.base / "models.json"
        models.write_text(json.dumps([{"provider_id": "custom:k", "model_id": "k3", "model_name": "Fake Kimi"}]))
        skill = self.home / ".agents/skills/orchestrate"
        resolved = subprocess.run([sys.executable, str(skill / "scripts/identity.py"),
                                   "--models", str(models), "--registry", str(skill / "references/identity-registry.json"),
                                   "--provider", "custom:k", "--model", "k3"],
                                  cwd=self.base, env=self.env, text=True, capture_output=True)
        self.assertEqual(resolved.returncode, 0, resolved.stderr)
        identity = json.loads(resolved.stdout)
        self.assertEqual((identity["provider_id"], identity["model_id"], identity["family"], identity["lane"], identity["billing"], identity["limit"]),
                         ("custom:k", "k3", "Kimi", "Kimi Code", "flat", 3))

    def test_pr_review_skills_resolve_identity_without_sibling_skills(self):
        path = self.base / "skills with 'apostrophe' & space"
        models = self.base / "models.json"
        models.write_text(json.dumps([{"provider_id": "custom:k", "model_id": "k3"}]))
        for name in ("pr-review", "pr-review-batch"):
            with self.subTest(skill=name):
                result = self.install("--force", "--skill-dir", str(path))
                self.assertEqual(result.returncode, 0, result.stderr)
                skill = path / name
                body = (skill / "SKILL.md").read_text()
                roster = skill / re.search(r"Read `([^`]+)` relative", body).group(1)
                reference = skill / re.search(r"follow `([^`]+)` before", body, re.I).group(1)
                self.assertEqual(roster.read_bytes(), (path / "orchestrate/references/roster.md").read_bytes())
                for sibling in path.iterdir():
                    if sibling != skill:
                        shutil.rmtree(sibling)
                self.assertTrue(roster.is_file())
                for filename in ("repositories.md", "review-voice.md"):
                    self.assertIn("references/" + filename, body)
                    self.assertEqual((skill / "references" / filename).read_bytes(),
                                     (ROOT / "skills/pr-review/references" / filename).read_bytes())
                self.assertNotIn("llm-wiki-igo", (skill / "references/review-voice.md").read_text())
                command = next(line.strip() for line in reference.read_text().splitlines()
                               if line.strip().startswith("python3 "))
                command = (command.replace("<sanitized-models-json>", shlex.quote(str(models)))
                           .replace("<exact-provider-id>", "custom:k").replace("<exact-model-id>", "k3"))
                resolved = subprocess.run(["/bin/sh", "-c", command], cwd=self.base, env=self.env,
                                          text=True, capture_output=True)
                self.assertEqual(resolved.returncode, 0, resolved.stderr)
                lines = resolved.stdout.splitlines()
                self.assertEqual(lines.pop(), "exit=0", resolved.stdout + resolved.stderr)
                identity = json.loads("\n".join(lines))
                self.assertEqual((identity["provider_id"], identity["model_id"], identity["family"], identity["lane"]),
                                 ("custom:k", "k3", "Kimi", "Kimi Code"))

    def test_missing_settings_and_runtime_tools_refuse_before_writes(self):
        settings = self.config / "settings.json"
        settings.unlink()
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no settings.json", result.stderr)
        self.assertNoWrites()
        settings.write_text("{}\n")
        for name in ("gh", "node"):
            tool = self.base / "bin" / name
            contents = tool.read_text()
            tool.unlink()
            result = self.install()
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(name + " not found", result.stderr)
            self.assertNoWrites()
            tool.write_text(contents)
            tool.chmod(0o755)

    def test_builtin_pins_match_roster_and_preserve_other_settings(self):
        worker = self.config / "profiles/worker.toml"
        worker.parent.mkdir()
        before = 'worktree = "isolated"\nprompt = "Keep this prompt"\n[model.any]\nmodel = "custom:q/qwen3.8-max"\nthinking_effort = "low"\nother = true\n'
        worker.write_text(before)
        refused = self.install()
        self.assertNotEqual(refused.returncode, 0)
        self.assertEqual(worker.read_text(), before)
        result = self.install("--force", BUILTIN_WORKER_EFFORT="high")
        self.assertEqual(result.returncode, 0, result.stderr)
        import tomllib
        data = tomllib.loads(worker.read_text())
        self.assertEqual(data["model"]["any"], {"model": "custom:k/k3", "thinking_effort": "high", "other": True})
        self.assertTrue(data["prompt"].endswith("Keep this prompt"))
        saved = list((self.base / "state/zetta-delta/backups").glob("*/" + str(worker).lstrip("/")))
        self.assertEqual(saved[0].read_text(), before)

    def test_all_shipped_profiles_get_the_same_context_stack_preamble(self):
        result = self.install(GPT_PROVIDER="openai-subscribed", GROK_PROVIDER="x_ai-subscribed",
                              MINIMAX_PROVIDER="custom:m", MINIMAX_BILLING="plan",
                              COPILOT_PROVIDER="custom:c", LOCAL_PROVIDER="custom:l", LOCAL_MODEL="fixture-local")
        self.assertEqual(result.returncode, 0, result.stderr)
        profiles = list((self.config / "profiles").glob("*.toml"))
        self.assertEqual(len(profiles), 10)
        blocks = []
        for path in profiles:
            data = tomllib.loads(path.read_text())
            prompt = data.get("prompt", "")
            with self.subTest(profile=path.name):
                self.assertEqual(prompt.count("<!-- DELTA_SUBAGENT_CONTEXT_START v1 -->"), 1)
                self.assertEqual(prompt.count("<!-- DELTA_SUBAGENT_CONTEXT_END -->"), 1)
                block = prompt.split("<!-- DELTA_SUBAGENT_CONTEXT_END -->", 1)[0]
                for tool in ("tgrep", "semble", "codegraph", "zg", "ctx7", "githits", "rtk", "caveman"):
                    self.assertIn(tool, block)
                self.assertIn("--no-index", block)
                self.assertIn("SOURCE ROOT", block)
                self.assertNotIn("system_prompt", data)
                self.assertNotIn("system_prompt_file", data)
                blocks.append(block)
        self.assertEqual(len(set(blocks)), 1)

    def test_builtin_context_keeps_user_prompt_and_reinstall_is_idempotent(self):
        worker = self.config / "profiles/worker.toml"
        worker.parent.mkdir()
        worker.write_text('prompt = """Keep user instructions ✨\nSecond line: \\\"quoted\\\" text."""\n'
                          'system_prompt = "Keep user base"\nworktree = "isolated"\nuser_flag = true\n'
                          '[model.any]\nmodel = "custom:q/qwen3.8-max"\nother = true\n')
        original = tomllib.loads(worker.read_text())
        first = self.install("--force", BUILTIN_WORKER_EFFORT="high")
        self.assertEqual(first.returncode, 0, first.stderr)
        rendered = worker.read_bytes()
        data = tomllib.loads(rendered.decode())
        self.assertEqual(data["prompt"].split("<!-- DELTA_SUBAGENT_CONTEXT_END -->\n\n", 1)[1], original["prompt"])
        for key in ("system_prompt", "worktree", "user_flag"):
            self.assertEqual(data[key], original[key])
        self.assertEqual(data["model"]["any"]["other"], True)
        second = self.install("--force", BUILTIN_WORKER_EFFORT="high")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(worker.read_bytes(), rendered)

    def test_malformed_subagent_context_refuses_before_bundle_writes(self):
        worker = self.config / "profiles/worker.toml"
        worker.parent.mkdir()
        before = 'prompt = "<!-- DELTA_SUBAGENT_CONTEXT_START v1 -->\\nUSER_CANARY"\n[model.any]\nmodel = "custom:k/k3"\n'
        worker.write_text(before)
        result = self.install("--force")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("built-in WORKER preflight failed", result.stderr)
        self.assertNotIn("USER_CANARY", result.stdout + result.stderr)
        self.assertEqual(worker.read_text(), before)
        self.assertFalse((self.home / ".agents").exists())
        self.assertEqual(list((self.base / "state").iterdir()), [])

    def test_selected_custom_template_failures_preserve_settings_and_retirements(self):
        profiles = self.config / "profiles"
        profiles.mkdir()
        for name in ("worker", "reviewer", "scout", "worker-kimi"):
            (profiles / (name + ".toml")).write_text('prompt = "USER_CANARY"\n[model.any]\nmodel = "custom:old/keep"\n')
        (profiles / "worker-kimi.toml.retired").write_text("KEEP_RETIRED_CANARY\n")
        settings = self.config / "settings.json"
        settings.write_text('{"version":1,"native":{"private_canary":"LOCAL_SECRET_CANARY"}}\n')
        rules = self.home / ".config/delta/AGENTS.md"
        rules.parent.mkdir(parents=True)
        rules.write_text("KEEP_PERSONAL_RULES\n")
        ids = {role.upper() + "_PROVIDER": self.catalog_id(role) for role in ("kimi", "zai", "qwen")}
        roots = (self.config, self.home, self.base / "state")
        def snapshot():
            return {str(p): p.read_bytes() for root in roots for p in root.rglob("*") if p.is_file()}
        for template, mutation, flags in (("model", '\nBROKEN_INPUT_CANARY = [\n', ()),
                                          ("scout-qwen", '\nBROKEN_INPUT_CANARY = [\n', ()),
                                          ("model", '\nunknown = "{{MISSING_PLACEHOLDER}}"\n', ()),
                                          ("model", '\nBROKEN_INPUT_CANARY = [\n', ("--dry-run",))):
            with self.subTest(template=template, mutation=mutation, flags=flags):
                before = snapshot()
                source = self.bundle / "profiles" / (template + ".toml.tmpl")
                original = source.read_text()
                try:
                    source.write_text(original + mutation)
                    result = self.install("--force", "--prune-legacy", "--configure-delta", *flags, **ids)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertTrue(snapshot() == before, "installer changed destination files")
                    self.assertEqual(list((self.base / "state").iterdir()), [])
                    self.assertEqual(list((self.base / "tmp").iterdir()), [])
                    self.assertFalse((self.home / ".agents").exists())
                    self.assertNotIn("BROKEN_INPUT_CANARY", result.stdout + result.stderr)
                    self.assertNotIn("LOCAL_SECRET_CANARY", result.stdout + result.stderr)
                finally:
                    source.write_text(original)

    def test_invalid_shared_context_source_refuses_before_any_writes(self):
        source = self.bundle / "rules/subagent-context.md"
        original = source.read_text()
        for label, content in (("truncated", '<!-- DELTA_SUBAGENT_CONTEXT_START v1 -->\nCONTEXT_CANARY\n'),
                               ("placeholder", original.replace("Context/token stack:", "{{MISSING_PLACEHOLDER}}")),
                               ("duplicate", original + original),
                               ("outside-block", "CONTEXT_CANARY\n" + original),
                               ("missing", None)):
            with self.subTest(source=label):
                try:
                    if content is None:
                        source.unlink()
                    else:
                        source.write_text(content)
                    result = self.install("--force")
                    self.assertNotEqual(result.returncode, 0)
                    self.assertNoWrites()
                    self.assertNotIn("CONTEXT_CANARY", result.stdout + result.stderr)
                finally:
                    source.write_text(original)

    def test_malformed_unselected_custom_template_does_not_block(self):
        (self.bundle / "profiles/scout-gemini.toml.tmpl").write_text('BROKEN_INPUT_CANARY = [\n')
        result = self.install("--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNoWrites()
        self.assertEqual(list((self.base / "tmp").iterdir()), [])

    def test_configure_delta_seeds_scrubbed_providers_and_preserves_local_secrets(self):
        settings = self.config / "settings.json"
        local = {"version": 1, "native": {"private_canary": "LOCAL_SECRET_CANARY", "custom_providers": []},
                 "portable": {"appearance": "dark", "subagent_defaults": {"unrelated": True}}}
        settings.write_text(json.dumps(local))
        providers = json.loads((self.bundle / "settings/provider-catalog.json").read_text())["providers"]
        import hashlib
        ids = {name.upper() + "_PROVIDER": "custom:" + hashlib.sha256(providers[name]["base_url"].encode()).hexdigest()
               for name in ("kimi", "zai", "qwen")}
        result = self.install("--configure-delta", "--force", **ids)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(settings.read_text())
        self.assertEqual(data["native"]["private_canary"], "LOCAL_SECRET_CANARY")
        self.assertNotIn("LOCAL_SECRET_CANARY", result.stdout + result.stderr)
        self.assertNotIn("LOCAL_SECRET_CANARY", (self.bundle / "settings/provider-catalog.json").read_text())
        self.assertEqual(data["portable"]["appearance"], "dark")
        self.assertEqual(data["portable"]["subagent_concurrency"], {"maximum_per_parent": 6, "maximum_total": 12})
        self.assertEqual(data["portable"]["subagent_defaults"], {"unrelated": True, "allow_parent_model_override": True})
        self.assertEqual(len(data["native"]["custom_providers"]), 3)
        self.assertTrue(all(row["headers"] == [] for row in data["native"]["custom_providers"]))
        self.assertEqual(settings.stat().st_mode & 0o777, 0o600)
        saved = list((self.base / "state/zetta-delta/backups").glob("*/" + str(settings).lstrip("/")))
        self.assertEqual(saved[0].stat().st_mode & 0o777, 0o600)
        provider = data["native"]["custom_providers"][0]
        provider["headers"] = [{"name": "Authorization", "value": "LOCAL_HEADER_CANARY"}]
        settings.write_text(json.dumps(data))
        result = self.install("--configure-delta", "--force", **ids)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("LOCAL_HEADER_CANARY", result.stdout + result.stderr)
        self.assertEqual(json.loads(settings.read_text())["native"]["custom_providers"][0]["headers"], provider["headers"])

    def test_configure_delta_initializes_missing_settings_and_provider_ids(self):
        (self.config / "settings.json").unlink()
        result = self.install("--configure-delta", KIMI_PROVIDER="", ZAI_PROVIDER="", QWEN_PROVIDER="", THREAD_CAP="8")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads((self.config / "settings.json").read_text())
        self.assertEqual(data["portable"]["subagent_concurrency"], {"maximum_per_parent": 8, "maximum_total": 16})
        import tomllib
        pins = {name: tomllib.loads((self.config / "profiles" / (name + ".toml")).read_text())["model"]["any"]["model"]
                for name in ("scout", "worker", "reviewer")}
        for name in pins:
            data = tomllib.loads((self.config / "profiles" / (name + ".toml")).read_text())
            self.assertEqual(data["model"]["any"]["thinking_effort"], "high")
        self.assertTrue(pins["worker"].endswith("/k3"))
        self.assertTrue(pins["reviewer"].endswith("/glm-5.3"))
        registry = json.loads((self.home / ".agents/skills/pr-review/references/identity-registry.json").read_text())
        bindings = {row["provider_id"] + "/" + row["model_id"] for row in registry["models"]}
        self.assertTrue(set(pins.values()) <= bindings)

    def test_settings_link_backups_are_private_snapshots_and_preserve_targets(self):
        settings = self.config / "settings.json"
        target = self.config / "original.json"
        original = '{"native":{"canary":"LOCAL_LINK_CANARY"}}\n'
        backups = self.base / "state/zetta-delta/backups"
        backups.mkdir(parents=True)
        for kind in ("relative", "absolute", "dangling"):
            with self.subTest(kind=kind):
                settings.unlink()
                if kind == "dangling":
                    target.unlink()
                else:
                    target.write_text(original)
                    target.chmod(0o640)
                link = str(target) if kind == "absolute" else target.name
                settings.symlink_to(link)
                before = set(backups.iterdir())
                result = self.install("--configure-delta", "--force",
                                      KIMI_PROVIDER="", ZAI_PROVIDER="", QWEN_PROVIDER="")
                self.assertEqual(result.returncode, 0, result.stderr)
                saved = (set(backups.iterdir()) - before).pop() / str(settings).lstrip("/")
                saved_link = Path(str(saved) + ".link")
                self.assertTrue(saved_link.is_symlink())
                self.assertEqual(os.readlink(saved_link), link)
                self.assertFalse(settings.is_symlink())
                self.assertEqual(settings.stat().st_mode & 0o777, 0o600)
                if kind == "dangling":
                    self.assertFalse(saved.exists())
                    self.assertFalse(target.exists())
                else:
                    self.assertFalse(saved.is_symlink())
                    self.assertEqual(saved.read_text(), original)
                    self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
                    self.assertEqual(target.read_text(), original)
                    self.assertEqual(target.stat().st_mode & 0o777, 0o640)
                    self.assertEqual(json.loads(settings.read_text())["native"]["canary"], "LOCAL_LINK_CANARY")
                    self.assertNotIn("LOCAL_LINK_CANARY", result.stdout + result.stderr)

    def test_builtin_efforts_follow_exact_catalog_identity(self):
        worker = self.config / "profiles/worker.toml"
        cases = [
            (self.catalog_id("minimax"), "MiniMax-M3", "MiniMax", "MiniMax", {}),
            (self.catalog_id("qwen"), "qwen3.8-max", "Qwen Token Plan", "Qwen", {}),
            (self.catalog_id("kimi"), "k3", "Kimi Code", "Kimi", {"thinking_effort": "high"}),
            ("custom:k", "k3", "Kimi Code", "Kimi", {}),
        ]
        for provider, model, lane, family, effort in cases:
            with self.subTest(provider=provider, model=model):
                result = self.install("--force", MINIMAX_PROVIDER=self.catalog_id("minimax"),
                                      KIMI_PROVIDER=provider if lane == "Kimi Code" else "custom:k",
                                      QWEN_PROVIDER=provider if lane == "Qwen Token Plan" else "custom:q",
                                      BUILTIN_WORKER_PROVIDER=provider, BUILTIN_WORKER_MODEL=model,
                                      BUILTIN_WORKER_LANE=lane, BUILTIN_WORKER_FAMILY=family)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(tomllib.loads(worker.read_text())["model"]["any"],
                                 {"model": provider + "/" + model, **effort})

    def test_builtin_explicit_efforts_validate_before_writes(self):
        minimax = dict(MINIMAX_PROVIDER=self.catalog_id("minimax"), BUILTIN_WORKER_MODEL="MiniMax-M3",
                       BUILTIN_WORKER_LANE="MiniMax", BUILTIN_WORKER_FAMILY="MiniMax")
        qwen = dict(QWEN_PROVIDER=self.catalog_id("qwen"), BUILTIN_WORKER_MODEL="qwen3.8-max",
                    BUILTIN_WORKER_LANE="Qwen Token Plan", BUILTIN_WORKER_FAMILY="Qwen")
        for extra, effort in ((minimax, "high"), (minimax, "max"), (qwen, "high")):
            result = self.install("--force", BUILTIN_WORKER_EFFORT=effort, **extra)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("built-in WORKER preflight failed", result.stderr)
            self.assertNoWrites()
        for effort in ("on", "off", ""):
            result = self.install("--force", BUILTIN_WORKER_EFFORT=effort, **minimax)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = tomllib.loads((self.config / "profiles/worker.toml").read_text())["model"]["any"]
            self.assertEqual(data.get("thinking_effort"), effort or None)
        result = self.install("--force", BUILTIN_WORKER_EFFORT="xhigh", **qwen)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = tomllib.loads((self.config / "profiles/worker.toml").read_text())["model"]["any"]
        self.assertEqual(data["thinking_effort"], "xhigh")

    def test_desktop_path_retains_separate_tool_and_runtime_directories(self):
        custom = self.base / "tools with 'quotes' & $dollar ;"
        runtime = self.base / "runtime"
        custom.mkdir()
        runtime.mkdir()
        for name, directory in (("githits", custom), ("node", runtime), ("python3", runtime)):
            (self.base / "bin" / name).rename(directory / name)
        (custom / "githits").write_text('#!/usr/bin/env node\n')
        (runtime / "node").write_text('#!/bin/sh\nprintf "RUNTIME_OK\\n"\n')
        ambient = self.base / "unrelated"
        ambient.mkdir()
        path = ":".join(map(str, (custom, runtime, ambient, self.base / "bin"))) + ":/usr/bin:/bin"
        result = self.install(PATH=path)
        self.assertEqual(result.returncode, 0, result.stderr)
        rules = (self.home / ".config/delta/AGENTS.md").read_text()
        prefix = re.search(r"`(export PATH=.*?;)`", rules).group(1)
        command = prefix + ' githits; python3 -c "import tomllib"; printf "%s\\n" "$PATH"'
        ran = subprocess.run(["/bin/sh", "-e", "-c", command],
                             env={"PATH": "/usr/bin:/bin", "HOME": str(self.home)},
                             cwd=self.base, capture_output=True, text=True)
        self.assertEqual(ran.returncode, 0, ran.stderr)
        self.assertEqual(ran.stdout.splitlines()[0], "RUNTIME_OK")
        directories = ran.stdout.splitlines()[1].split(":")
        self.assertIn(str(custom), directories)
        self.assertEqual(directories.count(str(runtime)), 1)
        self.assertNotIn(str(ambient), directories)
        for name in ("worker", "reviewer", "scout", "qwen-max"):
            prompt = tomllib.loads((self.config / "profiles" / (name + ".toml")).read_text())["prompt"]
            profile_prefix = re.search(r"`(export PATH=.*?;)`", prompt).group(1)
            profile_ran = subprocess.run(["/bin/sh", "-e", "-c", command.replace(prefix, profile_prefix, 1)],
                                        env={"PATH": "/usr/bin:/bin", "HOME": str(self.home)},
                                        cwd=self.base, capture_output=True, text=True)
            self.assertEqual(profile_ran.returncode, 0, profile_ran.stderr)
            self.assertEqual(profile_ran.stdout, ran.stdout)

    def test_bootstrap_orders_prerequisites_tools_and_configuration(self):
        stack = self.bundle / "stack"
        stack.mkdir()
        order = self.base / "tmp/order"
        (stack / "install.sh").write_text('#!/bin/sh\nprintf "STACK\\n" >> "$TMPDIR/order"\n')
        brew = self.base / "bin/brew"
        brew.write_text('#!/bin/sh\ncase "$1" in bundle) printf "BREW\\n" >> "$TMPDIR/order";; '
                        '--prefix) printf "%s\\n" ' + shlex.quote(str(self.base / "brew")) + ';; *) exit 2;; esac\n')
        brew.chmod(0o755)
        env = {key: value for key, value in self.env.items() if key not in ("KIMI_PROVIDER", "ZAI_PROVIDER", "QWEN_PROVIDER")}
        dry = subprocess.run([BASH, str(self.bundle / "bootstrap.sh"), "--dry-run"], env=env,
                             cwd=self.base, text=True, capture_output=True)
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertFalse(order.exists())
        self.assertNoWrites()
        ran = subprocess.run([BASH, str(self.bundle / "bootstrap.sh"), "--force"], env=env,
                             cwd=self.base, text=True, capture_output=True)
        self.assertEqual(ran.returncode, 0, ran.stderr)
        self.assertEqual(order.read_text().splitlines(), ["BREW", "STACK"])
        data = json.loads((self.config / "settings.json").read_text())
        self.assertEqual(len(data["native"]["custom_providers"]), 4)
        self.assertEqual(data["portable"]["subagent_concurrency"], {"maximum_per_parent": 8, "maximum_total": 16})
        self.assertIn("| MiniMax | 2 |", self.roster())
        self.assertIn("| grok |", self.roster())
        self.assertIn("| gpt-sol |", self.roster())

    def test_running_delta_and_complex_builtin_layout_refuse_before_writes(self):
        (self.base / "bin/pgrep").write_text("#!/bin/sh\nexit 0\n")
        result = self.install("--force")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("quit Delta", result.stderr)
        self.assertNoWrites()
        (self.base / "bin/pgrep").write_text("#!/bin/sh\nexit 1\n")
        worker = self.config / "profiles/worker.toml"
        worker.parent.mkdir()
        before = 'model.any.model = "custom:q/qwen3.8-max"\n'
        worker.write_text(before)
        result = self.install("--force")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(worker.read_text(), before)
        self.assertFalse((self.home / ".agents").exists())
        self.assertEqual(list((self.base / "state").iterdir()), [])

    def test_all_optional_lanes_keep_primary_families_before_duplicates(self):
        result = self.install(GPT_PROVIDER="openai-subscribed", GROK_PROVIDER="x_ai-subscribed",
                              MINIMAX_PROVIDER="custom:m", MINIMAX_BILLING="plan",
                              COPILOT_PROVIDER="custom:c", LOCAL_PROVIDER="custom:l", LOCAL_MODEL="fixture-local", LOCAL_FAMILY="Qwen")
        self.assertEqual(result.returncode, 0, result.stderr)
        active = {path.stem for path in (self.config / "profiles").glob("*.toml")} - {"worker", "reviewer", "scout"}
        self.assertEqual(active, {"qwen-max", "deepseek-pro", "grok", "gpt-sol", "minimax", "scout-gemini", "scout-local"})
        for name in active:
            profile = (self.config / "profiles" / (name + ".toml")).read_text()
            self.assertIn("REVIEW-PR", profile)
            if not name.startswith("scout-"):
                self.assertIn("VET", profile)
                self.assertIn("read-only analysis", profile)
        self.assertIn("PONYTAIL_START 4.13.0", (self.home / ".config/delta/AGENTS.md").read_text())

    def test_malformed_settings_and_catalog_secrets_refuse_before_writes(self):
        settings = self.config / "settings.json"
        settings.write_text('{"native":"LOCAL_SECRET_CANARY"}')
        result = self.install("--force")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("LOCAL_SECRET_CANARY", result.stdout + result.stderr)
        self.assertNoWrites()
        settings.write_text("{}\n")
        catalog = self.bundle / "settings/provider-catalog.json"
        data = json.loads(catalog.read_text())
        data["providers"]["kimi"]["headers"] = {"Authorization": "CATALOG_SECRET_CANARY"}
        catalog.write_text(json.dumps(data))
        result = self.install("--force")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("CATALOG_SECRET_CANARY", result.stdout + result.stderr)
        self.assertNoWrites()

    def test_flat_lane_cannot_be_declared_metered_elsewhere(self):
        result = self.install(METERED_LANES="Qwen Token Plan")
        self.assertNotEqual(result.returncode, 0)
        self.assertNoWrites()

    def test_reprioritization_retires_unselected_managed_profiles(self):
        providers = {"GPT_PROVIDER": "openai-subscribed", "GROK_PROVIDER": "x_ai-subscribed",
                     "MINIMAX_PROVIDER": "custom:m", "MINIMAX_BILLING": "plan",
                     "COPILOT_PROVIDER": "custom:c", "LOCAL_PROVIDER": "custom:l", "LOCAL_MODEL": "fake-local"}
        first = self.install(**providers)
        self.assertEqual(first.returncode, 0, first.stderr)
        all_before = {p.stem: p.read_bytes() for p in (self.config / "profiles").glob("*.toml")}
        old = {name: contents for name, contents in all_before.items() if name not in ("worker", "reviewer", "scout")}
        priority = dict(providers, PROFILE_PRIORITY="scout-local scout-gemini gpt-astra")
        refused = self.install(**priority)
        self.assertNotEqual(refused.returncode, 0)
        self.assertEqual({p.stem: p.read_bytes() for p in (self.config / "profiles").glob("*.toml")}, all_before)
        second = self.install("--force", **priority)
        self.assertEqual(second.returncode, 0, second.stderr)
        active = sorted(p.stem for p in (self.config / "profiles").glob("*.toml") if p.stem not in ("worker", "reviewer", "scout"))
        self.assertEqual(len(active), 7, active)
        self.assertTrue({"scout-local", "scout-gemini", "gpt-astra"}.issubset(active))
        for name in set(old) - set(active):
            self.assertEqual((self.config / "profiles" / (name + ".toml.retired")).read_bytes(), old[name])
        for line in self.roster().splitlines():
            if line.startswith("| ") and line.split("|")[1].strip() in old:
                self.assertIn(line.split("|")[1].strip(), active)

    def test_unmanaged_profiles_over_cap_refuse_before_writes(self):
        profiles = self.config / "profiles"
        profiles.mkdir()
        for i in range(8):
            (profiles / ("own-%d.toml" % i)).write_text("name='own'\n")
        result = self.install("--force")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(list(profiles.glob("*.toml"))), 8)
        self.assertFalse((self.home / ".agents").exists())

    def test_dangling_profile_is_preserved_without_force_and_link_backed_up_with_force(self):
        profiles = self.config / "profiles"
        profiles.mkdir()
        profile = profiles / "qwen-max.toml"
        profile.symlink_to("../missing-profile.toml")
        refused = self.install()
        self.assertNotEqual(refused.returncode, 0)
        self.assertTrue(profile.is_symlink())
        self.assertFalse((self.home / ".agents").exists())
        forced = self.install("--force")
        self.assertEqual(forced.returncode, 0, forced.stderr)
        self.assertFalse(profile.is_symlink())
        saved = [entry / str(profile).lstrip("/") for entry in (self.base / "state/zetta-delta/backups").iterdir()]
        self.assertEqual(len(saved), 1)
        self.assertTrue(saved[0].is_symlink())
        self.assertEqual(os.readlink(saved[0]), "../missing-profile.toml")

    def test_relative_config_backup_is_inside_the_per_install_directory(self):
        run = self.base / "run"
        run.mkdir()
        profiles = self.config / "profiles"
        profiles.mkdir()
        profile = profiles / "qwen-max.toml"
        profile.write_text("old profile\n")
        result = self.install("--force", cwd=run, DELTA_CONFIG_DIR="../delta")
        self.assertEqual(result.returncode, 0, result.stderr)
        backups = self.base / "state/zetta-delta/backups"
        directories = list(backups.iterdir())
        self.assertEqual(len(directories), 1)
        self.assertRegex(directories[0].name, r"^\d{8}T\d{6}Z-\d+$")
        self.assertEqual((directories[0] / str(profile).lstrip("/")).read_text(), "old profile\n")
        self.assertIn('qwen3.8-max', profile.read_text())

    def test_config_normalization_preserves_symlink_parent_semantics(self):
        physical = self.base / "physical/parent"
        child = physical / "child"
        child.mkdir(parents=True)
        alias = self.base / "alias"
        alias.symlink_to(child, target_is_directory=True)
        config = physical / "alternate-delta"
        profiles = config / "profiles"
        profiles.mkdir(parents=True)
        profile = profiles / "qwen-max.toml"
        profile.write_text("old physical profile\n")
        (config / "settings.json").write_text("{}\n")
        result = self.install("--force", DELTA_CONFIG_DIR=str(alias / "../alternate-delta"))
        self.assertEqual(result.returncode, 0, result.stderr)
        directories = list((self.base / "state/zetta-delta/backups").iterdir())
        self.assertTrue((directories[0] / str(profile).lstrip("/")).is_file())
        self.assertEqual((directories[0] / str(profile).lstrip("/")).read_text(), "old physical profile\n")
        self.assertFalse((self.base / "alternate-delta").exists())

    def test_equal_model_ids_on_distinct_providers_have_distinct_family_bindings(self):
        result = self.install(BUILTIN_WORKER_MODEL="default", BUILTIN_WORKER_FAMILY="Kimi",
                              BUILTIN_REVIEWER_MODEL="default", BUILTIN_REVIEWER_FAMILY="GLM")
        self.assertEqual(result.returncode, 0, result.stderr)
        skill = self.home / ".agents/skills/orchestrate"
        registry = skill / "references/identity-registry.json"
        records = json.loads(registry.read_text())["models"]
        self.assertIn({"provider_id": "custom:k", "model_id": "default", "family": "Kimi"}, records)
        self.assertIn({"provider_id": "custom:z", "model_id": "default", "family": "GLM"}, records)
        models = self.base / "models.json"
        models.write_text(json.dumps([{"provider_id": provider, "model_id": "default"} for provider in ("custom:k", "custom:z")]))
        for provider, family in (("custom:k", "Kimi"), ("custom:z", "GLM")):
            resolved = subprocess.run([sys.executable, str(skill / "scripts/identity.py"),
                                       "--models", str(models), "--registry", str(registry),
                                       "--provider", provider, "--model", "default"],
                                      cwd=self.base, env=self.env, text=True, capture_output=True)
            self.assertEqual(resolved.returncode, 0, resolved.stderr)
            self.assertEqual(json.loads(resolved.stdout)["family"], family)

    def test_helper_argument_roundtrips_special_skill_directory(self):
        # This synthetic command also specifies the shared installer/template API.
        template = self.bundle / "skills/isolated/SKILL.md"
        template.write_text("sh {{BON_SH}} fixture-probe\n")
        helper = self.bundle / "skills/orchestrate/scripts/bon.sh"
        helper.write_text("#!/bin/sh\nprintf '%s\\n' HELPER_OK\n")
        path = self.base / "skills with space & 'quotes' \\ | ; $dollar #"
        result = self.install("--skill-dir", str(path))
        self.assertEqual(result.returncode, 0, result.stderr)
        command = (path / "isolated/SKILL.md").read_text().strip()
        self.assertEqual(shlex.split(command), ["sh", str(path / "isolated/scripts/bon.sh"), "fixture-probe"])
        ran = subprocess.run(["/bin/sh", "-c", command], env=self.env, cwd=self.base, capture_output=True, text=True)
        self.assertEqual(ran.returncode, 0, ran.stderr)
        self.assertEqual(ran.stdout.strip(), "HELPER_OK")

    def test_actual_orchestrate_snapshot_command_is_rendered_and_runs(self):
        path = self.base / "skills with 'apostrophe' & space"
        result = self.install("--skill-dir", str(path))
        self.assertEqual(result.returncode, 0, result.stderr)
        for document in path.rglob("*.md"):
            self.assertNotRegex(document.read_text(), r"\{\{[A-Z_]*\}\}", str(document))
        body = (path / "orchestrate/SKILL.md").read_text()
        command = next(match.group(1) for match in re.finditer(r"`(sh [^`]+ snapshot)`", body))
        checkout = self.base / "checkout"
        checkout.mkdir()
        env = dict(self.env, XDG_CACHE_HOME=str(self.base / "cache"), GIT_CONFIG_GLOBAL="/dev/null",
                   GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="Fixture", GIT_AUTHOR_EMAIL="fixture@example.invalid",
                   GIT_COMMITTER_NAME="Fixture", GIT_COMMITTER_EMAIL="fixture@example.invalid")
        initialized = subprocess.run(["/usr/bin/git", "-c", "core.hooksPath=/dev/null", "init", str(checkout)],
                                     env=env, text=True, capture_output=True)
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        (checkout / "file.txt").write_text("fixture content\n")
        snapshot = subprocess.run(["/bin/sh", "-c", command], env=env, cwd=checkout, text=True, capture_output=True)
        self.assertEqual(snapshot.returncode, 0, snapshot.stderr + snapshot.stdout)
        self.assertIn("RESULT: ok:", snapshot.stdout)
        self.assertIn("TREE=", snapshot.stdout)

    def test_relative_skill_directory_renders_absolute_helper_argument(self):
        template = self.bundle / "skills/isolated/SKILL.md"
        template.write_text("sh {{BON_SH}} fixture-probe\n")
        result = self.install("--skill-dir", "relative skills")
        self.assertEqual(result.returncode, 0, result.stderr)
        path = self.base / "relative skills"
        command = (path / "isolated/SKILL.md").read_text().strip()
        self.assertEqual(shlex.split(command), ["sh", str(path / "isolated/scripts/bon.sh"), "fixture-probe"])

    def test_excess_parent_skill_path_keeps_backups_in_the_install_directory(self):
        run = self.base / "run"
        run.mkdir()
        skills = self.base / "custom-skills"
        previous = skills / "orchestrate/SKILL.md"
        previous.parent.mkdir(parents=True)
        previous.write_text("old custom skill\n")
        # Extra parents clamp at / for the target, but previously escaped the
        # backup prefix. The entire target and any escaped backup stay in this fixture.
        relative = "../" * len(run.parts) + str(skills).lstrip("/")
        result = self.install("--force", "--skill-dir", relative, cwd=run)
        self.assertEqual(result.returncode, 0, result.stderr)
        backups = list((self.base / "state/zetta-delta/backups").iterdir())
        self.assertEqual(len(backups), 1)
        self.assertRegex(backups[0].name, r"^\d{8}T\d{6}Z-\d+$")
        saved = backups[0] / str(previous).lstrip("/")
        self.assertEqual(saved.read_text(), "old custom skill\n")
        body = previous.read_text()
        command = next(match.group(1) for match in re.finditer(r"`(sh [^`]+ snapshot)`", body))
        self.assertEqual(shlex.split(command), ["sh", str(skills / "orchestrate/scripts/bon.sh"), "snapshot"])

    def test_skill_normalization_preserves_symlink_parent_semantics(self):
        physical = self.base / "physical/parent"
        child = physical / "child"
        child.mkdir(parents=True)
        alias = self.base / "alias"
        alias.symlink_to(child, target_is_directory=True)
        skills = physical / "custom-skills"
        previous = skills / "orchestrate/SKILL.md"
        previous.parent.mkdir(parents=True)
        previous.write_text("old physical skill\n")
        result = self.install("--force", "--skill-dir", "alias/../custom-skills")
        self.assertEqual(result.returncode, 0, result.stderr)
        backups = list((self.base / "state/zetta-delta/backups").iterdir())
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / str(previous).lstrip("/")).read_text(), "old physical skill\n")
        self.assertFalse((self.base / "custom-skills").exists())
        self.assertTrue(alias.is_symlink())
        command = next(match.group(1) for match in re.finditer(r"`(sh [^`]+ snapshot)`", previous.read_text()))
        self.assertEqual(shlex.split(command), ["sh", str(skills / "orchestrate/scripts/bon.sh"), "snapshot"])

    def test_nondirectory_skill_parent_refuses_before_writes(self):
        parent = self.base / "file-parent"
        parent.write_text("keep this file\n")
        result = self.install("--skill-dir", str(parent / "skills"))
        self.assertNotEqual(result.returncode, 0)
        self.assertNoWrites()
        self.assertEqual(parent.read_text(), "keep this file\n")

    def test_template_token_skill_directory_refuses_before_writes(self):
        result = self.install("--skill-dir", str(self.base / "skills{{UNKNOWN}}"))
        self.assertNotEqual(result.returncode, 0)
        self.assertNoWrites()

    def test_newline_skill_directory_refuses_before_writes(self):
        result = self.install("--skill-dir", str(self.base / "skills\nnewline"))
        self.assertNotEqual(result.returncode, 0)
        self.assertNoWrites()

    def test_duplicate_priority_refuses_before_writes(self):
        result = self.install(PROFILE_PRIORITY="scout-qwen scout-qwen")
        self.assertNotEqual(result.returncode, 0)
        self.assertNoWrites()

if __name__ == "__main__":
    unittest.main(verbosity=2)
