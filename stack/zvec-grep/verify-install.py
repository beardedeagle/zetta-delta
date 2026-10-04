#!/usr/bin/env python3
"""Detect replacement of the reviewed local zvec-grep package after an update.

The index maintainer runs this before every zg build and refuses zg when it
fails. stack/install.sh pins it with --pin after installing zvec-grep.

Usage:
    verify-install.py              check the zg on PATH against the pin
    verify-install.py PACKAGE      check the package folder PACKAGE
    verify-install.py --pin PACKAGE  pin PACKAGE as the reviewed build
"""
import hashlib
import json
from pathlib import Path
import shutil
import sys


def verify():
    manifest = json.loads(Path(__file__).with_name("local-install.json").read_text())
    executable = shutil.which("zg")
    if len(sys.argv) > 2 or (len(sys.argv) == 1 and not executable):
        raise ValueError("expected an installed zg or one candidate package path")
    root = (Path(sys.argv[1]) if len(sys.argv) == 2
            else Path(executable).resolve().parents[2]).resolve()
    package = json.loads((root / "package.json").read_text())
    if package.get("name") != "@zvec/zvec-grep" or package.get("version") != manifest["version"]:
        raise ValueError("installed package version differs from the reviewed local build")
    for relative, expected in manifest["files"].items():
        path = (root / relative).resolve()
        if not path.is_relative_to(root / "dist") or not path.is_file():
            raise ValueError("reviewed artifact is missing or outside the package")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("reviewed artifact checksum differs")
    print("zg local package verification passed")


def pin(root):
    """Record ROOT's version and every file under its dist/ as the reviewed build.

    Writes local-install.json next to this script, replacing it in one step.

    Args:
        root (Path): the installed zvec-grep package folder.

    Raises:
        ValueError: ROOT is not a zvec-grep package, or has no dist/ files.
        OSError: a file could not be read, or the manifest written.
    """
    root = root.resolve()
    package = json.loads((root / "package.json").read_text())
    if package.get("name") != "@zvec/zvec-grep" or not package.get("version"):
        raise ValueError("not a zvec-grep package")
    files = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted((root / "dist").rglob("*")) if p.is_file()}
    if not files:
        raise ValueError("the package has no dist/ files")
    manifest = Path(__file__).with_name("local-install.json")
    partial = manifest.with_name(manifest.name + ".partial")
    partial.write_text(json.dumps({"version": package["version"], "files": files}, indent=2) + "\n")
    partial.replace(manifest)
    print(f"zg local package pinned: {package['version']}, {len(files)} files")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--pin":
        try:
            pin(Path(sys.argv[2]))
        except (OSError, ValueError, KeyError, TypeError) as error:
            print(f"zg local package pin failed: {error}", file=sys.stderr)
            sys.exit(1)
        sys.exit(0)
    try:
        verify()
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        print("zg local package verification failed; reinstall the reviewed local package before indexing", file=sys.stderr)
        sys.exit(1)
