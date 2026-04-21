---
name: autoresearch
description: Iteratively optimize any text artifact (prompt, skill description, doc, spec) via a generate→score→keep loop. Carries a lessons ledger so each iteration learns from prior failures. Use when the developer says "make this better", "optimize this prompt", "iterate on", "keep improving until X", or when a prompt/skill is underperforming. Cascade use cases: clip_miner prompt quality, metadata_gen per-platform prompts, skill descriptions, agent system prompts.
---

# /autoresearch — iterative artifact optimization

Inspired by Karpathy's autoresearch pattern for ML research. The idea:
failed experiments are nearly as valuable as the winner if you keep a
lessons ledger and consult it before each new iteration. This avoids
re-exploring dead ends and actively redirects toward unexplored territory.

## What it works on
Any text artifact that has a clear success criterion:
- Prompts (optimize system prompt against a rubric)
- Skill descriptions (does a reader know when to invoke it?)
- Spec documents (clarity + completeness + conciseness)
- Agent definitions
- Email copy, rubrics, creative briefs

## Inputs (from the developer or inferred)

1. **Target artifact** — file path or inline text
2. **Scoring rubric** — binary-ish criteria ("produces JSON in the correct shape", "under 80 lines", "no restating comments"). If the developer doesn't give one, propose a draft and confirm before running.
3. **Target score** (optional) — exit when hit; defaults to "all rubric items pass"
4. **Iteration cap** (optional; default 5) — exit after N iterations regardless

## Run state

```
.claude/.state/autoresearch/<run-id>/
├── target.md          # the artifact being optimized (starting version)
├── rubric.md          # the scoring criteria
├── lessons.md         # growing ledger of what didn't work and why
├── variants/
│   └── iter-N/        # candidates generated this iteration
│       ├── a.md
│       ├── b.md
│       └── c.md
├── scores/
│   └── iter-N.json    # per-variant scores + commentary
└── winner.md          # best artifact so far (updated each iteration)
```

`run-id` is `YYYYMMDD-HHMMSS-<slug-of-target>`. State dir is gitignored
(`.claude/.state/` covers it).

## The loop

Run until target score reached OR iteration cap exhausted:

### 1. Read lessons.md
Everything that's been tried and failed. Use this to constrain generation.

### 2. Generate K variants (default K=3)
Dispatch via the Agent tool with a prompt like:

> Generate K alternative versions of the target artifact. The current
> best is at winner.md. Lessons from prior iterations (do NOT repeat these
> mistakes): [contents of lessons.md]. Write each variant to
> `variants/iter-N/<letter>.md`. Each should be a materially different
> approach, not a small tweak.

### 3. Score each variant
For each variant, evaluate against the rubric. Pass/fail per criterion,
plus a short reason. Write to `scores/iter-N.json`:
```json
{
  "a": {"passes": 3, "fails": 1, "reasons": {...}},
  "b": {"passes": 4, "fails": 0, "reasons": {...}},
  "c": {"passes": 2, "fails": 2, "reasons": {...}}
}
```

### 4. Pick the winner
Highest "passes" count. Tiebreak: shorter artifact. Update `winner.md`.

### 5. Record losers' lessons
For each non-winning variant, append to `lessons.md`:
```
## iter-N variant <letter>
Failed: <rubric item>
Because: <specific reason>
Avoid in future: <constraint>
```

### 6. Check exit conditions
- If winner passes all rubric items → success; finalize and stop
- If iteration cap reached → stop with best-so-far
- Otherwise → next iteration

## When to invoke (main-agent guidance)

Invoke /autoresearch when:
- The developer explicitly asks ("optimize this", "iterate on", "make better")
- A prompt is producing inconsistent outputs and you want to nail down a better version
- A skill description is vague and the skill isn't being triggered reliably
- A spec document has gotten unwieldy and needs a rewrite

Do NOT invoke for:
- Single-pass rewrites (just edit the file)
- Things without a measurable success criterion (don't autoresearch "make this more fun")
- Production code (use /clean + specialist instead)

## Cascade use cases on the wishlist

- `/autoresearch agents/clip_miner.py clip-mining-prompt` — optimize the
  clip mining prompt against a rubric of "picks clips in 45-75s sweet spot,
  prefers complete thoughts, avoids filler"
- `/autoresearch agents/metadata_gen.py platform-metadata` — per-platform
  metadata quality
- `/autoresearch .claude/agents/python-specialist.md` — is the specialist
  prompt producing good work?

## Rubric quality matters

A bad rubric produces a bad optimum. Before iterating, sanity-check the rubric:
- Are criteria measurable? ("short enough" is bad; "under 80 lines" is good)
- Are they independent? (overlapping criteria bias winners)
- Is there a clear pass/fail boundary per criterion?

If the rubric is vague, propose tightenings before running the loop.

## Output

At end of run, report:
- Number of iterations run
- Final score
- Diff between starting artifact and winner
- The top 2-3 lessons from lessons.md (most instructive failures)
- Suggest: apply winner to the actual file (overwrite), or leave in state dir for manual review
