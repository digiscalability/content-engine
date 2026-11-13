# ADR 0007: Config validation rules beyond FR-SRC-1

- Status: accepted, 2026-11-13 (slice P0.2)
- Requirements: SRD §12, FR-SRC-1, FR-SRC-2, FR-ING-2, FR-PUB-4, GR-5, §8.2, §17

## Context

FR-SRC-1 requires every source to have a `rights_type` in the seven tiers and a non-empty `rights_proof`. The SRD shows the identity and engine files by example but does not say which other mistakes the loader should refuse.

## Decision

The loader (`engine/config/`) refuses, with the file and field named:

- **Any unknown key**, everywhere, so a typo such as `rights_prof` cannot drop a required field without notice.
- **Sources:** `folder` without `path`; `drive` without `volume_serial` and `volume_label` (§8.2: drive letters change); `channel` without `url`, or with a tier other than `own` or `partner_written` (FR-ING-2); `film_reference` on anything but a `drive` (§8.2); `stock`, `cc_by` or `partner_written` without a `licence_url` holding the licence id or URL (FR-SRC-2); duplicate ids. Numeric-looking serials are read as text.
- **Identities:** `id` different from the file name; `autonomy_level` other than 0 or 1 (§17); format shares not adding up to 1.0; `min_gap_hours` below 3 (FR-PUB-4); `film_breakdown_share_max` above 0.2 (§8.2); no channels or two on one platform; a format C share with any channel whose `ai_disclosure` is not `required` (GR-5: format C is always an AI presenter); a `playbook` or `active_look` file that does not exist. `active_look` stays `null` until P0.8 approves a look.
- **Engine:** the seven §5.3 agents present, effort in `low`…`max`; `stop_free_gb` below `warn_free_gb`; schedule times as `HH:MM` (weekly `Ddd HH:MM`); QA thresholds in [0, 1]; loudness negative.
- **YAML errors** name the file and line; escape errors add a hint to single-quote Windows paths.

All problems across all files are reported together, and `ce doctor` fails while any remain.

## Consequences

- When Abbas adds a source (G0.9c), `uv run ce doctor` tells him at once whether it loads.
- A later slice that needs a new field adds it to the model; until then the key is an error, by design.
