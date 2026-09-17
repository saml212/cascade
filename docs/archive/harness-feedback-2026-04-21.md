# Harness Feedback — Dev Dogfood Session (2026-04-21)

Observations from the first real dev-agent use of the harness. Severity-ordered. Each item: location, severity, what I hit, suggested fix.

---

## Blockers (real bugs caught / real friction)

### blocker: Handoff prompt assumes a dev-level end-user; actual user is Sam (non-technical)
**File:** the handoff prompt embedded in the user's first message
**Severity:** blocker
**What I hit:** The entire handoff prompt is written for a hypothetical "dev agent" session — it uses terms like "TDD", "commit-or-revert", "xfail", "subprocess", "stage". Sam is a podcast creator who explicitly said: "I have no idea what this means... I'm relying on you to help me... I want to just worry about filming episodes." I spent the first several turns surfacing implementation-level decisions to him (e.g. "should I revert these TDD tests or commit them as xfail?") that he had no way to evaluate. He pushed back and I course-corrected, but the harness design assumes an incorrect user model.
**Suggested fix:**
- Rewrite CLAUDE.md's "Key Rules" and any handoff templates to centre the non-technical owner. The main agent should default to deciding and narrating in outcome terms ("this will stop X posts from failing silently"), not asking about implementation.
- Saved two memories (`user_role.md`, `feedback_decision_making.md`) — future sessions will have them preloaded, but other agents or fresh sessions would not.
- Consider whether the `Explore`, `Plan`, and subagent prompts also assume dev-level judgment calls from the main agent that the main agent shouldn't push further up to the user.

### friction: Sentinel-write and `git commit` can't share a single `bash && git commit` compound
**File:** `.claude/hooks/pre-commit-gate.sh` + `.claude/skills/clean/SKILL.md`
**Severity:** friction
**What I hit:** I ran `bash .claude/scripts/clean-write-sentinel.sh && git commit -m "..."` as a single Bash tool call. The PreToolUse hook inspects the full command string at submit time, sees `git commit` via the subcommand splitter, computes the current staged hash, and blocks — because the sentinel script hasn't run yet (it's the second half of the compound). Had to split into two separate Bash calls. Cost: one failed commit, one round-trip.
**Suggested fix:**
- Option A: update the `/clean` skill docs to instruct the main agent: "call sentinel-write as a separate tool call before the commit — not chained with `&&`." Right now the skill says to write the sentinel at step 4 but doesn't clarify it must be a separate Bash invocation from the commit.
- Option B: have `/clean` actually execute sentinel-write itself (it currently only tells the main agent to do so). Since the skill already orchestrates step 1 (ruff + vulture) via `clean-step1-static.sh`, step 4 could just run `clean-write-sentinel.sh` at the end of the pipeline instead of delegating to the main agent.
- Option A is the cheaper fix and preserves the current skill design.

---

## Friction

### friction: A specialist auto-formatted files outside its intended scope
**File:** `tests/test_lib_ass.py`, `tests/test_lib_ass_render.py`, `lib/ass.py`, `tests/test_agent_podcast_feed.py`
**Severity:** friction
**What I hit:** After the python-specialist completed its work on the publishing fixes, four files I had already committed earlier in the session showed up as modified in `git status`. They were all in directories the specialist was allowed to edit (`tests/`, `lib/`), but weren't in its target list. Pure ruff-format changes + one unused-import removal — no logic changes, tests still pass. Had to land a cleanup commit.
**Suggested fix:**
- When dispatching a specialist, explicitly scope its `ruff format` / `ruff check --fix` invocation to the target files, not the whole directory. E.g. in the prompt: "run `.venv/bin/ruff format agents/publish.py agents/podcast_feed.py server/routes/pipeline.py`" — not `tests/` or `.`.
- Alternatively, have the python-specialist system prompt forbid running any formatter on files outside its declared scope.

### friction: Handoff's "commit what's good, revert what's stale" framing missed the in-progress-TDD case
**File:** handoff prompt, priority #1 section
**Severity:** friction
**What I hit:** Two untracked test files were neither complete-and-working (they failed) nor abandoned (they match item 5 of the active `PLAN_publishing_2026-04-12.md` continuation). Framing as binary commit-or-revert forced me to surface the scope decision back to the user. After course-correction (make the call myself), I implemented items 1-4 of the plan and landed the tests green. The decision was reasonable; it just shouldn't have been surfaced.
**Suggested fix:** Handoff templates should include a third path: "if untracked test files reference functions/signatures that don't exist yet and there's a matching plan doc, implement to make them green — don't bounce the scope question to the user unless the work is clearly out of session budget."

### friction: Handoff described "~15 modified files" — actual was 1 modified + 4 untracked tests + 1 new lib + 2 plan docs
**File:** handoff prompt
**Severity:** friction (reported polish in initial pass; upgrading)
**What I hit:** Spent extra cycles cross-checking the claimed bad state against reality. The actual repo was mostly clean — most of the "stalled" files from the handoff prompt had been tidied before this session started.
**Suggested fix:** Auto-generate the "current repo state" portion of handoff prompts from a real `git status` snapshot at handoff-write time, and label the snapshot's timestamp so it's clear when it was taken.

### friction: Vulture false-positive on mock `**kwargs`
**File:** `tests/test_agent_publish.py` (4 sites: lines 252, 285, 326, 457)
**Severity:** friction
**What I hit:** `/clean` step 1 blocked because vulture flagged `**kwargs` in four `subprocess.run` mock functions as "unused variable". This is a standard Python idiom: mocks must accept the same kwargs signature as the mocked callable even if they don't use them. Had to rename to `_kwargs` across all four sites to silence vulture.
**Suggested fix:**
- Configure vulture in `.claude/profile.json` to ignore `**kwargs` / `*args` parameters by default (or use `--ignore-names kwargs`), OR
- Document the `_kwargs` convention in `CLAUDE.md` / the /clean skill so future sessions don't hit this first.

### friction: Route "double-prefix" bug was silently broken in production for weeks
**File:** `server/routes/pipeline.py:291` (pre-fix) — `/episodes/{episode_id}/approve-longform` under a router already prefixed `/api/episodes`
**Severity:** friction (not a harness bug per se — a cascade bug — but lacks preventive tooling)
**What I hit:** The plan doc mentioned this; I verified + fixed. But no route test or route-table lint would have flagged that the URL `/api/episodes/episodes/{id}/approve-longform` is definitely nobody's intended URL.
**Suggested fix:** `tests/test_routes_pipeline.py` already exists. Could add a sanity test that every declared route in `pipeline.router.routes` starts with `/api/episodes/` and does NOT contain `/api/episodes/episodes/` (doubled segment). Near-zero cost, would catch regressions.

---

## Wins (worth keeping)

### win: verifier caught a real bug that the new unit tests missed
**File:** `agents/podcast_feed.py:119` (use-before-assignment of `feed_url`)
**Severity:** N/A (positive observation)
**What happened:** The python-specialist landed the three files and said `477/0 pass`. When I dispatched the `verifier` agent as an independent gate, it ran ruff and flagged `F821 Undefined name 'feed_url'` — the new code used `feed_url` on line 119 but only defined it on line 132, after the R2 upload. Unit tests on `_build_feed_xml` directly didn't exercise this path. This would have exploded the first time `podcast_feed.execute()` ran in prod.
**Implication:** Keep the verifier-after-specialist pattern. Static checks as a second gate catch things tests don't.

### win: python-specialist followed the test spec precisely
**File:** N/A
**Severity:** N/A (positive)
**What happened:** Given a ~1500-line spec (two test files + plan doc), specialist landed 3 files of changes, kept scope tight, made reasonable decisions on ambiguity, emitted a relevant [LEARN] block about ElementTree namespaces, and reported concisely in under 200 words as requested. No scope creep. No imports from `anthropic`. Matched the "don't touch frontend" boundary.

### win: frontend-specialist reported "couldn't browser-test" honestly
**File:** N/A
**Severity:** N/A (positive)
**What happened:** Instead of claiming success, the specialist explicitly flagged that no episode in `awaiting_publish_approval` state exists, so the button couldn't be visually verified. This is exactly the candor the harness encourages. Good signal.

---

## Follow-ups / suggestions for future sessions

- The `approve-longform` handler (line 365 after fix) runs `["clip_miner", "shorts_render", "metadata_gen", "thumbnail_gen", "qa"]` but NOT `podcast_feed`. The podcast feed likely needs to run somewhere in the sequence before `awaiting_publish_approval`, but currently only `approve-publish` triggers it. Check whether this is intentional; if not, `approve-longform` should probably end with `podcast_feed` too, so the feed is built-and-ready-for-review by the time publish is approved.
- `approve-publish` runs `["podcast_feed", "publish"]`. If `podcast_feed` is already supposed to have run earlier, this double-runs it. Worth clarifying in `docs/architecture.md`.

---

## Session tally

- 4 commits landed, all through `/clean`:
  1. `6e11b7f` — lib/ass.py (ASS subtitle generator) + 34 passing tests
  2. `c7fd811` — clip_miner.py ruff reformat
  3. `dc32f46` — publishing subsystem hardening (safety gate, error surfacing, RSS fixes, approve-publish route, YouTube funnel) + 59 passing tests
  4. `23d553a` — Approve & Publish frontend button
- Full test suite: 477 passing, 0 failing.
- No `[LEARN]` blocks emitted by the main agent this session — the specialist's learn about ElementTree namespaces is Python trivia, not a cascade-specific gotcha worth saving.
- Two memories saved about user profile + how to work with Sam.
