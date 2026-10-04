#!/usr/bin/env python3
"""Resolve effective dispatch identity from sanitized, local Delta metadata.

No provider requests, configuration writes, model-name heuristics, or credentials.
"""
from __future__ import annotations

import argparse
import json
import sys


def text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or any(c in value for c in '\r\n\0'):
        raise ValueError(f'{label} must be a nonempty single-line string')
    return value


def positive(value: object, label: str) -> int:
    if type(value) is not int or value < 1:
        raise ValueError(f'{label} must be a positive integer')
    return value


def resolve(models: object, registry: object, model: str, provider: str | None = None,
            family: str | None = None, evidence: str | None = None) -> dict[str, str | int]:
    """Bind an available model to its truthful dispatch metadata.

    Args:
        models: Sanitized records returned by Delta's model-listing tool.
        registry: Generated provider budgets and exact provider/model-family bindings.
        model: Exact user-requested model ID.
        provider: Exact provider ID, required when model selection is ambiguous.
        family: Explicit verified/user-supplied family for an unknown model.
        evidence: Source of that explicit family identification.

    Returns:
        Effective identity, qualified selector, billing class, and shared budgets.

    Raises:
        ValueError: Metadata is invalid, ambiguous, unavailable, or contradictory.
    """
    model = text(model, 'requested model')
    if provider is not None:
        provider = text(provider, 'requested provider')
    if not isinstance(models, list) or not isinstance(registry, dict):
        raise ValueError('models must be a list and registry must be an object')
    if registry.get('version') != 1:
        raise ValueError('identity registry version must be 1')
    providers = {}
    for row in registry.get('providers', []):
        if not isinstance(row, dict):
            raise ValueError('provider registry entry must be an object')
        key = text(row.get('provider_id'), 'provider_id')
        if key in providers:
            raise ValueError('duplicate provider in registry')
        lane = text(row.get('lane'), 'lane')
        billing = row.get('billing')
        if billing not in ('flat', 'metered'):
            raise ValueError('provider billing must be flat or metered')
        providers[key] = {'lane': lane, 'billing': billing,
                          'limit': positive(row.get('limit'), 'provider limit')}
    families = {}
    for row in registry.get('models', []):
        if not isinstance(row, dict):
            raise ValueError('model registry entry must be an object')
        key = (text(row.get('provider_id'), 'family provider_id'),
               text(row.get('model_id'), 'model_id'))
        binding = text(row.get('family'), 'family')
        if key in families and families[key] != binding:
            raise ValueError('conflicting family bindings for provider/model')
        families[key] = binding
    thread_limit = positive(registry.get('thread_limit'), 'thread_limit')
    allowance = positive(registry.get('metered_max_spawns'), 'metered_max_spawns')
    seen = set()
    matches = []
    for row in models:
        if not isinstance(row, dict):
            raise ValueError('Delta model metadata entry must be an object')
        key = (text(row.get('provider_id'), 'provider_id'), text(row.get('model_id'), 'model_id'))
        if key in seen:
            raise ValueError('duplicate provider/model in Delta metadata')
        seen.add(key)
        if key[1] == model and (provider is None or key[0] == provider):
            matches.append(row)
    if not matches:
        raise ValueError('requested provider/model is unavailable in Delta model metadata')
    if len(matches) != 1:
        raise ValueError('ambiguous model: qualify its exact provider_id')
    selected = matches[0]
    provider_id = selected['provider_id']
    if provider_id not in providers:
        raise ValueError('provider has no configured lane, billing, and budget; update the registry before dispatch')
    known = families.get((provider_id, model))
    if family is not None:
        family = text(family, 'family')
        if known is not None and family != known:
            raise ValueError('supplied family contradicts the verified registry binding')
    if known is not None:
        resolved_family, family_source = known, 'identity-registry.json'
    else:
        if family is None:
            raise ValueError('unknown model family: supply explicit verified/user family and its evidence')
        resolved_family = family
        family_source = text(evidence, 'family evidence')
    if evidence is not None and family is None:
        raise ValueError('family evidence requires an explicit family')
    result = {
        'provider_id': provider_id, 'model_id': model,
        'model_name': text(selected.get('model_name', model), 'model_name'),
        'family': resolved_family, 'family_source': family_source,
        'selector': provider_id + '/' + model,
        'thread_limit': thread_limit, 'metered_max_spawns': allowance,
    }
    result.update(providers[provider_id])
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--models', required=True, help='sanitized list_subagent_models records as JSON')
    parser.add_argument('--registry', required=True, help='generated identity-registry.json')
    parser.add_argument('--model', required=True)
    parser.add_argument('--provider')
    parser.add_argument('--family', help='verified/user-supplied family for an unknown model')
    parser.add_argument('--family-evidence', help='source or explicit user statement; never credentials')
    args = parser.parse_args()
    try:
        with open(args.models, encoding='utf-8') as source:
            models = json.load(source)
        with open(args.registry, encoding='utf-8') as source:
            registry = json.load(source)
        result = resolve(models, registry, text(args.model, 'requested model'), args.provider,
                         args.family, args.family_evidence)
    except (OSError, ValueError, TypeError) as error:
        # Do not echo the input records, setting contents, or exception snippets.
        message = str(error) if isinstance(error, ValueError) and not isinstance(error, json.JSONDecodeError) else 'invalid or unreadable identity metadata'
        print('IDENTITY: blocked: ' + message, file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())
