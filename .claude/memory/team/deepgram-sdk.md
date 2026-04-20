**Rule:** Do not use `deepgram-sdk` v5. Use httpx against the REST endpoint directly.

**Why:** v5 API is completely incompatible with v3 — the SDK migration is messy and adds no value over a direct httpx call.

**Pattern:**
```python
import httpx
r = httpx.post(
    "https://api.deepgram.com/v1/listen?...",
    headers={"Authorization": f"Token {DEEPGRAM_API_KEY}"},
    content=wav_bytes,
    timeout=600,
)
```
