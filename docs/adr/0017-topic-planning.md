# ADR 0017: Topic planning from the playbook

- Status: accepted, 2026-11-13 (slice P1.2)
- Requirements: SRD §6.5 (FR-PLN-1, FR-PLN-2, FR-PLN-3), §16, §5.4, FR-QA-6, FR-QA-8, GR-3

## Context

FR-PLN-1 picks topics from the playbook's topic bank, respecting cadence, banned topics and format shares. FR-PLN-2 makes about 20% of a week exploration. The SRD leaves open:

- where the bank lives and how the engine reads it;
- how "about 20%" is counted;
- how a used topic is recognised;
- which candidates come first;
- where banned topics are enforced before QA.

## Decision

1. **The bank lives in the playbook's Rules section**, as `### Topic bank` with one `#### <pillar>` group per pillar and a `#### Explore` group.
   - `engine/plan/playbook.py` parses the Rules section into pillars, banned topics and the bank, plus the Learned section. The pillar name is a bullet's text before any `:` or `(`.
   - A missing section, or a group that names no pillar, is an error naming it. `get_playbook` and `get_topic_candidates` then refuse, so a broken playbook never plans from nothing.
   - The engine never edits Rules. The starter bank is a proposal Abbas confirms or edits, like the other proposal sections.
2. **Exploration is counted per week**, Monday first, as `get_state` counts it. A post explores when `floor((n + 1) × share + 0.5)` is more than the explore posts so far, where `n` is this week's briefs.
   - At 0.2 and five posts, that is the third post. Ten posts give two, five apart.
   - The share counts briefs, not published posts: the brief is where the decision is made.
3. **A brief uses a bank topic when its topic text matches**, ignoring case, spacing and trailing punctuation.
   - No migration: the briefs table already holds the topic, and the prompts ask the agents to copy a candidate's text as written. An off-bank topic counts toward no pillar.
   - Topics stay free text. FR-PLN-1's "from the bank" is guidance through the candidates, not a hard check, so exploration can still try a new topic.
4. **Candidate ranking**:
   - Skip any topic among the identity's last 20 briefs (FR-PLN-3's window).
   - Then never-used topics before reused ones.
   - Within each, the pillar used least recently comes first, and one topic per pillar leads before any pillar repeats.
   - When an exploration post finds no Explore topic left, the tool offers pillar topics and says to explore through format or hook pattern (FR-PLN-2's other two kinds).
   - The format is the one furthest below its share of the last 20 briefs.
5. **Banned topics are enforced twice before money is spent**, with the same text checks the QA gate runs on a script (FR-QA-6 banned terms, FR-QA-8 first person, never list and self-reference claims):
   - `get_topic_candidates` skips any bank line that does not pass outright and names it with the reason.
   - `submit_brief` refuses a topic, angle, hook or CTA that fails. A hold (a claim about Abbas with no Fact Sheet to check it against) is left for the QA gate, as for a script.
   - A unit test holds every line of the selfhelp bank to these checks.

## Consequences

- Phase 2's trend candidates (FR-PLN-1) join `get_topic_candidates` as a second source; the ranking above stays for the bank.
- Rewording a bank topic makes it look unused, so it can come back sooner than 20 briefs. Abbas edits the bank knowing that.
- The retro (Phase 3) writes only to Learned. Proposing new bank topics would be a digest item for Abbas, not an edit to Rules.
