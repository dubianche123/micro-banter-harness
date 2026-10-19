# Changelog

## 0.2.0 — 2026-10-20 (source update)

- Make first launch actionable: stop rejected QQ authentication, bound failures
  before first login, retain recovery after login, and confirm readiness only after
  gateway authentication. Keep rejected token payloads out of error summaries.
- Validate required setup input immediately and treat optional aliases as optional.

- Use one bounded provider request policy and explicit OpenAI-compatible endpoints/model chains; keep vendor presets as optional setup shortcuts. Image reading inherits the main provider and first chat model by default, with optional independent provider/model overrides.
- Add installable `regular` CLI: offline demo, private configuration initialization,
  guided setup that fills missing settings and runs local checks, explicit metadata
  checks, foreground run and structured status. Keep configured values and hidden
  secret input; show actionable next steps for the selected instance directory.
- Generate portable macOS/Linux service definitions. No automatic process killing,
  power-policy changes or service startup. Add a single-container Compose path.
- Default to TLS verification. Silently save the first nonempty C2C sender as
  owner, with no claim phrase or extra notification; handle the message normally.
  Group messages cannot claim. Give new groups 10 minutes before appending one
  ownership reminder to a normal chat reply, marking it shown only after delivery.
  Keep cross-endpoint fallback explicitly opt-in.
- Keep runtime state, owner identity, logs, archives and summaries in `data/` by
  default, with an explicit deployment directory and data-path overrides.
- Commit actual delivered text after a positive QQ receipt; reuse the same sequence
  on retries and supersede unconfirmed older turns after a new turn or reset.
- Persist source-linked image descriptions and share in-flight/cache work across
  foreground and background paths. All physical model attempts use one daily budget.
- Advance digest archive offsets only for the processed prefix, preserving late and
  equal-timestamp arrivals; reject incomplete or invalid compression results.
- Make idle relation decay idempotent, restore persona/milestone fields, and keep
  stable promise identities and nag counts across renames.
- Record real background-task and gateway transitions; drain work before shutdown.
- Align configuration examples and bilingual onboarding; add offline CI, packaging,
  contributor guidance and issue forms. Runtime correctness fixes are described in
  the associated change review and regression tests.

This version is recorded in source; no Git tag, package upload or GitHub Release is
created by these changes. See the [provider configuration guide](docs/providers.md)
for endpoint and image-model setup.
