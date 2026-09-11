# Commands

## Setup & Server
```bash
./start.sh                    # Repairs/creates .venv, installs core deps, builds frontend, starts 127.0.0.1:8420
uv pip install --python .venv/bin/python -r requirements-restoration.txt  # Optional ML restoration
```

## Pipeline (CLI)
```bash
.venv/bin/python -m agents --source-path "/path/to/media/"
.venv/bin/python -m agents --source-path "/path/to/media/" --episode-id ep_2026-02-13_test
.venv/bin/python -m agents --source-path "/path/to/media/" --agents ingest stitch audio_analysis
```

## Tests
```bash
uv pip install --python .venv/bin/python -r requirements-dev.txt
.venv/bin/pytest              # All Python tests
.venv/bin/pytest -v           # Verbose
.venv/bin/pytest tests/test_agent_ingest.py  # Single test file
(cd frontend && node --test tests/*.test.mjs)  # Frontend helper/state tests
npm --prefix frontend run build               # Type-check + production bundle
```

## API
```bash
curl -X POST http://localhost:8420/api/episodes/ep_001/run-pipeline \
  -H "Content-Type: application/json" \
  -d '{"source_path": "/path/to/media/"}'
curl http://localhost:8420/api/episodes/ep_001/pipeline-status
curl -X POST http://localhost:8420/api/episodes/ep_001/auto-approve
```

## Server restarts
The normal launcher runs without `--reload`, because restarting the process
interrupts active audio and video preparation. After changing Python code,
wait for active jobs to finish, stop the server, and run `./start.sh` again.
Frontend changes only require `npm --prefix frontend run build` and a browser
refresh.
