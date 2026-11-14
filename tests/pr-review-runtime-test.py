#!/usr/bin/env python3
"""Exercise portable runtime identities and atomic release installation offline."""

import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("review_runtime", ROOT / "review/scripts/runtime.py")
assert SPEC is not None and SPEC.loader is not None
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="review-runtime-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.source = self.base / "source"
        self.source.mkdir()
        for name, path in runtime.install_sources().items():
            shutil.copy2(path, self.source / name)
        self.destination = self.base / "review"
        self.backup = self.base / "backup"

    def install(self, **kwargs):
        return runtime.install(self.destination, self.backup, sources={p.name: p for p in self.source.iterdir()}, **kwargs)

    def test_identity_is_portable_and_detects_owned_code_and_schema_drift(self):
        original = runtime.identity()
        with patch.object(runtime, "__file__", str(self.source / "runtime.py")):
            self.assertEqual(runtime.identity(), original)
            runtime.verify_recorded(original)
            (self.source / "workflow.py").write_text("# changed implementation\n")
            with self.assertRaisesRegex(ValueError, "runtime.*differs"):
                runtime.verify_recorded(original)
        for malformed in (None, {}, dict(original, schema_version=True), dict(original, schema_version=999),
                          dict(original, fingerprint="0" * 64), dict(original, files={})):
            with self.subTest(expected=malformed), self.assertRaises(ValueError):
                runtime.verify_recorded(malformed)

    def test_external_helpers_do_not_change_runtime_identity(self):
        with patch.object(runtime, "__file__", str(self.source / "runtime.py")):
            expected = runtime.identity()
            (self.source / "identity.py").write_text("# independent identity helper\n")
            (self.source / "ensure-context-indexes.py").write_text("# independent index helper\n")
            self.assertEqual(runtime.identity(), expected)

    def test_manifest_is_verified_and_legacy_files_are_preserved_before_switch(self):
        scripts = self.destination / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "local.py").write_text("preserve this legacy runtime\n")
        with self.assertRaisesRegex(ValueError, "force"):
            self.install()
        self.assertFalse((self.destination / "releases").exists())
        result = self.install(force=True)
        self.assertTrue(scripts.is_symlink())
        pinned = scripts.resolve()
        self.assertTrue(pinned.is_relative_to(self.destination / "releases"))
        self.assertEqual((Path(result["backup"]) / "local.py").read_text(), "preserve this legacy runtime\n")
        with patch.object(runtime, "__file__", str(pinned / "runtime.py")):
            expected = runtime.identity()
            self.assertEqual(expected, result["runtime"])
            manifest = pinned / "runtime-manifest.json"
            data = json.loads(manifest.read_text())
            data["runtime"]["fingerprint"] = "0" * 64
            manifest.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, "manifest"):
                runtime.identity()

    def test_release_switch_keeps_old_release_pinned_and_same_install_is_idempotent(self):
        first = self.install()
        scripts = self.destination / "scripts"
        old = scripts.resolve()
        original = (old / "workflow.py").read_bytes()
        self.assertFalse(self.install(force=True)["changed"])
        (self.source / "workflow.py").write_text("# second release\n")
        second = self.install(force=True)
        self.assertNotEqual(scripts.resolve(), old)
        self.assertEqual((old / "workflow.py").read_bytes(), original)
        self.assertNotEqual(first["runtime"], second["runtime"])
        self.assertTrue(Path(second["backup"]).is_symlink())
        self.assertEqual(Path(second["backup"]).resolve(), old)

    def test_dry_run_and_source_race_leave_the_active_release_unchanged(self):
        self.install()
        scripts = self.destination / "scripts"
        original = scripts.resolve()
        (self.source / "workflow.py").write_text("# candidate release\n")
        self.install(force=True, dry_run=True)
        self.assertEqual(scripts.resolve(), original)
        original_copy = runtime.shutil.copy2

        def changed_during_copy(source, target, *args, **kwargs):
            result = original_copy(source, target, *args, **kwargs)
            if Path(source).name == "workflow.py":
                Path(target).write_text("# raced bytes\n")
            return result

        with (patch.object(runtime.shutil, "copy2", side_effect=changed_during_copy),
              self.assertRaisesRegex(ValueError, "source changed")):
            self.install(force=True)
        self.assertEqual(scripts.resolve(), original)
        self.assertEqual(list((self.destination / "releases").glob(".pending-*")), [])

    def test_redirected_release_paths_are_refused_without_external_mutation(self):
        external = self.base / "external"
        external.mkdir()
        self.destination.mkdir()
        (self.destination / "scripts").symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "owned release"):
            self.install(force=True)
        self.assertEqual(list(external.iterdir()), [])
        self.assertFalse((self.destination / "releases").exists())

    def test_forced_install_can_replace_a_broken_manifest_without_changing_old_bytes(self):
        self.install()
        old = (self.destination / "scripts").resolve()
        (old / "runtime-manifest.json").write_text("broken receipt\n")
        result = self.install(force=True)
        self.assertTrue(result["changed"])
        self.assertEqual((old / "runtime-manifest.json").read_text(), "broken receipt\n")
        with patch.object(runtime, "__file__", str(self.destination / "scripts/runtime.py")):
            self.assertEqual(runtime.identity(), result["runtime"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
