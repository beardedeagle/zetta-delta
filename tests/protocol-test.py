#!/usr/bin/env python3
"""Acceptance checks for executable commands and completion decisions in the skills.

These tests exercise the written protocol with temporary Git copies. They do not
run Delta agents or assert that a textual status controls Delta's runtime.
"""
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BON = ROOT / 'skills/orchestrate/scripts/bon.sh'
BEST = ROOT / 'skills/orchestrate/references/best-of-n.md'
ISOLATED = ROOT / 'skills/isolated/SKILL.md'
ORCHESTRATE = ROOT / 'skills/orchestrate/SKILL.md'


def decisions(path, title):
    text = path.read_text()
    match = re.search(r'^## ' + re.escape(title) + r'\n(.*?)(?=^## |\Z)', text, re.M | re.S)
    if not match:
        raise AssertionError(f'{path.name}: missing operational decision table {title}')
    rows = [tuple(v.strip() for v in line.strip().strip('|').split('|'))
            for line in match.group(1).splitlines() if line.startswith('|')]
    return rows[2:]


class ProtocolAcceptance(unittest.TestCase):
    def test_failed_worker_completion_accounts_for_landed_partial_work(self):
        rows = decisions(ORCHESTRATE, 'Completion decisions')
        lookup = {(r[0], r[1]): r[2:] for r in rows}
        self.assertEqual(lookup.get(('Completed', 'failed')),
                         ('inspect-and-account', 'hold', 'once-after-accounting'))
        self.assertEqual(lookup.get(('Completed', 'blocked')),
                         ('inspect-and-account', 'hold', 'after-scope-answer'))
        self.assertEqual(lookup.get(('Failed or Stopped', 'any')),
                         ('inspect-and-account', 'hold', 'once-after-accounting'))
        # A normal runtime completion with a failed task must never authorize a
        # dependent or assume a pristine parent. This is the decision encoded by
        # the table consumed above, not a mocked Delta terminal status.
        for task_status in ('failed', 'blocked'):
            inspect, dependency, _retry = lookup[('Completed', task_status)]
            self.assertEqual(inspect, 'inspect-and-account')
            self.assertEqual(dependency, 'hold')

    def test_comparison_failure_cannot_normally_finish_unrestored(self):
        for path in (BEST, ISOLATED):
            with self.subTest(mode=str(path.relative_to(ROOT))):
                rows = decisions(path, 'Comparison completion decisions')
                lookup = {(r[0], r[1]): r[2:] for r in rows}
                self.assertEqual(lookup.get(('failed', 'failed')),
                                 ('notify-parent-and-hold', 'stop-check-and-hold'))
                self.assertEqual(lookup.get(('failed', 'ok')),
                                 ('finish-failed', 'check-and-exclude'))
                self.assertEqual(lookup.get(('ok', 'ok')),
                                 ('finish-with-restored-tree', 'check-before-selection'))
                # Apply the actual documented decision to irrecoverable restore:
                # only the restored cases may produce a normal final report.
                action = lookup[('failed', 'failed')][0]
                self.assertFalse(action.startswith('finish'))
                text = path.read_text()
                self.assertIn('recipient: "parent"', text)
                self.assertIn('Stop Subagent', text)
                self.assertIn('RESTORED_TREE:', text)

    def test_parent_gate_checks_dirty_file_bytes_in_both_comparison_modes(self):
        for path in (BEST, ISOLATED):
            with self.subTest(mode=str(path.relative_to(ROOT))):
                command = 'sh {{BON_SH}} check <RUN> <TREE>'
                self.assertIn(command, path.read_text(),
                              'comparison must execute the helper content gate before selection')
                with tempfile.TemporaryDirectory(prefix='delta-protocol-') as tmp:
                    tmp = Path(tmp)
                    repo = tmp / 'parent'
                    repo.mkdir()
                    task_env = dict(os.environ, HOME=str(tmp / 'home'),
                                    XDG_CACHE_HOME=str(tmp / 'cache'),
                                    GIT_CONFIG_GLOBAL='/dev/null', GIT_CONFIG_NOSYSTEM='1')
                    def run(args):
                        return subprocess.run(args, cwd=repo, env=task_env,
                                              text=True, capture_output=True, check=False)
                    for args in (['git', 'init', '-q'], ['git', 'config', 'user.email', 'fixture@example.invalid'],
                                 ['git', 'config', 'user.name', 'Fixture']):
                        self.assertEqual(run(args).returncode, 0)
                    f = repo / 'work.txt'
                    f.write_text('committed\n')
                    self.assertEqual(run(['git', 'add', '--', 'work.txt']).returncode, 0)
                    self.assertEqual(run(['git', 'commit', '-qm', 'fixture']).returncode, 0)
                    f.write_text('user dirty baseline\n')
                    before = run(['git', 'status', '--porcelain']).stdout
                    snapshot = run(['sh', str(BON), 'snapshot'])
                    self.assertEqual(snapshot.returncode, 0, snapshot.stdout + snapshot.stderr)
                    run_id = re.search(r'^RUN=(.+)$', snapshot.stdout, re.M).group(1)
                    tree = re.search(r'^TREE=(.+)$', snapshot.stdout, re.M).group(1)
                    # Fill the command that the actual mode instructs its parent
                    # to execute, including the installer's quoted helper site.
                    filled = command.replace('{{BON_SH}}', "'" + str(BON).replace("'", "'\\''") + "'")
                    filled = filled.replace('<RUN>', run_id).replace('<TREE>', tree)
                    baseline = run(['sh', '-c', filled])
                    self.assertEqual(baseline.returncode, 0, baseline.stdout + baseline.stderr)
                    f.write_text('unselected attempt merged over dirty user bytes\n')
                    after = run(['git', 'status', '--porcelain']).stdout
                    self.assertEqual(before, after, 'fixture must defeat a status-only comparison')
                    changed = run(['sh', '-c', filled])
                    self.assertNotEqual(changed.returncode, 0)
                    self.assertIn('RESULT: failed:', changed.stdout)
                    # Selection is authorized only by a successful RESULT from
                    # this filled content gate, never by status-list equality.
                    self.assertNotIn('RESULT: ok:', changed.stdout)
                    self.assertEqual(f.read_text(), 'unselected attempt merged over dirty user bytes\n')


if __name__ == '__main__':
    unittest.main()
