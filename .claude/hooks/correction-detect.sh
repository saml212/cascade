#!/usr/bin/env bash
# UserPromptSubmit hook — when the developer's prompt contains correction
# language, nudge the assistant to emit a [LEARN] block in its response.
#
# Targeted pattern list (not the coarse pro-workflow regex — too noisy).
# Stdout is shown to the assistant as additional context for this turn.
#
# Spec: harness-v2 §5.

set -u

INPUT="$(cat)"
command -v jq >/dev/null 2>&1 || exit 0

PROMPT="$(echo "$INPUT" | jq -r '.prompt // .user_prompt // empty' 2>/dev/null)"
[ -z "$PROMPT" ] && exit 0

# Lower-case for matching (bash 4+ or macOS zsh-compatible via tr)
PROMPT_LOWER="$(echo "$PROMPT" | tr '[:upper:]' '[:lower:]')"

# High-confidence correction patterns (case-insensitive). Kept narrow on
# purpose — false positives are worse than false negatives here (they train
# the assistant to over-emit [LEARN] blocks for non-corrections).
#
# Rejected patterns (too noisy):
#   "i already"       → matches "I already have X", "I already did that"
#   "no,? ..."        → matches "no, that's fine" and lots of conversational "no"
#   "i (said|told)"   → matches benign restatement
if echo "$PROMPT_LOWER" | grep -qE "that'?s (wrong|not right|incorrect|not what (i|you))|you (forgot|should have|shouldn'?t have|missed the|need to fix)|wrong (file|function|variable|approach|repo|directory|way|module|answer)|(undo|revert|roll ?back) (that|this|the last)|dude,?\s+(stop|no|fuck|what)|that'?s not (right|what|how|correct)|this is (wrong|broken|not what|not it)"; then
  cat <<'NUDGE'

ℹ️  correction-detect: the user's message looks like a correction. When you
respond, end with a [LEARN] block capturing the mistake and the right approach,
so this doesn't happen again:

    [LEARN] Category: short-rule-slug
    Mistake: What you got wrong, concretely.
    Correction: What the right approach is.

The Stop hook will save this to the developer's corrections file automatically.
NUDGE
fi

exit 0
