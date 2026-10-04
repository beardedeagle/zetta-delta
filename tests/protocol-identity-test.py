#!/usr/bin/env python3
"""Effective dispatch identity from sanitized Delta model metadata and registry."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / 'skills/orchestrate/scripts/identity.py'
MODELS = [
    {'provider_id': 'kimi', 'provider_name': 'Kimi Code', 'model_id': 'k3', 'model_name': 'Kimi K3'},
    {'provider_id': 'zai', 'provider_name': 'Z.AI', 'model_id': 'glm-unprofiled', 'model_name': 'GLM'},
    {'provider_id': 'qwen', 'provider_name': 'Qwen plan', 'model_id': 'qwen-max', 'model_name': 'Qwen'},
    {'provider_id': 'qwen', 'provider_name': 'Qwen plan', 'model_id': 'deepseek-pro', 'model_name': 'DeepSeek'},
]
REGISTRY = {
    'version': 1,
    'providers': [
        {'provider_id': 'kimi', 'lane': 'Kimi Code', 'billing': 'flat', 'limit': 3},
        {'provider_id': 'zai', 'lane': 'Z.AI', 'billing': 'flat', 'limit': 2},
        {'provider_id': 'qwen', 'lane': 'Qwen plan', 'billing': 'flat', 'limit': 2},
    ],
    'models': [{'provider_id': 'kimi', 'model_id': 'k3', 'family': 'Kimi'},
               {'provider_id': 'zai', 'model_id': 'glm-unprofiled', 'family': 'GLM'},
               {'provider_id': 'qwen', 'model_id': 'qwen-max', 'family': 'Qwen'},
               {'provider_id': 'qwen', 'model_id': 'deepseek-pro', 'family': 'DeepSeek'}],
    'thread_limit': 6, 'metered_max_spawns': 2,
}


class EffectiveIdentity(unittest.TestCase):
    def resolve(self, model, *, provider=None, family=None, evidence=None, models=None, registry=None):
        with tempfile.TemporaryDirectory(prefix='delta-identity-') as td:
            td = Path(td)
            (td / 'models.json').write_text(json.dumps(MODELS if models is None else models))
            (td / 'registry.json').write_text(json.dumps(REGISTRY if registry is None else registry))
            args = ['python3', str(HELPER), '--models', str(td / 'models.json'),
                    '--registry', str(td / 'registry.json'), '--model', model]
            for flag, value in (('--provider', provider), ('--family', family), ('--family-evidence', evidence)):
                if value is not None:
                    args += [flag, value]
            return subprocess.run(args, text=True, capture_output=True,
                                  env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'), check=False)

    def success(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_glm_override_uses_actual_family_lane_and_review_eligibility(self):
        identity = self.success(self.resolve('glm-unprofiled', provider='zai'))
        self.assertEqual((identity['model_id'], identity['provider_id'], identity['family'], identity['lane']),
                         ('glm-unprofiled', 'zai', 'GLM', 'Z.AI'))
        self.assertEqual(identity['limit'], 2)
        self.assertEqual(identity['thread_limit'], 6)
        self.assertEqual(identity['billing'], 'flat')
        # Selecting a Worker profile whose default family is Kimi must not make
        # its overridden GLM author eligible for another GLM review.
        eligible = [f for f in ['GLM', 'DeepSeek'] if f != identity['family']]
        self.assertEqual(eligible, ['DeepSeek'])
        self.assertNotIn('Kimi', json.dumps(identity))

    def test_qwen_lane_preserves_deepseek_family(self):
        qwen = self.success(self.resolve('qwen-max'))
        deepseek = self.success(self.resolve('deepseek-pro'))
        self.assertEqual((qwen['lane'], deepseek['lane']), ('Qwen plan', 'Qwen plan'))
        self.assertEqual((qwen['family'], deepseek['family']), ('Qwen', 'DeepSeek'))
        self.assertEqual((qwen['limit'], deepseek['limit']), (2, 2))

    def test_duplicate_provider_choice_requires_qualification(self):
        models = MODELS + [dict(MODELS[1], provider_id='qwen', provider_name='Qwen plan')]
        result = self.resolve('glm-unprofiled', models=models)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('ambiguous', result.stderr)
        resolved = self.success(self.resolve('glm-unprofiled', provider='zai', models=models))
        self.assertEqual(resolved['provider_id'], 'zai')
        duplicate = self.resolve('glm-unprofiled', provider='zai', models=MODELS + [MODELS[1]])
        self.assertNotEqual(duplicate.returncode, 0)
        self.assertIn('duplicate', duplicate.stderr)

    def test_same_model_alias_can_have_different_families_by_provider(self):
        models = [dict(MODELS[0], model_id='default'), dict(MODELS[1], model_id='default')]
        registry = json.loads(json.dumps(REGISTRY))
        registry['models'] += [{'provider_id': 'kimi', 'model_id': 'default', 'family': 'Kimi'},
                               {'provider_id': 'zai', 'model_id': 'default', 'family': 'GLM'}]
        kimi = self.success(self.resolve('default', provider='kimi', models=models, registry=registry))
        glm = self.success(self.resolve('default', provider='zai', models=models, registry=registry))
        self.assertEqual((kimi['family'], glm['family']), ('Kimi', 'GLM'))

    def test_another_provider_model_binding_is_not_inherited(self):
        models = MODELS + [dict(MODELS[1], provider_id='qwen')]
        unknown = self.resolve('glm-unprofiled', provider='qwen', models=models)
        self.assertNotEqual(unknown.returncode, 0)
        self.assertIn('family', unknown.stderr)
        explicit = self.success(self.resolve('glm-unprofiled', provider='qwen', models=models,
                                            family='GLM', evidence='User identifies Qwen-hosted GLM'))
        self.assertEqual(explicit['family'], 'GLM')
        self.assertEqual(explicit['lane'], 'Qwen plan')

    def test_unknown_family_requires_explicit_evidenced_binding(self):
        models = MODELS + [dict(MODELS[1], model_id='new-model')]
        unknown = self.resolve('new-model', models=models)
        self.assertNotEqual(unknown.returncode, 0)
        self.assertIn('family', unknown.stderr)
        missing_evidence = self.resolve('new-model', models=models, family='GLM')
        self.assertNotEqual(missing_evidence.returncode, 0)
        self.assertIn('evidence', missing_evidence.stderr)
        setup = self.success(self.resolve('new-model', models=models, family='GLM',
                                        evidence='User explicitly identified served family in intake'))
        self.assertEqual(setup['family'], 'GLM')
        self.assertEqual(setup['family_source'], 'User explicitly identified served family in intake')
        conflict = self.resolve('glm-unprofiled', family='Kimi', evidence='bad binding')
        self.assertNotEqual(conflict.returncode, 0)
        self.assertIn('contradicts', conflict.stderr)

    def test_unknown_lane_rejects_without_inheriting_base_profile(self):
        models = MODELS + [dict(MODELS[1], provider_id='api')]
        unknown = self.resolve('glm-unprofiled', provider='api', models=models)
        self.assertNotEqual(unknown.returncode, 0)
        self.assertIn('provider', unknown.stderr)
        registry = json.loads(json.dumps(REGISTRY))
        registry['providers'].append({'provider_id': 'api', 'lane': 'GLM API', 'billing': 'metered', 'limit': 1})
        setup = self.success(self.resolve('glm-unprofiled', provider='api', models=models, registry=registry,
                                          family='GLM', evidence='User identifies GLM served on API'))
        self.assertEqual((setup['lane'], setup['billing'], setup['limit'], setup['metered_max_spawns']),
                         ('GLM API', 'metered', 1, 2))

    def test_invalid_or_conflicting_registry_is_rejected(self):
        for mutation in ('zero', 'duplicate-provider', 'family-conflict'):
            registry = json.loads(json.dumps(REGISTRY))
            if mutation == 'zero':
                registry['providers'][0]['limit'] = 0
            elif mutation == 'duplicate-provider':
                registry['providers'].append(registry['providers'][0])
            else:
                registry['models'].append({'provider_id': 'zai', 'model_id': 'glm-unprofiled', 'family': 'Kimi'})
            with self.subTest(mutation=mutation):
                self.assertNotEqual(self.resolve('glm-unprofiled', registry=registry).returncode, 0)

    def test_malformed_json_does_not_echo_input(self):
        with tempfile.TemporaryDirectory(prefix='delta-identity-json-') as td:
            path = Path(td) / 'malformed.json'
            path.write_text('{"API_SECRET_FIXTURE": invalid}')
            registry = Path(td) / 'registry.json'
            registry.write_text(json.dumps(REGISTRY))
            result = subprocess.run(['python3', str(HELPER), '--models', str(path),
                                     '--registry', str(registry), '--model', 'glm-unprofiled'],
                                    text=True, capture_output=True, check=False)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stdout, '')
            self.assertIn('invalid or unreadable', result.stderr)
            self.assertNotIn('API_SECRET_FIXTURE', result.stderr)

    def test_requested_provider_is_validated_without_echoing_value(self):
        result = self.resolve('glm-unprofiled', provider='zai\nAPI_SECRET_FIXTURE')
        self.assertEqual(result.returncode, 1)
        self.assertIn('requested provider', result.stderr)
        self.assertNotIn('API_SECRET_FIXTURE', result.stderr)

    def test_missing_actual_model_and_credentials_are_not_output(self):
        result = self.resolve('unavailable')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')
        models = [dict(m, api_key='DO-NOT-PRINT-FIXTURE') for m in MODELS]
        identity = self.success(self.resolve('glm-unprofiled', models=models))
        self.assertNotIn('DO-NOT-PRINT-FIXTURE', json.dumps(identity))
        self.assertNotIn('api_key', identity)


if __name__ == '__main__':
    unittest.main()
