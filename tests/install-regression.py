#!/usr/bin/env python3
"""Offline installer regressions; all writes stay under fresh temporary fixtures."""
import os
import re
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
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
        for directory in ("profiles", "rules", "skills"):
            shutil.copytree(ROOT / directory, self.bundle / directory)
        self.home = self.base / "home"
        self.config = self.base / "delta"
        for directory in (self.home, self.config, self.base / "tmp", self.base / "state", self.base / "bin"):
            directory.mkdir()
        (self.config / "settings.json").write_text("{}\n")
        curl = self.base / "bin/curl"
        curl.write_text("#!/bin/sh\nexit 77\n")
        curl.chmod(0o755)
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
        for name in ("orchestrate", "adversarial", "isolated", "pr-review"):
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

    def test_pr_review_resolves_identity_without_sibling_skills(self):
        path = self.base / "skills with 'apostrophe' & space"
        result = self.install("--skill-dir", str(path))
        self.assertEqual(result.returncode, 0, result.stderr)
        skill = path / "pr-review"
        body = (skill / "SKILL.md").read_text()
        roster = skill / re.search(r"Read `([^`]+)` relative", body).group(1)
        reference = skill / re.search(r"follow `([^`]+)` before", body).group(1)
        self.assertEqual(roster.read_bytes(), (path / "orchestrate/references/roster.md").read_bytes())
        for sibling in ("orchestrate", "adversarial", "isolated"):
            shutil.rmtree(path / sibling)
        self.assertTrue(roster.is_file())
        command = next(line.strip() for line in reference.read_text().splitlines()
                       if line.strip().startswith("python3 "))
        models = self.base / "models.json"
        models.write_text(json.dumps([{"provider_id": "custom:k", "model_id": "k3"}]))
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
        old = {p.stem: p.read_bytes() for p in (self.config / "profiles").glob("*.toml")}
        priority = dict(providers, PROFILE_PRIORITY="scout-local scout-gemini gpt-astra")
        refused = self.install(**priority)
        self.assertNotEqual(refused.returncode, 0)
        self.assertEqual({p.stem: p.read_bytes() for p in (self.config / "profiles").glob("*.toml")}, old)
        second = self.install("--force", **priority)
        self.assertEqual(second.returncode, 0, second.stderr)
        active = sorted(p.stem for p in (self.config / "profiles").glob("*.toml"))
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
