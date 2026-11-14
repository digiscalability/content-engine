# ADR 0019: Budget caps, preflights and the manual kill switch

- Status: accepted, 2026-11-13 (slice P1.4)
- Requirements: SRD NFR-4, NFR-5, NFR-1, §3.5, D10, C16, GR-7, §17, §2.3 (`ce kill`, `ce warn`, `ce resume`), §13

## Context

NFR-4 asks for a per-run Claude budget, monthly caps per vendor in `budget_ledger`, a preflight before every voice, avatar or render batch, and a hard stop at each cap, reported in the digest and by notification. GR-7's manual half asks for `ce warn` and `ce kill` to set the kill flag. §17 says a kill drops the identity to L0 until `ce resume`. The SRD leaves open:

- how the Claude run budget meets the monthly cap;
- what HeyGen's cap counts;
- what a render preflight checks;
- how a kill survives a config sync;
- whether the engine touches Postiz on a kill.

## Decision

1. **One budget module, `engine/ops/budget.py`.** H3, `budget_status`, the digest and the SDK options all read the same functions.
2. **Claude:**
   - A run's `max_budget_usd` is the smaller of its command budget (daily 3, weekly 5) and what is left of the monthly cap (60).
   - A spent month raises `BudgetStop` in `build_options`, before any run starts.
   - `record_claude_run` books a finished run's reported cost on its `agent_runs` row and once in the ledger (`claude:run:<run_id>`). The live `ce daily` (P0.9) calls it.
3. **ElevenLabs is unchanged** (D10 engine cap, C16 account floor, both in H3 and the provider's preflight).
4. **HeyGen counts estimated credits.**
   - The estimate is the voiced audio's minutes × `credits_per_minute`.
   - It is charged once per script (`avatar:script:<id>`) when either route accepts the request. The drop-folder route spends Abbas's plan minutes just as the API spends credits.
   - H3 refuses a request that would pass `monthly_credit_cap`.
   - Both numbers (30 credits, 1 credit a minute) are placeholders until G0.5 shows the plan.
5. **Renders preflight the disk.** H3 refuses `render_post` while free space on the media drive is below `disk.stop_free_gb` (NFR-5), beside the existing daily render cap (GR-6).
6. **A hard stop is an alert.** A vendor at its cap, or an identity paused, opens the digest with an Alerts section, counts in the summary, and raises the toast on its own.
7. **The kill switch lives in the database.**
   - `ce kill <identity>|--all [--reason]` sets `kill_flag`, `kill_reason` and `autonomy_level = 0`.
   - `ce warn <identity> "<what arrived>"` records the warning, then kills with the reason `platform warning: …`.
   - `ce resume <identity>` clears the flag and restores the YAML's level. It refuses an identity that is not paused.
   - `sync_identities` never lifts a killed identity above L0.
   - Every step is a row in `identity_events` (migration 0003). L1's "zero platform warnings in 30 days" will count its `warn` rows.
   - The agent tools already refuse a killed identity (`require_alive`).
8. **The engine does not touch Postiz on a kill.** The commands list the identity's drafted, privately uploaded and scheduled posts, with Postiz ids, so Abbas can pull them. At L0 nothing publishes unless Abbas scheduled it, and unscheduling is his call.
9. **H3 reads avatar input as the tool does** (security review finding). It validates with the tool's own input model and voice-file helper and fails closed. A script id the tool would coerce (`2.0`) can no longer point the guard at another file and slip past the cap.

## Consequences

- P0.9 must call `record_claude_run` with `total_cost_usd`, `model_usage`, `num_turns` and `subtype` from the `ResultMessage`, and pass `command="weekly"` for the weekly retro.
- Phase 5's automatic kill on a reach drop reuses `kill()`. Unscheduling posts through Postiz on a kill can come with it, once L1 schedules posts the engine owns.
- After G0.5, set `budgets.heygen` from the plan's real allowance.
