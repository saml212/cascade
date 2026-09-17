# Error Handling

- If a pipeline fails mid-run, fix the issue and re-run with `--agents <remaining_agents>`. The pipeline is idempotent — already-completed agents' outputs are read rather than recomputed.
- Each agent's JSON output includes `_status`, `_elapsed_seconds`, and `_error` (if failed).
- Check `episode.json` → `pipeline.agents_completed` to see what's already done.
- Video/short publication, backup, and thumbnail stages are non-critical and do not erase other completed work when they fail.

## Resume pattern
```bash
# See what's done
cat /Volumes/1TB_SSD/cascade/episodes/<ep_id>/episode.json | jq '.pipeline.agents_completed'

# Resume from the failed stage
.venv/bin/python -m agents --source-path "<path>" --episode-id <ep_id> --agents <stage> <stage> ...
```
