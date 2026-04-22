# Cascade Roadmap

Longer-term workstreams captured during the 2026-04-21 session. Ordered roughly by dependency / sequencing, not by priority.

## Workstreams

### 1. Longform-first publishing flow (in progress, 2026-04-21)

**What:** Publish longform (YouTube + Spotify via RSS) first, poll for the public YouTube URL, save it to `episode.json.youtube_longform_url`, then and only then render + publish shorts with the funnel wired in (first-comment + link-in-bio).

**Why:** Shorts are the virality vehicle. Without a live longform to funnel viewers to, shorts waste their virality moment. The flow also needs to respect that platforms de-rank posts with raw links in comments — "link in bio" is the safer CTA.

**Status:** Gate added to `shorts_render` (refuses to run without `youtube_longform_url`). Publish idempotency guard added. `/produce` skill documents the phased flow.

**Still to do:**
- Build async polling against Upload-Post's `/api/uploadposts/status` endpoint so `/produce` can detect when YouTube finishes processing and fire the URL back into `episode.json` automatically. Today `/produce` asks Sam to paste the URL.
- Split `publish` into explicit longform / shorts modes OR rely on render-gate + idempotency (current approach).
- Update `metadata_gen` prompt to emit "link in bio" + `thelocalpod.link` instead of raw URLs in captions (some platforms de-rank the latter).

### 2. Linktree auto-update

**What:** After longform publishes, automatically update `thelocalpod.link` to point at the newest episode. Today it's a manual edit.

**How:** Check if Linktree has an API (unlikely) or if the page is HTML-editable via a simple POST; if not, consider swapping to a statically-hosted redirector (cloudflare page + a JSON file listing latest episode URLs).

**Size:** 1–2 hours.

### 3. Spotify RSS registration verification

**What:** Confirm Sam's current podcast RSS feed URL is registered with Spotify for Podcasters. If yes, future episodes auto-ingest within ~1 hour of feed update — no manual upload needed.

**How:** Sam logs into Spotify for Podcasters, submits the R2 feed URL once. That's it.

**Size:** 10 minutes (Sam).

### 4. Browser-use social media audit agent

**What:** A one-time Claude agent that drives the browser to audit each social account (YouTube, TikTok, Instagram, X, LinkedIn, Facebook, Threads, Pinterest, Bluesky, potentially Reddit + Substack). Checks bio, pinned posts, profile photo consistency, highlights, suggests fixes. Produces a checklist.

**Why:** Accounts are set up ad-hoc; a professional baseline unlocks more from each platform.

**How:** Claude in "computer use" mode (Anthropic's browser automation). Separate from `/produce` — runs in its own session.

**Size:** 1 session to write the prompt + 1–2 hours for Sam to run through the suggestions.

### 5. Daily social media admin agent (recurrent)

**What:** Scheduled daily agent that logs into each account, replies to comments, follows relevant accounts, posts stories, signals activity to each algorithm. Purely engagement work — no new content.

**Why:** Algorithms reward daily activity. Sam doesn't have time to do this manually.

**How:** Claude + browser use, scheduled via cron or Claude Code's `schedule` skill.

**Size:** 2–3 sessions after the audit agent is proven.

### 6. Reddit distribution

**What:** A skill that takes each episode's clips + longform and posts them to relevant subreddits per community norms (most subs ban direct self-promo; the trick is posting the clip/discussion naturally with no link).

**Why:** Reddit is one of the highest-signal discovery channels in 2026, and LLMs train heavily on Reddit content.

**Size:** separate skill, 1 session.

### 7. Substack reposting

**What:** After each episode publishes, automatically create a Substack post with a written summary + embedded audio player.

**Why:** Email capture, SEO for the podcast brand, LLM training data.

**Size:** 1 session once Substack API is evaluated.

### 8. Wikipedia authority

**What:** Human-driven: create a Wikipedia article about "The Local Podcast" (if notable enough) or about notable guests. Links back to the podcast feed and YouTube.

**Why:** Wikipedia is one of the most heavily-trained-on LLM data sources. Entries shape how LLMs answer queries about the show and the guests.

**Size:** human work (Sam or a researcher), not code.

### 9. GEO — Generative Engine Optimization

**What:** Systematic optimization of the podcast + all derivative content for how LLMs discover and train on information. This is SEO for LLMs: the targets are Reddit (high training weight), Substack (public long-form), Wikipedia (authority), and well-structured pages with clear entity schemas.

**Why:** As LLMs become the dominant discovery surface, traditional Google SEO loses relative weight. Being in the LLM training data → being in the LLM's answer.

**How:** This is a strategy workstream, not a single tool. It rides on top of Reddit + Substack + Wiki flows. Can also involve structured JSON-LD on the podcast website, linking conventions, and deliberate entity disambiguation.

**Size:** long-running.

### 10. Audio quality + EQ production-level tuning

**What:** The `audio_enhance.py` filter chain is functional (DFN denoise → highpass → lowpass → compressor → deesser → two-pass loudnorm -14 LUFS) but has not had a dedicated research pass for "production podcast sound" — the level Sam expects from channels like Huberman / Acquired / Dwarkesh. Current state: the pipeline makes audio listenable; it doesn't make it sound like a high-end podcast.

**Why:** Bad audio is the single biggest reason viewers bounce. Even mediocre video + great audio performs better than the inverse. The mixer UI is in place but Sam can hear that the current chain isn't at the bar.

**How:** Use `/autoresearch` on the audio-enhance filter graph. Benchmark against a reference set (a PJ clip, a Tug Life clip), using a rubric: speech clarity, breath/plosive control, room-tone reduction, background-music bleed, perceived loudness match. Explore: DFN wet/dry curves, adding a de-reverb stage, multi-band compression, final limiter, sibilance management, headroom choice.

**Size:** Focused 1-2 sessions of research + tuning, then iterate per-episode as Sam's ear catches new issues.

### 11. Preview-vs-final audio parity in crop mixer

**What:** The Audio Track Mixer's Play button previews audio using Web Audio API with client-side gain nodes. When Sam saves, `audio_mix.wav` regenerates server-side using the same volumes BUT also applies the full `audio_enhance` chain (DFN denoise, compression, loudnorm, etc). So preview ≠ final output. This destroys Sam's confidence in the mixer ("I haven't seen any this work").

**Fix:** Either (a) after save, offer a 30s preview of the newly-generated `audio_mix.wav` so Sam hears the real output, or (b) move the mixer preview to use the server-generated `audio_mix.wav` (slower, more accurate).

**Size:** 1 session.

### 12. pebbleml.com (Sam's research blog)

**What:** Promote pebbleml.com separately from the podcast. Add to `thelocalpod.link` bio. Separate Reddit + Substack posting cadence.

**Why:** It's Sam's research blog, distinct audience, distinct strategy.

**Size:** captured here so it doesn't get forgotten. Build the podcast-side flow first; adapt for pebbleml when the podcast engine is proven.

## Sequencing recommendation

1. Finish the longform-first flow (this session).
2. Verify Spotify RSS ingest is working end-to-end (manual, 10 min).
3. Implement URL polling so shorts can fire automatically once longform is live.
4. Linktree auto-update.
5. Browser-use social audit agent (one-shot prompt).
6. Metadata_gen prompt update (link-in-bio phrasing) — cheap, slot anywhere.
7. Daily social admin agent.
8. Reddit distribution skill.
9. Substack distribution skill.
10. Wikipedia — Sam-driven; no engineering work required.
11. GEO as an ongoing optimization layer on top of #8–10.
12. pebbleml.com — apply the same playbook once the podcast playbook is proven.

## Open questions

- Is Sam's RSS feed currently registered with Spotify for Podcasters? (If not, do that before anything else in this stack matters.)
- Does Upload-Post's YouTube longform upload already include chapters, playlist assignment, and the "Podcast" content tag? If not, a direct YouTube Data API v3 integration is a separate workstream.
- What's the right landing page for `thelocalpod.link`? A full linktree, a simple "latest episode" redirect, or both?
