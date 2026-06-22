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
| `/api/auth/login` | POST — login |
| `/api/auth/signup` | POST — signup |
| `/api/jobs` | POST — submit render job |
| `/api/jobs/<id>/status` | GET — poll job status |
| `/api/admin/*` | Admin endpoints |

---

## Architecture

```
User prose
    │
    ▼
ModelArk planner (seed-2-0-lite)
    │  outputs: shots[], music_prompt
    ▼
Seedance 2.0 render (per shot, async)
    │  Standard 1080p / Fast 720p
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

**1 credit = $0.01 USD**, charged at 1.5× cost margin.

| Operation | Cost basis | Credits (approx) |
|---|---|---|
| Fast shot (720p, 5s) | $0.015/s | 11 credits |
| Standard shot (1080p, 5s) | $0.075/s | 56 credits |
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
- [x] Credit system — atomic deduction, TOCTOU-safe
- [x] Planner — ModelArk chat completions, JSON output
- [x] Video render — Seedance 2.0 Standard + Fast, async poll (600s timeout)
- [x] ffmpeg stitch — partial success supported (failed shots skipped)
- [x] Admin panel
- [x] `index.html` — hero, sticky nav, "Open Studio →" CTA for logged-in users
- [x] `inner.html` — studio workspace with job polling
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

---

### 6. 🎭 Character consistency

Investigate Seedance / Kling persistent character references so the same face appears across shots without drift.

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
- [ ] Output video path on ephemeral volume → wiped on redeploy, needs durable storage (S3/R2)
- [ ] Signup bonus (120 credits) needs recalibration once real invoice data in
- [ ] Rotate compromised `ARK_API_KEY` in BytePlus console
- [ ] Suno API: no official access — monitor `suno.com/api/docs` for launch

---

## Environment Variables

| Var | Required | Description |
|---|---|---|
| `ARK_API_KEY` | ✅ | BytePlus ModelArk API key |
| `ARK_API_BASE` | optional | defaults to `https://ark.ap-southeast.bytepluses.com/api/v3` |
| `DATABASE_URL` | ✅ | PostgreSQL connection string |
| `SECRET_KEY` | ✅ | Flask session secret |
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
| `seed-1-6-250915` | Balanced |
| `seed-1-8-251228` | Higher quality |

### Video (Seedance 2.0)
| Model ID | Resolution | $/sec |
|---|---|---|
| `dreamina-seedance-2-0-260128` | 1080p Standard | $0.075 |
| `dreamina-seedance-2-0-fast-260128` | 720p Fast | $0.015 |

