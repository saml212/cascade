**Rule:** When iterating SD-card files, always skip macOS resource forks (`._*.MP4`, `._*.WAV`).

**Why:** DJI cameras and Zoom H6E SD cards land files on HFS+, which writes `._<name>` metadata sidecars. These are not real media files and break ffprobe/ffmpeg.

**Pattern:**
```python
for f in path.iterdir():
    if f.name.startswith("._"):
        continue
    ...
```
