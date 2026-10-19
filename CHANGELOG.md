# Changelog

## 0.2.0 — 2026-10-20 (source update)

- Use one bounded provider request policy and explicit OpenAI-compatible endpoints/model chains; keep vendor presets as optional setup shortcuts. New instances default to text chat; vision requires explicit capability setup.
- Add installable `regular` CLI: offline demo, private configuration initialization,
  local diagnostics, explicit metadata checks, foreground run and structured status.
- Generate portable macOS/Linux service definitions. No automatic process killing,
  power-policy changes or service startup. Add a single-container Compose path.
- Default to TLS verification and disabled owner claiming; explicit blank owner
  phrase and fallback provider now retain their documented meaning.
- New deployments keep runtime data in `data/`; existing root layouts remain
  compatible without automatic moves or deletion.
- Commit actual delivered text after a positive QQ receipt; reuse the same sequence
  on retries and supersede unconfirmed older turns after a new turn or reset.
- Persist source-linked image descriptions and share in-flight/cache work across
  foreground and background paths. All physical model attempts use one daily budget.
- Advance digest archive offsets only for the processed prefix, preserving late and
  equal-timestamp arrivals; reject incomplete or invalid compression results.
- Make idle relation decay idempotent, restore persona/milestone fields, and keep
  stable promise identities and nag counts across renames and old-data upgrades.
- Record real background-task and gateway transitions; drain work before shutdown.
- Align configuration examples and bilingual onboarding; add offline CI, packaging,
  contributor guidance and issue forms. Runtime correctness fixes are described in
  the associated change review and regression tests.

This version is recorded in source; no Git tag, package upload or GitHub Release is
created by these changes. Follow the [0.2 upgrade checklist](docs/UPGRADE_0.2.md)
and [provider migration guide](docs/providers.md) before upgrading.
