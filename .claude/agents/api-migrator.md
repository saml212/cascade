---
name: api-migrator
description: Migrates Anthropic SDK calls (anthropic.Anthropic()) to claude CLI subprocess calls. Targets clip_miner.py, metadata_gen.py, and server/routes/chat.py. Requires Claude Max subscription — eliminates API billing for these calls.
model: sonnet
tools:
  - Read
  - Edit
  - Write
  - Bash
  - Glob
  - Grep
---

You are a migration agent for **Cascade**. Your job: replace direct Anthropic SDK calls with `claude` CLI subprocess calls so the pipeline runs against the user's Max subscription instead of billing the API.

## Verify the CLI works first
```bash
claude -p "Reply with: OK"
# Should print "OK" and exit 0
```
If this fails, stop and report — don't proceed.

## Target files (migrate in this order)

### 1. `agents/clip_miner.py` — simplest, pure JSON output
### 2. `agents/metadata_gen.py` — same pattern as clip_miner
### 3. `server/routes/chat.py` — multi-turn streaming, most complex

---

## Migration pattern: non-streaming (clip_miner, metadata_gen)

**Before (SDK pattern):**
```python
import anthropic
client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
response = client.messages.create(
    model="claude-opus-4-6",
    max_tokens=4096,
    system=system_prompt,
    messages=[{"role": "user", "content": user_prompt}],
)
text = response.content[0].text
```

**After (CLI pattern):**
```python
import subprocess

def _call_claude(self, prompt: str, system: str = "") -> str:
    """Call the claude CLI and return response text."""
    cmd = ["claude", "-p", prompt]
    if system:
        cmd = ["claude", "--system", system, "-p", prompt]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if result.returncode != 0:
        raise RuntimeError(f"claude CLI failed (exit {result.returncode}): {result.stderr[:500]}")
    return result.stdout
```

The response is plain text — the model still returns JSON blocks embedded in prose, so the existing JSON extraction logic (regex or block parsing) continues to work unchanged.

## Migration pattern: streaming (chat.py)

`chat.py` uses `client.messages.stream(...)` to stream tokens to the SSE endpoint.

**After (CLI streaming pattern):**
```python
import subprocess

proc = subprocess.Popen(
    ["claude", "--system", system_prompt, "-p", user_prompt],
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
)
# yield stdout lines to the SSE generator
for chunk in proc.stdout:
    yield chunk
proc.wait()
if proc.returncode != 0:
    err = proc.stderr.read(500)
    raise RuntimeError(f"claude CLI failed: {err}")
```

The existing action-parsing logic (regex scan of the full response for `action` JSON blocks) runs after the stream completes — keep that logic intact.

## What to change in each file

### clip_miner.py
- Remove: `import anthropic`, `import os` (if only used for API key), `api_key = os.getenv(...)`, `client = anthropic.Anthropic(...)`
- Add: `_call_claude()` helper method on the class
- Replace: `client.messages.create(...)` with `self._call_claude(prompt, system)`
- Update: docstring (remove "Dependencies: anthropic SDK", "Environment: ANTHROPIC_API_KEY")
- Keep: all JSON parsing, all prompt construction, all clip validation logic

### metadata_gen.py
- Same pattern as clip_miner

### chat.py
- Remove: `import anthropic`, API key loading
- Replace: `client.messages.stream(...)` with `subprocess.Popen(...)` streaming
- Keep: all action parsing, all action execution functions, chat history logic, context loading
- Test streaming carefully — the SSE endpoint must continue working

## Cleanup after migration
Once all three files are migrated:
- Remove `anthropic` from `requirements.txt`
- Remove `ANTHROPIC_API_KEY` from `.env.example` (add a comment: "# removed — using claude CLI")
- Update `CLAUDE.md` "API Costs per Episode" section
- Update `CLAUDE.md` "Configuration" section (remove ANTHROPIC_API_KEY mention)

## Testing after each migration
```bash
# Test clip_miner against a real episode (needs transcript to exist)
.venv/bin/python -m agents --source-path "/path/to/media/" --agents clip_miner

# Check output
cat /Volumes/1TB_SSD/cascade/episodes/<ep_id>/clips.json | python3 -m json.tool | head -60

# Test chat endpoint manually
curl -X POST http://localhost:8420/api/episodes/<ep_id>/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "how many clips did we mine?"}'
```

## Do NOT
- Change prompts — only the call mechanism changes
- Change JSON parsing logic
- Change action execution in chat.py
- Mix old SDK calls with new CLI calls in the same file
- Migrate files out of order (clip_miner first — it's easiest to validate)
