**Rule:** All `ffprobe` calls MUST go through `lib/ffprobe`.

**Why:** cascade standardizes on `lib.ffprobe.probe()`, `get_duration()`, `get_dimensions()` so output shape is consistent and tests can mock one place.

**Wrong:** `subprocess.run(["ffprobe", "-print_format", "json", path], ...)`

**Right:** `from lib.ffprobe import probe; data = probe(path)`
