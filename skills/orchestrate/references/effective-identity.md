# Effective dispatch identity

Use this procedure before any user-requested unprofiled model override. For a
pinned spawn without an override, its truthful generated roster row is the
effective identity. Never substitute a base profile's family, provider, lane, or
billing after overriding its model. Profile choice still controls role eligibility,
tools, and isolated/shared worktree behavior. For an unprofiled scout model, use
the built-in Scout as the base; dedicated custom scout prompts retain their fixed
identities. Candidate overrides require a candidate-capable isolated base profile.

1. Call Delta's actual `list_subagent_models` tool once for the user's requested
   model. It lists `provider_id`, `provider_name`, `model_id`, `model_name`, and
   supported effort values. Resolve exact IDs; a bare model available from several
   providers is ambiguous. Ask which provider, preserving the explicit requested
   model. Do not make a model/provider request to discover identity.
2. Save only those returned identity fields as a JSON list in a temporary file
   outside every checkout, for example `[{"provider_id":"...","model_id":"...",
   "model_name":"..."}]`. Do not dump Delta settings, headers, tokens, credentials,
   or an entire provider configuration. The generated `identity-registry.json`
   binds exact provider IDs to lane/billing/limit and exact provider/model pairs
   to families. Identical aliases such as `default` on different providers may
   serve different families; never reuse another provider's model-family binding.
3. Run the resolver, replacing and POSIX-quoting every argument:

   ```sh
   python3 {{IDENTITY_SH}} --registry {{IDENTITY_REGISTRY}} --models <sanitized-models-json> --provider <exact-provider-id> --model <exact-model-id>; echo "exit=$?"
   ```

   Require exit 0 and its JSON result. No JSON result means no dispatch. If the
   family is unknown, obtain the served family from verifiable provider model
   metadata or an explicit user identification; record that source. Add
   `--family <family> --family-evidence <source-or-user-statement>` and run again.
   A provider name is not a model family: the Qwen provider can serve DeepSeek.
   A supplied family cannot contradict a known registry binding. If provider
   lane/billing/budget is unknown, have its truthful metadata configured through
   the installer and rerun; preserve the requested model rather than silently
   replacing it. Never inherit an unrelated profile's lane or assume flat billing.
4. Record the JSON result beside the chosen profile in the dispatch ledger. Fill
   the block's IDENTITY and EFFECTIVE fields from it; charge every original, fix,
   retry, and follow-up spawn to its effective lane and billing. Apply both its
   lane limit and the roster's shared/thread limits; queue excess work. Metered
   identity still requires the user's request and consumes the metered allowance.
   Select reviewers and distinct-family participants using effective families.
5. Pass the exact `selector` (`provider_id/model_id`) as the model override only
   for the user's explicitly requested unprofiled model. A profile-pinned model
   needs no override. Append `[effort=<level>]` only when the user asked for that
   effort and live metadata lists it as supported. Confirm Delta's spawn label
   matches the effective record before relying on it. If it differs, hold new
   dispatches, account for any edits, and resolve the routing discrepancy.

Carry every retained author's effective record into author provenance and reviewer
selection; retries and fixes add their own records without erasing retained partial-work
authorship. Reviewers must differ from all retained author families. Carry the records into retries,
winning-candidate application, and follow-ups. Resolve again if the model or
provider changes. The same served model on another lane retains its family only when that
provider/model binding is separately verified; each lane shares its own budget. Delete only the temporary metadata
file you created when dispatch setup is complete; leave generated registries and
user settings untouched.
