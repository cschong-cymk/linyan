# 琳嫣 Linyan

> End-to-end AI video pipeline: story text → shot plan → rendered video → lip sync & music

A hosted web app that turns prose storyboards into finished MP4 artifacts. Write a story; Linyan plans every shot, renders each clip via Seedance 2.0 (ByteDance), stitches with ffmpeg, and returns a downloadable video.

**Partners:** ByteDance (ModelArk / Seedance)  
**Stack:** Flask · PostgreSQL · ModelArk API · ffmpeg · sync.so · kie.ai/Suno

---

## Quick Start

```bash
# clone and install
pip install flask psycopg2-binary requests

# env vars required
export ARK_API_KEY=...          # BytePlus ModelArk key
export DATABASE_URL=...         # PostgreSQL connection string
export LINYAN_SECRET_KEY=...    # Flask session secret
export LINYAN_PUBLIC_BASE_URL=... # your public domain, required for character consistency (e.g. https://linyan.io)
export SYNC_API_KEY=...         # sync.so (planned)
export KIE_API_KEY=...          # kie.ai Suno wrapper (planned)

python app.py
```

### Routes
| Route | Description |
|---|---|
| `/` | Home page — auth, hero, marketing |
| `/studio` | Main workspace (`inner.html`) |
| `/settings` | Account settings |
| `/character-refs/<filename>` | GET — serves generated character reference images. Unauthenticated by design: ModelArk's servers fetch this directly when rendering a shot, not the logged-in user. |
| `/api/session` | GET — current user, settings, model catalog |
| `/api/ledger` | GET — credit transaction history |
| `/api/auth/login` | POST — login |
| `/api/auth/signup` | POST — signup |
| `/api/auth/logout` | POST — logout |
| `/api/quote` | POST — pre-planning cost estimate for a config (character count is a guess at this stage — see Credit System) |
| `/api/jobs` | GET — list your jobs · POST — submit render job |
| `/api/jobs/<id>/status` | GET — poll job status |
| `/api/jobs/<id>/download` | GET — download the finished artifact |
| `/api/jobs/<id>/source` | GET — the job's original storyboard text + full config, for reloading into the render form to edit and resubmit ("reprompt") |
| `/api/admin/*` | Admin endpoints — overview, credit top-ups, settings |

---

## Architecture

```
User prose
    │
    ▼
ModelArk planner (seed-2-0-lite)
    │  outputs: characters{name → description}, shots[], music_prompt
    ▼
Cost true-up: real character count → real cost → balance re-check
    │  (fails fast here if the user can't afford it — before any render spend)
    ▼
Per-character reference image
    │  one short Seedance clip per character → ffmpeg frame-extract → re-hosted PNG
    │  best-effort: skipped entirely if LINYAN_PUBLIC_BASE_URL isn't set
    ▼
Seedance 2.0 render (per shot, async)
    │  Standard 1080p / Fast 720p
    │  each shot's prompt has its characters' descriptions locked in verbatim,
    │  plus their reference image(s) attached as image_url content
    ▼
ffmpeg stitch (concat demuxer)
    │
    ├──[lipsync ON]──▶ sync.so lipsync API ──▶ synced video
    │
    ├──[music ON]────▶ Suno via kie.ai ──▶ MP3
    │                       │
    └───────────────────────▼
                    ffmpeg audio mix (-20dB)
                            │
                            ▼
                        final MP4
```

---

## Credit System

**1 credit = $0.01 USD**, charged at 1.5× cost margin (admin-tunable).

Pricing now has two phases, because character reference generation (see *Character consistency* below) is itself a real Seedance render whose cost depends on how many named characters are in the storyboard — and that's only known after the planner runs.

1. **Pre-planning quote** (`/api/quote`, and the upfront balance check when a job is created): character count is unknown yet, so cost is estimated using `settings.quote_character_assumption` (default 2, admin-tunable via `/api/admin/settings`). This is shown as an estimate, not charged.
2. **Post-planning true-up**: once the planner has actually run and the real character count is known, the job's cost is recomputed and the user's balance is re-checked *before any shot or reference image is generated*. If the real cost exceeds their balance, the job fails here — with zero render spend incurred, not after spending on shots that then can't be paid for. The job's stored `estimated_credits` is updated to this real number, and this is what actually gets charged on completion.

| Operation | Cost basis | Credits (approx) |
|---|---|---|
| Fast shot (720p, 5s) | $0.015/s | 11 credits |
| Standard shot (1080p, 5s) | $0.075/s | 56 credits |
| Character reference (one 4s clip, Standard) | $0.075/s × 4s | ~45 credits *(per named character, per job)* |
| Lip sync (lipsync-2, 25s video) | $0.06/s | 90 credits |
| Background music | flat fee | 5–10 credits |

### Pricing Tiers (planned)
| Tier | Credits/mo | Price | Includes |
|---|---|---|---|
| Free | 120 | $0 | ~1 Fast job |
| Starter | 2,000 | $15 | Standard render |
| Pro | 8,000 | $50 | + lip sync |
| Studio | 25,000 | $120 | + dubbing, priority queue |

---

## What's Shipped

- [x] Auth — signup / login / session
- [x] Credit system — atomic deduction, TOCTOU-safe, two-phase pricing (pre-planning estimate → post-planning true-up against real character count, balance re-checked before any render spend)
- [x] Planner — ModelArk chat completions, JSON output, now also extracts a per-character "bible" (name + canonical visual description)
- [x] Character consistency — each character's description is locked verbatim into every shot's prompt it appears in (not just asked-for-nicely), plus one auto-generated reference image per character attached to every shot containing them. ⚠️ The reference-image step (rendering a throwaway clip + ffmpeg frame-extract, then attaching it as `image_url` content on the real shot request) has not been confirmed against a live ModelArk call — see *Known limitations* below. Gated behind `LINYAN_PUBLIC_BASE_URL`; falls back cleanly to text-only locking if unset.
- [x] Video render — Seedance 2.0 Standard + Fast, async poll (600s timeout)
- [x] ffmpeg stitch — partial success supported (failed shots skipped)
- [x] Job reprompt — `/api/jobs/<id>/source` + an "Edit & reprompt" action in the Studio journal reload a past job's storyboard text and full config into the render form, for any job status (including failed). Submitting creates a new job; the original is untouched.
- [x] Admin panel — now includes `quote_character_assumption` as a tunable setting
- [x] `index.html` — hero, sticky nav (Studio link now points at the real `/studio` page rather than a dead in-page anchor), "Open Studio →" CTA for logged-in users
- [x] `inner.html` — studio workspace with job polling, logo links back home, Admin shortcut in the topbar (admins only)
- [x] `settings.html` — account settings

---

## Roadmap

### 1. 🎙 Lip Sync — sync.so

Sync character mouth movements to narration audio after video is stitched.

**API:** `POST https://api.sync.so/v2/generate`

| Model | Quality | $/sec @ 25fps |
|---|---|---|
| `lipsync-1.9.0-beta` | Fast | $0.02 |
| `lipsync-2` | Good | $0.06 |
| `lipsync-2-pro` | Premium | $0.10 |
| `sync-3` | Best (4K) | $0.133 |

```python
# submit
POST https://api.sync.so/v2/generate
{
  "model": "lipsync-2",
  "input": [
    {"type": "video", "url": "<video_url>"},
    {"type": "audio", "url": "<audio_url>"}
  ],
  "options": {"output_format": "mp4"}
}
# → {"id": "sync-xxx"}

# poll
GET https://api.sync.so/v2/generate/{id}
# → {"status": "completed", "outputUrl": "..."}
```

**Env:** `SYNC_API_KEY`  
**UI:** checkbox — "Sync lips to narration audio" — off by default  
**SDK:** `pip install syncsdk`

---

### 2. 🗣 Voice / TTS for narration

Generate narration audio from the planner's `narration` field per shot. Required input for lip sync.

Options (evaluating):
- **sync.so built-in TTS** — simplest, one fewer API
- **ModelArk TTS** — keeps everything in ByteDance family
- **ElevenLabs** — highest quality, multilingual

---

### 3. 🌍 Dubbing / Localisation

sync.so supports full dubbing: translate + TTS + lipsync in one call.

Supported languages: EN, ZH, FR, HI, IT, JA, KO, PT, RU, TR, ES, DE, AR, PL, ID, FI, SV

**Use case:** render in English → dub to Mandarin or Japanese in one step.

---

### 4. 🎵 Background Music — Suno via kie.ai

Generate a score from a text prompt and mix it under the final video.

> ⚠️ **Suno has no official public API** (as of June 2026). V5 launched Sept 2025 but API access is still partner/beta only. Using **kie.ai** as cleanest third-party wrapper in the interim.

| Provider | Notes |
|---|---|
| **kie.ai** (`docs.kie.ai`) | Clean REST, V4/V4.5/V5, recommended |
| sunoapi.org | OpenAI-compatible format, cookie-based underneath |
| gcui-art/suno-api | OSS self-hosted, fragile (requires 2captcha) |

**Flow:**
```
planner outputs music_prompt (one per video, not per shot)
→ POST /suno/generate (instrumental, text-to-music)
→ poll for audioUrl (MP3)
→ ffmpeg mix at -20dB under narration
```

**Example planner output:** `"sparse piano and strings, melancholic, slow tempo, no lyrics"`

**ffmpeg mix:**
```bash
ffmpeg -i video.mp4 -i music.mp3 \
  -filter_complex "[1:a]volume=0.2[music];[0:a][music]amix=inputs=2:duration=first[a]" \
  -map 0:v -map "[a]" -c:v copy -c:a aac output.mp4
```

**Env:** `KIE_API_KEY`  
**UI:** music style field pre-filled by planner, editable. Toggle on/off.  
**Credits:** flat fee per job (~5–10 credits)

---

### 5. 🖼 Shot-level image anchoring

Upload reference image per shot (character face, location) → passed as `start_image` to Seedance. Improves visual consistency across shots.

*Note: the per-character reference image generated automatically for character consistency (see What's Shipped) uses the same `image_url` attachment mechanism this would need — extending it to accept a direct user upload instead of an auto-generated one is mostly UI work at this point, not new plumbing.*

---

### 6. ✅ ~~Character consistency~~ — shipped, see *What's Shipped*

Verifying the reference-image request shape (`image_url` content type, attached per shot) against a live ModelArk call is the one piece of this still outstanding — see *Known limitations*. Once confirmed, the same reference-image plumbing built for this could be extended to accept a user-uploaded image directly (folding in Roadmap item 5, shot-level image anchoring, as a special case rather than a separate feature).

---

### 7. ⚙️ Background job queue — Celery / Redis

Current rendering blocks the request thread sequentially.  
Fix: Celery workers for render + lipsync + music. Frontend polls `/api/jobs/<id>/status`.

Enables: parallel music generation during shot rendering (saves time).

---

### 8. 🔔 Webhooks

Register a URL to receive job completion POSTs instead of polling. Target: Pro/Studio tier.

---

## Open Issues

- [ ] `init_db()` race condition in gunicorn multi-worker → needs Alembic migrations
- [ ] Output video path on ephemeral volume → wiped on redeploy, needs durable storage (S3/R2). **This now also takes the job-reprompt feature down with it** — storyboard files live on the same volume, so a redeploy doesn't just lose finished renders, it loses the ability to reload and reprompt every past job too. Was a "missing nice-to-have" before; now it's a "feature silently breaks" risk.
- [ ] Signup bonus (120 credits) needs recalibration once real invoice data in — character reference costs (new, ~45 credits per named character per job) make jobs with multiple characters meaningfully more expensive than the original estimate assumed; recheck whether 120 credits is still a reasonable free allotment
- [ ] Rotate compromised `ARK_API_KEY` in BytePlus console
- [ ] Suno API: no official access — monitor `suno.com/api/docs` for launch
- [ ] **Verify the `image_url` content-type attachment on `/contents/generations/tasks`** against a real ModelArk call — character reference images are built on this assumption (mirroring the existing `content: [{"type": "text", ...}]` pattern already proven for shots) but it has not been confirmed live. If wrong: requests either 400 (caught — fails just that shot, no worse than today) or silently ignore the field (falls back to text-only locking, also no worse than today) — but "no worse than today" isn't "verified working."
- [ ] `quote_character_assumption` (default 2) is a guess, not derived from real storyboard data — recalibrate once there's a real distribution of how many named characters jobs actually have

## Known limitations

- **`LINYAN_PUBLIC_BASE_URL` is required for character consistency to do anything beyond text-locking.** Without it, reference image generation is skipped entirely and silently — no error, just text-only locking, same as before this feature shipped. Easy to deploy without noticing it's off.
- **`ffmpeg` is now a hard dependency at render time, not just at stitch time** — character reference generation extracts a frame from a throwaway clip via `ffmpeg -ss ... -frames:v 1`. If `ffmpeg` is missing or fails, that one character just doesn't get a reference image (caught, non-fatal) — but it's a new failure surface in a part of the request path that previously didn't touch ffmpeg at all.
- **`/character-refs/<filename>` is intentionally unauthenticated.** ModelArk's servers fetch it directly and carry no session cookie, so it can't require login. Filenames are job-id + random-suffix based, not guessable, but anyone who obtains a URL can view that image without authenticating — acceptable for AI-generated character portraits, worth a second look if storyboards ever contain anything more sensitive.

---

## Environment Variables

| Var | Required | Description |
|---|---|---|
| `ARK_API_KEY` | ✅ | BytePlus ModelArk API key |
| `ARK_API_BASE` | optional | defaults to `https://ark.ap-southeast.bytepluses.com/api/v3` |
| `DATABASE_URL` | ✅ | PostgreSQL connection string |
| `LINYAN_SECRET_KEY` | ✅ | Flask session secret. (Was previously documented here as `SECRET_KEY` — that was always wrong; the code reads `LINYAN_SECRET_KEY`. Falls back to an insecure dev default if unset, so this is required in any real deployment even though the code won't refuse to boot without it.) |
| `LINYAN_PUBLIC_BASE_URL` | required for character consistency | Your public domain (e.g. `https://linyan.io`). ModelArk's servers — not the user's browser — fetch character reference images from this host, so it needs to be internet-reachable, not localhost. Bare domains without a scheme are auto-prefixed with `https://`. Without this set, character consistency silently falls back to text-only locking — no error. |
| `SYNC_API_KEY` | planned | sync.so lip sync key |
| `KIE_API_KEY` | planned | kie.ai Suno music key |
| `SHOT_POLL_TIMEOUT` | optional | defaults to 600s |
| `SHOT_POLL_INTERVAL` | optional | defaults to 10s |

---

## Models

### Planner (ModelArk)
| Model ID | Notes |
|---|---|
| `seed-2-0-lite-260228` | Default, fast |
| `seed-1-8-251228` | Deep reasoning; best for complex multi-scene storyboards |
| `glm-4-7-251222` | Long-context scene planning, ×1.1 cost factor |

### Video (Seedance 2.0)
| Model ID | Resolution | $/sec |
|---|---|---|
| `dreamina-seedance-2-0-260128` | 1080p Standard | $0.075 |
| `dreamina-seedance-2-0-fast-260128` | 720p Fast | $0.015 |

