# Publishing Subsystem — 2026-04-12

Continuation of `docs/PLAN_2026-04-11.md`. This file scopes the publishing-spec
work, what's being done **this** session, what's being deferred, and why. The
next session should start by reading this file.

## Context

User handed over a "Publishing Agent — Handoff Spec for Coding Agent" — a wide
spec covering ~10 platforms, trending audio, watermark removal, guest cross-
promo assets, link-tree management, analytics, and direct API integrations.
Almost all of the *baseline* described in that spec is already built in this
repo (Upload-Post-backed `publish` agent, per-platform metadata via `metadata_gen`,
RSS feed via `podcast_feed`, scheduling rules in `[schedule]`, link-in-bio
generator under `links/`).

The user's actual goals — distilled from the conversation:

1. Stop having to publish to Spotify manually through the Spotify for Podcasters
   web UI. (RSS-only path; no public Spotify upload API.)
2. Get YouTube longform + Shorts deploying reliably from the same flow.
3. Fix the X (Twitter) bug — recent shorts have not been posting to X.
4. Decide whether to add Facebook to the mix (recommendation: yes, low cost).
5. Move toward "more automation, less manual distribution work."

## What this session is doing

Tight cut, prioritized for verifiable end-to-end correctness over breadth.

1. **`approve-longform` route prefix bug** (`server/routes/pipeline.py:335`) —
   double `/episodes` prefix means the frontend's POST never hits the handler.
   This has been silently broken; longform approval has presumably been working
   only because the chat agent / direct curl bypasses it. Fix + route test.
2. **Frontend "Approve & Publish" button** — the `POST /api/episodes/{id}/approve-publish`
   endpoint exists and the publish agent's safety gate already enforces
   `publish_approved`, but there is no UI affordance. User can only invoke
   publishing via curl. Add a button that appears in the
   `awaiting_publish_approval` state with a confirm dialog showing the platforms
   and clip count.
3. **RSS feed hardening for Spotify / Apple ingestion** — `agents/podcast_feed.py`
   builds a feed missing several tags Apple validates and Spotify uses for
   episode ordering: `atom:link rel="self"`, `itunes:type=episodic` at channel,
   `itunes:episode`, `itunes:episodeType=full`, `itunes:summary`, `itunes:subtitle`,
   `content:encoded` (HTML show notes). Add them, validate the produced XML in
   a unit test, and make episode ordering deterministic.
4. **Per-platform error surfacing in `publish.py`** — currently the agent reads
   only `request_id` from the Upload-Post response and reports `status: submitted`
   even if a per-platform sub-upload failed. Persist the full response body into
   `publish.json`, and treat per-platform `success: false` as a per-platform
   failure surfaced in the result. This is a precondition for fixing the X bug
   in a verifiable way — without it we have no signal.
5. **Tests**: `tests/test_agent_publish.py`, `tests/test_agent_podcast_feed.py`,
   `tests/test_routes_pipeline.py` extensions. Cover safety gate, schedule
   conversion, RSS XML required-tag presence, and route-existence for
   approve-publish/longform.

## Deferred — captured here for the next session

Each item explains *why* it's deferred so we don't drift back into half-builds.

### X (Twitter) deep fix
- **What we know without SSD access**: `x_title` is the right field, X is enabled
  in `config.toml`, `metadata_gen` produces an `x.text` field. Most likely root
  cause: text exceeds 280 chars and `x_long_text_as_post` defaults to `false`
  on Upload-Post → silent per-platform failure swallowed by `publish.py`'s
  result handling.
- **Why deferred**: Need one real `publish.json` from a recent run where X
  failed, to confirm the response shape. SSD access is currently blocked by
  macOS TCC. Once unblocked or once user pastes the response, the fix is small:
  enforce 280-char truncation in `metadata_gen`, send `x_long_text_as_post=true`
  defensively, surface per-platform failure in `publish.json`. Step 4 above is
  the prerequisite.
- **Verification**: re-publish one episode, confirm X tweet appears, confirm
  `publish.json` shows `x.success=true`.

### Facebook page
- **Recommendation**: yes, set it up. Upload-Post supports `facebook` as a
  platform; `metadata_gen.py` already produces `facebook.title`/`description`.
  Cost: one-time Page creation + Meta Business connection in Upload-Post. Reach
  is mediocre vs IG/TT in 2026 but local-community FB groups still drive listens
  for Bay Area shows.
- **Why deferred**: Trivial code change (add `[platforms.facebook] enabled = true`
  to `config/config.toml`) but requires the user to actually create the Page +
  connect it in Upload-Post first. No code work needed once the user is ready.

### Direct YouTube Data API v3 integration
- **Why useful**: longform episodes deserve chapters (`#chapters` in description),
  playlist assignment, "Podcast" content tagging (enables YouTube Music
  distribution), A/B thumbnail testing via `Test & Compare`, custom captions
  upload, and post-publication analytics polling. Upload-Post supports basic
  YouTube uploads but doesn't expose any of these.
- **Why deferred**: Real OAuth flow with refresh-token storage, requires
  credential setup the user hasn't done yet (`YOUTUBE_CLIENT_ID`/`SECRET` env
  vars exist in `.env.example` but are unused). Different failure modes than
  Upload-Post (quota errors, OAuth token refresh). One full session of work to
  build right. Not blocking the rest of the publishing flow today.
- **Plan when picked up**: new `lib/youtube.py` with `google-api-python-client`,
  separate `agents/youtube_publish.py` running *after* `publish.py` for longform
  enrichment only (chapters + playlist + podcast tag). Keep Upload-Post as the
  primary path; YouTube direct is enrichment, not replacement.

### Spotify automation beyond RSS
- **What's possible**: nothing on the audio side. Spotify for Podcasters has no
  public upload API. The RSS feed (which podcast_feed already produces) is the
  *only* path. Once the feed is registered with Spotify and the iTunes tags are
  correct, episodes auto-ingest within ~hour of feed update. This session's
  RSS hardening is the actual unblocker.
- **Spotify "video clips" feature**: completely manual, no API. Defer
  permanently or until Spotify ships an API.
- **Action item for user**: register the R2 feed URL with Spotify for Podcasters
  *once* (one-time manual step), then never touch the website again.

### Trending audio overlay (TikTok / IG Reels)
- **Why useful**: TikTok algorithm signal — laying a trending sound very quietly
  under interview audio reportedly improves FYP distribution.
- **Why deferred**: Requires (a) a trending-sound source (TikTok Creative Center
  scrape or third-party API like Trendpop), (b) an audio mix step in
  `shorts_render.py`, (c) refresh logic to keep the trending list <7 days old,
  (d) per-clip audio mix in ffmpeg. Non-trivial. Also: the marginal benefit is
  unproven — could easily be cargo-cult. Defer until baseline is rock-solid and
  we can A/B with/without.

### Watermark removal (TikTok → IG cross-post)
- **Why useful**: TikTok-watermarked videos get suppressed on IG Reels.
- **Why deferred**: Cascade renders shorts directly from source video — there's
  no TikTok watermark to remove, because clips are not coming from re-downloads
  of TikTok. This item from the spec is **not applicable** to our pipeline.
  Document and drop.

### Per-clip landing pages
- **Why useful**: SEO surface, individual clip embeds, link-shareable URLs.
- **Why deferred**: Static-site generator territory. Useful but not on the
  critical path to "stop manually publishing to Spotify." Defer.

### Link-tree integration with the RSS feed
- **What user means**: `links/index.html` is generated separately from the RSS
  feed. The user's "unified RSS feed thing" is vague — most likely they want
  one single public landing page that surfaces both the latest episode and the
  social platform links (currently two separate things).
- **Why deferred**: The tactical answer is not RSS-related at all — it's a
  landing-page change in `links/template.html` to embed the feed's latest item
  via JS. Defer until the user clarifies what "unified" means or until the RSS
  hardening is shipped and we can revisit holistically.

### Analytics collection / request-id polling
- **What user wants**: per-post impressions, engagement, click-throughs, growth
  rate per platform per week.
- **Why deferred**: Upload-Post has a status endpoint (`/api/uploadposts/status`,
  already in `publish.py:26`) but we never call it. Building a real analytics
  collector means a recurring poller, a SQLite/Postgres table for time series,
  and a frontend dashboard. Big surface. Defer until baseline publishing is
  reliable.

### Guest cross-promotion assets (quote graphics, suggested copy)
- **Why deferred**: Whole new generation pipeline (image gen + caption draft +
  tag instructions). Defer. The lower-effort version — auto-emailing the guest
  a link to their episode + clip URLs — could be a 1-day add later.

## What we are explicitly NOT doing

- Trying to replace Upload-Post. User confirmed it's the intended backend.
- Implementing a parallel direct-API publishing path for TikTok/IG/X. Upload-Post
  handles those.
- Building the analytics dashboard or per-clip landing pages.
- Touching `shorts_render.py`'s ffmpeg pipeline (out of scope for publishing).

## Done-when (this session)

- Frontend: clicking "Approve & Publish" submits the episode with the correct
  guardrails and the publish agent runs.
- RSS feed XML passes a structural test for the required iTunes tags and is
  ordered deterministically.
- `publish.json` contains the full Upload-Post response body for each clip,
  including per-platform success/failure if the API exposes it.
- `tests/` has new tests that pin all of the above.
- The route prefix bug is fixed and a route test exists that would have caught
  it.
- This file exists with the deferred items documented.
