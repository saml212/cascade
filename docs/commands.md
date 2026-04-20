# Commands

## Setup & Server
```bash
./start.sh                    # Creates .venv (Python 3.12 via uv), installs deps, starts uvicorn on :8420
```

## Pipeline (CLI)
```bash
.venv/bin/python -m agents --source-path "/path/to/media/"
.venv/bin/python -m agents --source-path "/path/to/media/" --episode-id ep_2026-02-13_test
.venv/bin/python -m agents --source-path "/path/to/media/" --agents ingest stitch audio_analysis
```

## Tests
```bash
.venv/bin/pytest              # All Python tests
.venv/bin/pytest -v           # Verbose
.venv/bin/pytest tests/test_agent_ingest.py  # Single test file
cd frontend && npm test       # Frontend Jest tests (jsdom)
```

## API
```bash
curl -X POST http://localhost:8420/api/episodes/ep_001/run-pipeline \
  -H "Content-Type: application/json" \
  -d '{"source_path": "/path/to/media/"}'
curl http://localhost:8420/api/episodes/ep_001/pipeline-status
curl -X POST http://localhost:8420/api/episodes/ep_001/auto-approve
```

## Uvicorn reload caveat
After modifying `.py` files, `--reload` sometimes loads stale bytecode. Restart the server or clear caches:
```bash
find . -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null; true
```
