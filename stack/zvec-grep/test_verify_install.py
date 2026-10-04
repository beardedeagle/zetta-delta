"""The zg guard accepts the build it pinned and rejects any other.

Run with: python3 -m unittest (in this folder).
"""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

GUARD = Path(__file__).with_name("verify-install.py")


class PinAndVerify(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory(prefix="zg-guard-test-")
        self.addCleanup(work.cleanup)
        self.tools = Path(work.name) / "tools"
        self.package = Path(work.name) / "package"
        self.tools.mkdir()
        shutil.copy2(GUARD, self.tools / GUARD.name)
        (self.package / "dist/engine").mkdir(parents=True)
        self.write_version("0.2.2+zetta-delta.0123456789ab")
        (self.package / "dist/index.js").write_text("export {};\n")
        (self.package / "dist/engine/config.js").write_text("export const metal = false;\n")

    def write_version(self, version):
        (self.package / "package.json").write_text(
            json.dumps({"name": "@zvec/zvec-grep", "version": version}))

    def guard(self, *args):
        return subprocess.run([sys.executable, str(self.tools / GUARD.name), *args],
                              capture_output=True, text=True, check=False)

    def assert_verdict(self, expected):
        result = self.guard(str(self.package))
        self.assertEqual(result.returncode, expected, result.stderr)
        self.assertNotIn(str(self.package), result.stderr)

    def test_pin_covers_every_dist_file(self):
        self.assertEqual(self.guard("--pin", str(self.package)).returncode, 0)
        manifest = json.loads((self.tools / "local-install.json").read_text())
        self.assertEqual(manifest["version"], "0.2.2+zetta-delta.0123456789ab")
        self.assertEqual(sorted(manifest["files"]), ["dist/engine/config.js", "dist/index.js"])
        self.assert_verdict(0)

    def test_changed_or_missing_file_is_rejected(self):
        self.guard("--pin", str(self.package))
        config = self.package / "dist/engine/config.js"
        config.write_text("export const metal = true;\n")
        self.assert_verdict(1)
        config.unlink()
        self.assert_verdict(1)

    def test_upstream_reinstall_is_rejected(self):
        self.guard("--pin", str(self.package))
        self.write_version("0.2.2")
        self.assert_verdict(1)

    def test_pin_refuses_another_package(self):
        (self.package / "package.json").write_text(json.dumps({"name": "other", "version": "1.0.0"}))
        result = self.guard("--pin", str(self.package))
        self.assertEqual(result.returncode, 1)
        self.assertFalse((self.tools / "local-install.json").exists())


if __name__ == "__main__":
    unittest.main()
