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
BATCH = ROOT / 'skills/pr-review-batch/SKILL.md'
BATCH_NOTES = ROOT / 'skills/pr-review-batch/references/notes.md'
REVIEW = ROOT / 'skills/pr-review/SKILL.md'
PUBLICATION = ROOT / 'skills/pr-review/references/review-publication.md'


def decisions(path, title):
    text = path.read_text()
    match = re.search(r'^## ' + re.escape(title) + r'\n(.*?)(?=^## |\Z)', text, re.MULTILINE | re.DOTALL)
    if not match:
        raise AssertionError(f'{path.name}: missing operational decision table {title}')
    rows = [tuple(v.strip() for v in line.strip().strip('|').split('|'))
            for line in match.group(1).splitlines() if line.startswith('|')]
    return rows[2:]


class ProtocolAcceptance(unittest.TestCase):
    def test_single_review_preserves_three_families_when_vetter_finds_spillover(self):
        """Check reciprocal spillover vetting with the same three participants."""
        rows = decisions(REVIEW, 'Finding routing')
        self.assertEqual(rows, [
            ('primary finder', 'primary vetter', 'orchestrator'),
            ('primary vetter (spillover)', 'primary finder', 'orchestrator'),
        ])
        families = {
            'primary finder': 'A',
            'primary vetter': 'B',
            'primary vetter (spillover)': 'B',
            'orchestrator': 'C',
        }
        for finder, vetter, adjudicator in rows:
            with self.subTest(finder=finder):
                self.assertEqual(len({families[finder], families[vetter],
                                      families[adjudicator]}), 3)

    def test_batch_adjudication_preserves_parallel_exception_and_family_reuse(self):
        rows = decisions(BATCH_NOTES, 'Batch adjudication decisions')
        lookup = {(overlap, stand_in): (adjudicator, action)
                  for overlap, stand_in, adjudicator, action in rows}
        self.assertEqual(lookup[('no', 'not needed')], ('orchestrator', 'adjudicate'))
        for activity in ('idle', 'active on another PR'):
            with self.subTest(activity=activity):
                self.assertEqual(lookup[('yes', 'eligible and capable; ' + activity)],
                                 ('stand-in', 'adjudicate-in-parallel'))
        for assessment in ('family already participated on this PR',
                           'lower or unknown capability', 'unavailable or unknown identity'):
            with self.subTest(assessment=assessment):
                self.assertEqual(lookup[('yes', assessment)], ('none', 'hold affected PR'))

    def test_shared_publication_has_three_outcomes_and_holds_unsafe_submission(self):
        rows = decisions(PUBLICATION, 'Publication decisions')
        submissions = [(status, evidence, event, body)
                       for status, evidence, event, body in rows if event != 'hold']
        self.assertEqual(submissions, [
            ('complete', 'confirmed actionable blocker/major/minor',
             'REQUEST_CHANGES', 'verdict and findings'),
            ('complete', 'no findings, no material gaps',
             'APPROVE', 'omit unless user supplied one'),
            ('complete', 'non-blocking nits only, no material gaps',
             'APPROVE', 'nits and explicit non-blocking statement'),
        ])
        lookup = {(status, evidence): (event, body)
                  for status, evidence, event, body in rows}
        for status, evidence in (
            ('partial/blocked/failed', 'any'),
            ('any', 'unresolved material evidence gap'),
            ('any', 'missing publication authorization'),
            ('any', 'closed PR or changed base/head'),
        ):
            with self.subTest(status=status, evidence=evidence):
                self.assertEqual(lookup[(status, evidence)], ('hold', 'none'))

    def test_both_review_methods_route_to_one_publication_contract(self):
        for path in (REVIEW, BATCH):
            with self.subTest(skill=path.parent.name):
                text = path.read_text()
                self.assertIn('references/review-publication.md', text)
                self.assertIn('references/review-voice.md', text)
                self.assertIn('references/review-intake.md', text)
                self.assertIn('references/review-workflow.md', text)
                self.assertNotIn('## Publication decisions', text,
                                 'publication decisions belong to the shared reference')

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
                    def run(args, cwd=repo, env=task_env):
                        return subprocess.run(args, cwd=cwd, env=env,
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
                    run_match = re.search(r'^RUN=(.+)$', snapshot.stdout, re.MULTILINE)
                    tree_match = re.search(r'^TREE=(.+)$', snapshot.stdout, re.MULTILINE)
                    assert run_match is not None and tree_match is not None
                    run_id = run_match.group(1)
                    tree = tree_match.group(1)
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
