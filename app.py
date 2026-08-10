from dotenv import load_dotenv
load_dotenv()
import hashlib
import hmac
import json
import os
import shutil
import threading
import subprocess
import textwrap
import time
import uuid
import zipfile
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import quote as url_quote


import psycopg2
import psycopg2.extras

from flask import Flask, g, jsonify, redirect, render_template, request, send_file, send_from_directory, session
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

APP_ROOT = Path(__file__).resolve().parent
TEMPLATE_DIR = APP_ROOT / "templates"
DATA_DIR = APP_ROOT / "data"
ASSETS_DIR = APP_ROOT / "assets"
UPLOAD_DIR = DATA_DIR / "uploads"
OUTPUT_DIR = DATA_DIR / "outputs"
CHARACTER_DIR = DATA_DIR / "characters"
DATA_DIR.mkdir(exist_ok=True)
UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)
CHARACTER_DIR.mkdir(exist_ok=True)

APP_SECRET = os.environ.get("LINYAN_SECRET_KEY", "linyan-dev-secret-change-me")
DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgres://postgres:postgres@localhost:5432/flask"
)
# ModelArk (BytePlus Ark) — used for planning, video generation, and now
# character reference images (rendered via the same video endpoint — see
# generate_character_reference_image).
ARK_API_KEY = os.environ.get("ARK_API_KEY")
ARK_API_BASE = os.environ.get("ARK_API_BASE", "https://ark.ap-southeast.bytepluses.com/api/v3")

# ── kie.ai (Suno wrapper) — background music ─────────────────────────────────
# Verified live 2026-07-23: POST /generate (callBackUrl is REQUIRED even when
# polling; a dead callback URL doesn't block completion), poll
# GET /generate/record-info until status SUCCESS, download sunoData[0].audioUrl
# (their CDN 403s Python's default User-Agent — send a browser UA).
KIE_API_KEY = os.environ.get("KIE_API_KEY")
KIE_API_BASE = os.environ.get("KIE_API_BASE", "https://api.kie.ai/api/v1")
MUSIC_FLAT_CREDITS = 10.0     # flat fee per job with music enabled (README: 5–10)
MUSIC_POLL_TIMEOUT = 300      # observed live: ~2.5 min to SUCCESS
MUSIC_POLL_INTERVAL = 10
MUSIC_DL_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

# ── Stripe (credit purchases) ────────────────────────────────────────────────
# STRIPE_PAYMENT_LINK is a Stripe-hosted Payment Link (buy.stripe.com/...).
# We redirect logged-in users to it with ?client_reference_id=<user_id> so the
# resulting checkout.session.completed webhook event can be matched back to the
# account that paid. One Payment Link sells exactly one product at one price —
# credits are granted from the *amount actually paid* (see stripe webhook
# handler), so adding more links/tiers later needs no code change.
STRIPE_PAYMENT_LINK = os.environ.get(
    "STRIPE_PAYMENT_LINK", "https://buy.stripe.com/fZueVd17u5Au5mU88z8Vi00"
).strip()

# Stripe Secret Key - required for Checkout Sessions API
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "").strip()

# Webhook signing secret ("whsec_...") from the Stripe Dashboard endpoint you
# create at Developers → Webhooks → Add endpoint → https://<domain>/api/stripe/webhook.
# REQUIRED for payments to credit accounts. Without it the webhook endpoint
# refuses all events (503) rather than trusting unauthenticated POSTs — anyone
# on the internet can POST JSON at this route, so signature verification is the
# only thing standing between "payment system" and "free credits endpoint".
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()
# Reject webhook events whose signature timestamp is older than this (replay
# protection window, seconds). Matches Stripe's own SDK default.
STRIPE_SIGNATURE_TOLERANCE = 300

# Public, internet-reachable base URL for this app (e.g. https://linyan.io).
# Required because ModelArk's servers — not the user's browser — fetch the
# reference image URL we hand them when rendering a shot. A localhost or
# private URL is unreachable from their side and will just fail every shot
# silently. Left empty by default for the same reason: no guessing. Bare
# domains (e.g. "linyan.io" with no scheme) are normalized to https:// rather
# than silently producing a malformed URL.
PUBLIC_BASE_URL = os.environ.get("LINYAN_PUBLIC_BASE_URL", "").rstrip("/")
if PUBLIC_BASE_URL and not PUBLIC_BASE_URL.startswith(("http://", "https://")):
    PUBLIC_BASE_URL = f"https://{PUBLIC_BASE_URL}"

# How long to poll for a single shot clip before giving up (seconds)
SHOT_POLL_TIMEOUT = int(os.environ.get("SHOT_POLL_TIMEOUT", "600"))
SHOT_POLL_INTERVAL = int(os.environ.get("SHOT_POLL_INTERVAL", "10"))

ASPECT_PRESETS = {
    "16:9": (1280, 720),
    "9:16": (720, 1280),
    "1:1": (1080, 1080),
    "4:5": (864, 1080),
}

# Duration of the throwaway clip rendered per character for reference-image
# extraction (see generate_character_reference_image). Shared with
# estimate_job_cost, which prices that render in — it's a real Seedance call,
# not a freebie.
REFERENCE_CLIP_DURATION = 4   # Seedance's minimum valid duration — cheapest still-frame source

MODEL_CATALOG = {
    "planner_models": [
        {
            "id": "seed-2-0-lite-260228",
            "label": "Seed 2.0 Lite",
            "family": "ark",
            "recommended": True,
            "summary": "Fast story breakdown and shot planning.",
        },
        {
            "id": "seed-1-8-251228",
            "label": "Seed 1.8",
            "family": "ark",
            "recommended": False,
            "summary": "Deep reasoning mode; best for complex multi-scene storyboards.",
        },
{
    "id": "glm-4-7-251222",
    "label": "GLM-4.7",
    "family": "ark",
    "recommended": False,
    "summary": "Long-context scene planning.",
    "cost_factor": 1.1,
},
    ],
    "video_models": [
        {
            "id": "dreamina-seedance-2-0-260128",
            "label": "Seedance 2.0 Standard",
            "provider": "ark",
            "cost_factor": 1.0,
            "summary": "1080p. Balanced quality and speed for commercial use.",
        },
        {
            "id": "dreamina-seedance-2-0-fast-260128",
            "label": "Seedance 2.0 Fast",
            "provider": "ark",
            "cost_factor": 0.5,
            "summary": "720p. Rapid prototyping and draft iterations.",
        },
    ],
    "voice_models": [
        {"id": "none", "label": "No narration"},
        {"id": "warm-guide", "label": "Warm Guide"},
        {"id": "campaign-voice", "label": "Campaign Voice"},
        {"id": "neutral-briefing", "label": "Neutral Briefing"},
    ],
}

DEFAULT_SETTINGS = {
    "allow_signup": True,
    "margin_multiplier": 1.5,
    "signup_bonus_credits": 120.0,
    "credit_label": "Linyan credits",
    "default_planner_model": "seed-2-0-lite-260228",
    "default_video_model": "dreamina-seedance-2-0-260128",
    # Used only when the real character count isn't known yet (pre-planning
    # quotes and the upfront balance check) — see estimate_job_cost. This is a
    # guess, admin-tunable, not a measurement.
    "quote_character_assumption": 2,
    # Credits granted per USD actually paid through Stripe. 1 credit = $0.01
    # cost basis, so 100 = face value (a $15 payment grants 1,500 credits).
    # NOTE: the README's planned tiers promise *better* rates at higher tiers
    # ($15→2,000 ≈ 133/$; $120→25,000 ≈ 208/$). A single flat rate can't
    # express that — see admin panel note. Tunable via /api/admin/settings.
    "credits_per_dollar": 100.0,
}

# Linux container font candidates (macOS paths removed)
DRAW_TEXT_FONTS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
]

app = Flask(__name__, template_folder=str(TEMPLATE_DIR))
app.secret_key = APP_SECRET
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024


@app.after_request
def add_no_cache_headers(resp):
    """Stop browsers/proxies serving stale HTML.

    The CSS is inlined inside the templates, so there is no separate
    stylesheet file to version with a ?v=timestamp query string. Instead
    we tell clients never to cache the HTML, which means every markup/CSS
    change shows up on the next request. (Cloudflare still has its own edge
    cache — purge it once after deploying if a change doesn't appear.)
    """
    if resp.mimetype == "text/html":
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    return resp


@app.route("/assets/<path:filename>")
def assets(filename):
    """Serve files from the assets/ folder (e.g. /assets/linyan.mp4).

    Flask's default static folder isn't used by this app, so media bundled
    in assets/ (shipped into the image by the Dockerfile's COPY . .) needs an
    explicit route. send_from_directory keeps it safe against path traversal.
    """
    return send_from_directory(ASSETS_DIR, filename)


@app.route("/character-refs/<path:filename>")
def character_refs(filename):
    """Serve generated character reference images.

    Deliberately unauthenticated: ModelArk's servers fetch this URL directly
    when rendering a shot, and they carry no session cookie, so a login_required
    decorator here would just make every shot fail. This mirrors the existing
    /assets/<filename> trust model. Filenames are job-id + random-suffix based
    (see generate_character_reference_image), not derived from anything a
    different user controls, but note this does mean: anyone who obtains one of
    these URLs can view that reference image without authenticating. Acceptable
    for AI-generated character portraits; flag it if storyboards ever contain
    anything more sensitive than that.
    """
    return send_from_directory(CHARACTER_DIR, filename)


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def new_connection():
    """Create a new psycopg2 connection with dict-style rows."""
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    return conn


def get_db():
    if "db" not in g:
        g.db = new_connection()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def db_execute(query, params=(), commit=False):
    """Helper: run a query on the request-scoped connection."""
    db = get_db()
    cur = db.cursor()
    cur.execute(query, params)
    if commit:
        db.commit()
    return cur


def init_db():
    db = new_connection()
    cur = db.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            display_name TEXT NOT NULL,
            is_admin BOOLEAN NOT NULL DEFAULT FALSE,
            credit_balance REAL NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            title TEXT NOT NULL,
            status TEXT NOT NULL,
            original_filename TEXT NOT NULL,
            storyboard_path TEXT NOT NULL,
            render_plan_path TEXT,
            output_path TEXT NOT NULL,
            output_kind TEXT NOT NULL,
            planner_model TEXT NOT NULL,
            video_model TEXT NOT NULL,
            provider TEXT NOT NULL,
            estimated_credits REAL NOT NULL,
            params_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    # Prompt history: storyboard text lives in the DB, not just as a file on
    # the ephemeral volume. Older rows have NULL here; job_source falls back
    # to storyboard_path for those (until the file is lost to a redeploy).
    cur.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS storyboard_text TEXT;")
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS stripe_events (
            event_id TEXT PRIMARY KEY,
            event_type TEXT NOT NULL,
            object_id TEXT,
            user_id INTEGER,
            amount_cents INTEGER,
            currency TEXT,
            credits REAL,
            status TEXT NOT NULL,
            note TEXT,
            created_at TEXT NOT NULL
        );
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS credit_ledger (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            delta REAL NOT NULL,
            reason TEXT NOT NULL,
            note TEXT,
            actor_user_id INTEGER,
            created_at TEXT NOT NULL
        );
        """
    )
    # Ground-truth provider usage per Ark task, as reported by the poll
    # response's `usage` block. Job charges are computed from an internal $/s
    # assumption table; this is the raw material for verifying that assumption
    # against actual BytePlus billing (margin/mispricing analysis).
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS provider_usage (
            id SERIAL PRIMARY KEY,
            job_id TEXT,
            kind TEXT NOT NULL DEFAULT 'shot',
            provider TEXT NOT NULL DEFAULT 'ark',
            model TEXT NOT NULL,
            task_id TEXT,
            status TEXT,
            duration_seconds INTEGER,
            resolution TEXT,
            completion_tokens BIGINT,
            total_tokens BIGINT,
            created_at TEXT NOT NULL
        );
        """
    )
    timestamp = now_iso()
    for key, value in DEFAULT_SETTINGS.items():
        cur.execute(
            """
            INSERT INTO settings (key, value, updated_at)
            VALUES (%s, %s, %s)
            ON CONFLICT (key) DO NOTHING
            """,
            (key, json.dumps(value), timestamp),
        )
    db.commit()
    db.close()


def decode_setting(raw_value):
    try:
        return json.loads(raw_value)
    except (json.JSONDecodeError, TypeError):
        return raw_value


def load_settings():
    cur = db_execute("SELECT key, value FROM settings")
    rows = cur.fetchall()
    settings = dict(DEFAULT_SETTINGS)
    for row in rows:
        settings[row["key"]] = decode_setting(row["value"])
    return settings


def save_setting(key, value):
    db_execute(
        """
        INSERT INTO settings (key, value, updated_at)
        VALUES (%s, %s, %s)
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at
        """,
        (key, json.dumps(value), now_iso()),
        commit=True,
    )


def current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    cur = db_execute(
        """
        SELECT id, email, display_name, is_admin, credit_balance, created_at
        FROM users
        WHERE id = %s
        """,
        (user_id,),
    )
    return cur.fetchone()


def login_required(handler):
    @wraps(handler)
    def wrapped(*args, **kwargs):
        user = current_user()
        if not user:
            return jsonify({"error": "Authentication required."}), 401
        g.current_user = user
        return handler(*args, **kwargs)

    return wrapped


def admin_required(handler):
    @wraps(handler)
    def wrapped(*args, **kwargs):
        user = current_user()
        if not user:
            return jsonify({"error": "Authentication required."}), 401
        if not user["is_admin"]:
            return jsonify({"error": "Admin access required."}), 403
        g.current_user = user
        return handler(*args, **kwargs)

    return wrapped


def serialize_user(row):
    return {
        "id": row["id"],
        "email": row["email"],
        "display_name": row["display_name"],
        "is_admin": bool(row["is_admin"]),
        "credit_balance": round(row["credit_balance"], 1),
        "created_at": row["created_at"],
    }


def apply_credit_delta(user_id, delta, reason, note="", actor_user_id=None):
    db = get_db()
    cur = db.cursor()
    cur.execute(
        "UPDATE users SET credit_balance = ROUND(CAST(credit_balance + %s AS numeric), 1) WHERE id = %s",
        (delta, user_id),
    )
    cur.execute(
        """
        INSERT INTO credit_ledger (user_id, delta, reason, note, actor_user_id, created_at)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (user_id, round(delta, 1), reason, note, actor_user_id, now_iso()),
    )
    db.commit()


def clamp_float(value, default, minimum, maximum):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def clamp_int(value, default, minimum, maximum):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def normalize_config(raw_config, settings):
    config = {
        "title": (raw_config.get("title") or "Untitled project").strip(),
        "planner_model": raw_config.get("planner_model") or settings["default_planner_model"],
        "video_model": raw_config.get("video_model") or settings["default_video_model"],
        "aspect_ratio": raw_config.get("aspect_ratio") or "16:9",
       
"video_model": raw_config.get("video_model") or settings["default_video_model"],
"resolution": raw_config.get("resolution") or ("720p" if "fast" in (raw_config.get("video_model") or settings["default_video_model"]) else "1080p"),
        "style_preset": raw_config.get("style_preset") or "Cinematic realism",
        "voice_model": raw_config.get("voice_model") or "none",
        "narration_enabled": bool(raw_config.get("narration_enabled")),
        "target_duration": clamp_int(raw_config.get("target_duration"), 30, 8, 180),
        "planner_temperature": clamp_float(raw_config.get("planner_temperature"), 0.4, 0.0, 1.0),
        "direction_temperature": clamp_float(raw_config.get("direction_temperature"), 0.5, 0.0, 1.0),
        "motion_temperature": clamp_float(raw_config.get("motion_temperature"), 0.5, 0.0, 1.0),
        "dialogue_temperature": clamp_float(raw_config.get("dialogue_temperature"), 0.4, 0.0, 1.0),
        "consistency_strength": clamp_float(raw_config.get("consistency_strength"), 0.8, 0.1, 1.0),
        "music_enabled": bool(raw_config.get("music_enabled")),
        # Optional user override; when empty the planner's music_prompt is used.
        # kie.ai non-custom mode caps prompt at 500 chars; leave headroom for
        # the ", instrumental" suffix added in generate_background_music.
        "music_prompt": str(raw_config.get("music_prompt") or "").strip()[:450],
    }
    if config["aspect_ratio"] not in ASPECT_PRESETS:
        config["aspect_ratio"] = "16:9"
    if config["voice_model"] == "none":
        config["narration_enabled"] = False
    return config


def estimate_job_cost(config, settings, character_count=None):
    """
    Estimate job cost in Linyan credits based on real Seedance 2.0 published rates.

    Published USD rates (per second of generated video):
      dreamina-seedance-2-0-260128       (Standard, 1080p): $0.05–$0.10  → midpoint $0.075/s
      dreamina-seedance-2-0-fast-260128  (Fast,     720p):  $0.01–$0.02  → midpoint $0.015/s

    1 Linyan credit = $0.01 USD. Margin multiplier applied on top (default 1.5 = 50% margin).

    Two billed components, both priced at the job's own per-second rate, because
    a character reference is now literally a REFERENCE_CLIP_DURATION-second
    video render (see generate_character_reference_image), not a separate,
    separately-priced image call:
      1. target_duration seconds for the final video.
      2. REFERENCE_CLIP_DURATION seconds per named character.

    character_count has two different levels of truth depending on the caller,
    and that distinction matters — don't blur it:
      - None (the default): character extraction is an LLM step the planner
        hasn't run yet, so there is no real count available. Falls back to
        settings["quote_character_assumption"] — an admin-tunable GUESS. Used
        by /api/quote (shown before any storyboard is even uploaded) and by
        the upfront balance gate in create_job_record (before planning runs).
        Treat any number this produces as an estimate range, not a bill.
      - An explicit int: the real, measured count from
        len(planner["characters"]) after call_planner has actually run. Used
        by run_job_async to recompute the TRUE cost and re-check the user's
        balance before rendering any shots, and to charge that real number —
        not the pre-planning guess — on success.
    """
    SEEDANCE_USD_PER_SECOND = {
        "dreamina-seedance-2-0-260128":      0.075,
        "dreamina-seedance-2-0-fast-260128": 0.015,
    }
    CREDITS_PER_USD = 100.0  # 1 credit = $0.01

    video_model = config["video_model"]
    usd_per_second = SEEDANCE_USD_PER_SECOND.get(video_model, 0.075)
    provider = "ark"
    margin = float(settings["margin_multiplier"])

    if character_count is None:
        character_count = clamp_int(settings.get("quote_character_assumption"), 2, 0, 20)

    video_seconds = config["target_duration"]
    reference_seconds = character_count * REFERENCE_CLIP_DURATION

    raw_usd = usd_per_second * (video_seconds + reference_seconds)
    charged_usd = raw_usd * margin
    credits = charged_usd * CREDITS_PER_USD

    # Background music: flat fee, margin NOT applied — MUSIC_FLAT_CREDITS is
    # already the charged price (kie.ai's per-generation cost is cents; a flat
    # 10 credits comfortably covers it). Refunded by run_job_async if music
    # generation fails or is skipped: the fee is only kept when music ships.
    if config.get("music_enabled"):
        credits += MUSIC_FLAT_CREDITS

    return round(credits, 1), provider


def ffmpeg_text(value):
    return (
        value.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\'")
        .replace("%", "\\%")
        .replace(",", "\\,")
    )


def first_existing_font():
    for candidate in DRAW_TEXT_FONTS:
        if Path(candidate).exists():
            return candidate
    return None


def generate_placeholder_video(output_path, title, job_id, config, storyboard_text):
    width, height = ASPECT_PRESETS[config["aspect_ratio"]]
    clip_seconds = min(max(config["target_duration"], 6), 12)
    color_source = f"color=c=#0d1b16:s={width}x{height}:d={clip_seconds}"
    command = [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        color_source,
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=330:sample_rate=44100:duration={clip_seconds}",
    ]
    command.extend(
        [
            "-shortest",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    )
    subprocess.run(command, check=True, capture_output=True)


def generate_placeholder_bundle(output_path, title, job_id, config, storyboard_text, planner_payload):
    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("storyboard.md", storyboard_text)
        archive.writestr(
            "job.json",
            json.dumps(
                {
                    "job_id": job_id,
                    "title": title,
                    "config": config,
                    "planner": planner_payload,
                    "note": "Fallback package because MP4 generation failed.",
                },
                indent=2,
            ),
        )


def _find_character(name, character_bible):
    """Case-insensitive character bible lookup. Returns (canonical_name, entry) or None.

    Shared by lock_characters_into_prompt and call_video_model so both pick the
    same character on a casing mismatch ("Mira" vs "mira") rather than disagreeing.
    """
    target = str(name).strip().lower()
    for canonical_name, entry in character_bible.items():
        if canonical_name.lower() == target:
            return canonical_name, entry
    return None


# Thread-local context for usage recording. run_job_async (one thread per job)
# sets job_id; generate_character_reference_image flips kind while its clip
# renders. This avoids threading a usage accumulator through every call layer.
_usage_ctx = threading.local()


def record_provider_usage(config, duration, task_id, status, usage):
    """Persist one Ark task's reported usage. Best-effort bookkeeping:
    never raises — a failed insert must not fail a render that already
    succeeded (or add noise to one that failed)."""
    if not usage and status != "succeeded":
        return  # terminal failure with no usage block: nothing billed to record
    try:
        conn = new_connection()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO provider_usage (
                job_id, kind, provider, model, task_id, status,
                duration_seconds, resolution, completion_tokens, total_tokens, created_at
            )
            VALUES (%s, %s, 'ark', %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                getattr(_usage_ctx, "job_id", None),
                getattr(_usage_ctx, "kind", "shot"),
                config["video_model"],
                task_id,
                status,
                duration,
                config.get("resolution"),
                (usage or {}).get("completion_tokens"),
                (usage or {}).get("total_tokens"),
                now_iso(),
            ),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def submit_and_poll_video_task(content, config, duration, timeout=None):
    """
    Low-level Ark video task submit + poll + download. Shared by call_video_model
    (per-shot rendering) and generate_character_reference_image (a short clip
    used purely as a source frame). This is the one request shape in this file
    that's actually proven against your account — it's the same code path that
    already renders real shots — so reusing it instead of inventing a second
    schema is the whole point.

    Returns (video_bytes, None) on success, or (None, reason_string) on failure.
    Does not write to disk — callers decide where the bytes go.
    """
    if not ARK_API_KEY:
        return None, "ARK_API_KEY not set"

    payload = {
        "model": config["video_model"],
        "content": content,
        "ratio": config["aspect_ratio"],
        "resolution": config["resolution"],
        "duration": duration,
        "generate_audio": False,
    }
    submit_req = urllib_request.Request(
        f"{ARK_API_BASE}/contents/generations/tasks",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {ARK_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib_request.urlopen(submit_req, timeout=30) as resp:
            submit_data = json.loads(resp.read().decode("utf-8"))
        task_id = submit_data.get("id") or submit_data.get("task_id")
        if not task_id:
            return None, f"No task_id in submit response: {submit_data}"
    except urllib_error.HTTPError as exc:
        # Include the response body: ModelArk 400s carry a structured error
        # with a `param` field (e.g. "content[1].image_url" when a reference
        # image couldn't be downloaded) that callers use to decide whether a
        # retry without image attachments is worth it.
        try:
            body = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            body = ""
        return None, f"Submit HTTP {exc.code}: {exc.reason} {body}".strip()
    except (urllib_error.URLError, json.JSONDecodeError) as exc:
        return None, f"Submit error: {exc}"

    poll_url = f"{ARK_API_BASE}/contents/generations/tasks/{task_id}"
    poll_headers = {"Authorization": f"Bearer {ARK_API_KEY}"}
    deadline = time.monotonic() + (timeout or SHOT_POLL_TIMEOUT)

    while time.monotonic() < deadline:
        time.sleep(SHOT_POLL_INTERVAL)
        try:
            poll_req = urllib_request.Request(poll_url, headers=poll_headers, method="GET")
            with urllib_request.urlopen(poll_req, timeout=15) as resp:
                poll_data = json.loads(resp.read().decode("utf-8"))
        except (urllib_error.HTTPError, urllib_error.URLError, json.JSONDecodeError):
            continue  # transient; keep polling

        status = (poll_data.get("status") or "").lower()

        if status in ("succeeded", "completed"):
            # Record before the download attempt: tokens were spent even if
            # fetching the finished video subsequently fails.
            record_provider_usage(config, duration, task_id, status, poll_data.get("usage"))
            video_url = (
                poll_data.get("content", {}).get("video_url")
                or poll_data.get("output", {}).get("video_url")
                or poll_data.get("video_url")
            )
            if not video_url:
                return None, "succeeded but no video_url in response"
            try:
                dl_req = urllib_request.Request(video_url, method="GET")
                with urllib_request.urlopen(dl_req, timeout=120) as dl_resp:
                    return dl_resp.read(), None
            except (urllib_error.URLError, OSError) as exc:
                return None, f"Download failed: {exc}"

        elif status in ("failed", "cancelled", "expired", "error"):
            # Failed tasks sometimes still report usage (partial billing);
            # record_provider_usage skips the insert when there's none.
            record_provider_usage(config, duration, task_id, status, poll_data.get("usage"))
            err = poll_data.get("error") or {}
            return None, f"Task {status}: {err.get('message', 'no details')}"

        # still queued/running — keep polling

    return None, f"Timed out after {timeout or SHOT_POLL_TIMEOUT}s waiting for task {task_id}"


REFERENCE_CLIP_SEEK = "1.5"   # seconds into the clip to grab a frame: past any startup motion, before any tail movement


def generate_character_reference_image(job_id, name, description, config):
    """
    Generate one canonical reference portrait for a character by rendering a
    short, mostly-static clip with the job's own Seedance video_model and
    extracting a single frame from it with ffmpeg.

    This is NOT a call to a separate image-generation endpoint. Seedance is a
    video model — it doesn't do text-to-image — so there is no "use the same
    Seedance model" path through an /images/generations-style endpoint; that
    endpoint needs an actual image model id, which this deployment has none of.
    Instead this renders a minimal clip through submit_and_poll_video_task —
    the same request shape already proven by the real shot-rendering pipeline —
    and pulls a frame out of it. No unverified API schema involved.

    Trade-off, stated plainly: this costs more than a real image-generation call
    would (you're paying for REFERENCE_CLIP_DURATION seconds of video generation
    per character, not one image call), and it's slower (video submit+poll, not
    a synchronous image response). It is still NOT counted in estimate_job_cost —
    the billing gap flagged earlier, now slightly larger per named character.

    Why re-host the extracted frame under our own /character-refs/ route rather
    than just keeping the source clip around: a multi-shot job can run long
    enough (each shot polls for up to SHOT_POLL_TIMEOUT seconds) that anything
    Ark-hosted with a TTL could expire mid-job; re-hosting locally removes that
    risk entirely, and it's the same reasoning as the original image-endpoint
    version of this function had, just applied to a frame instead of an image.

    Best-effort: returns a public https URL on success, or None on ANY failure
    (missing config, generation failure, ffmpeg missing or failing). A None
    result means that character renders with text-only consistency for this
    job — it never blocks or fails the job itself.
    """
    if not ARK_API_KEY or not PUBLIC_BASE_URL:
        return None

    reference_prompt = (
        "Character reference shot. Single subject standing still, facing the "
        "camera, centered in frame, full figure visible, neutral plain "
        "background, soft even studio lighting, minimal motion. "
        f"{description}. Static locked-off camera, no camera movement."
    )
    content = [{"type": "text", "text": reference_prompt}]

    _usage_ctx.kind = "character_ref"
    try:
        video_bytes, _reason = submit_and_poll_video_task(content, config, REFERENCE_CLIP_DURATION)
    finally:
        _usage_ctx.kind = "shot"
    if video_bytes is None:
        return None

    suffix = uuid.uuid4().hex[:8]
    safe_name = secure_filename(name) or "character"
    clip_path = CHARACTER_DIR / f"{job_id}-{safe_name}-{suffix}-ref-clip.mp4"
    frame_path = CHARACTER_DIR / f"{job_id}-{safe_name}-{suffix}.png"
    try:
        clip_path.write_bytes(video_bytes)
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-ss", REFERENCE_CLIP_SEEK,
                "-i", str(clip_path),
                "-frames:v", "1",
                "-q:v", "2",
                str(frame_path),
            ],
            check=True, capture_output=True,
        )
        return f"{PUBLIC_BASE_URL}/character-refs/{frame_path.name}"
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None
    finally:
        clip_path.unlink(missing_ok=True)


def populate_character_reference_images(job_id, character_bible, config):
    """
    Generate and attach a reference image for every character in the bible.
    Mutates character_bible's entries in place (same dict object the caller
    already holds — e.g. planner["characters"] — so callers that captured a
    reference to it before this runs will see the URLs appear without needing
    it passed back).

    Call this ONLY after confirming the job's real cost — which already
    includes REFERENCE_CLIP_DURATION seconds of generation per character here
    — is something the user can actually afford. Generating images before that
    check, as an earlier version of this pipeline did, spends real render
    money on jobs that then fail the balance check anyway, which is strictly
    worse than just failing the balance check first.
    """
    for name, entry in character_bible.items():
        entry["reference_image_url"] = generate_character_reference_image(
            job_id, name, entry["description"], config
        )
        time.sleep(1)  # courtesy spacing between consecutive ARK calls
    return character_bible


def lock_characters_into_prompt(base_prompt, characters_in_shot, character_bible, consistency_strength):
    """
    Deterministically prepend each character's canonical description onto a shot
    prompt, in code, every single time. This is the actual fix: we do not trust
    the planner LLM to repeat itself with identical wording across N independent
    JSON list items — it won't, reliably, especially on longer storyboards. By
    enforcing it here, shot 1 and shot 12 get byte-identical character wording
    even if the model's own phrasing drifted.

    consistency_strength >= 0.5 (also the config default of 0.8): full verbatim
      description repeated every shot. Use this whenever the same character must
      look the same in every cut — which, per the request that built this, is the
      whole point.
    consistency_strength < 0.5: only the first clause of the description (treated
      as a short identity anchor) is repeated, trading strict consistency for more
      prompt variety shot to shot. Still always uses the same anchor wording.

    Matching is case-insensitive against the bible (via _find_character), because
    planner output drifts on casing ("Mira" vs "mira") far more often than it
    drops a character outright.
    """
    if not characters_in_shot:
        return base_prompt

    blocks = []
    for raw_name in characters_in_shot:
        match = _find_character(raw_name, character_bible)
        if not match:
            # Planner referenced a character it never defined in the bible.
            # Don't fail the whole job over it — render the shot without a lock
            # rather than crash, but this should show up in shot_log for review.
            continue
        name, entry = match
        description = entry["description"]
        if consistency_strength >= 0.5:
            blocks.append(f"{name} ({description})")
        else:
            anchor = description.split(".")[0].strip()
            blocks.append(f"{name} ({anchor})")

    if not blocks:
        return base_prompt

    character_clause = "Characters, rendered identically to every other shot: " + "; ".join(blocks) + "."
    return f"{character_clause} {base_prompt}".strip()


def call_planner(storyboard_text, config):
    """
    Call ModelArk (BytePlus Ark) chat completions to decompose a prose storyboard
    into a list of structured shots with durations that sum to target_duration.

    Reality check on character consistency: Seedance shots are independent API
    calls — the model has no memory of any other shot (see the system prompt
    below). So the planner does two things instead of one:
      1. Builds a "character bible" — one exhaustive, named description per
         character, written once. reference_image_url starts as None here on
         purpose: generating that image costs real money (see
         generate_character_reference_image), and this function has no idea
         yet whether the caller can actually afford the job once that cost is
         included. Populating it is run_job_async's job, after it has
         confirmed the real cost against the user's balance — see
         populate_character_reference_images.
      2. Tags which characters appear in each shot, then `lock_characters_into_prompt`
         deterministically re-injects the exact description into every shot's
         prompt in code, not just via LLM instruction-following. This part
         only needs the text description, so it doesn't have to wait for step 1
         of run_job_async's post-planning sequence.
    Asking the model nicely to "stay consistent" (the original behavior) does
    not survive a 10-shot JSON completion in practice; forcing identical
    wording in code does much better, and combining it with a real reference
    image (once generated) better still.

    Returns a dict:
      {
        "status": "ok" | "skipped" | "failed",
        "message": str,
        "characters": {
            name: {"description": str, "reference_image_url": None}, ...
        },
        "shots": [
          {
            "index": int,                  # 1-based
            "duration": int,                # 4-15s (Seedance constraint)
            "characters_in_shot": [str, ...],
            "prompt": str,                  # visual prompt, character block already locked in
            "camera": str,                  # camera move / framing note
            "narration": str                # VO line or "" if none
          },
          ...
        ],
        "plan_text": str           # raw JSON string for the plan file
      }
    """
    if not ARK_API_KEY:
        return {
            "status": "skipped",
            "message": "ARK_API_KEY not configured.",
            "characters": {},
            "shots": [],
            "plan_text": "",
        }

    # Decide reasonable shot count from target duration.
    # Seedance supports clips from 4-15 s. We prefer ~5 s clips for tighter control,
    # allowing longer only when the planner decides a scene needs more breathing room.
    target = config["target_duration"]
    max_shots = max(1, target // 5)   # upper bound: one 5-second clip per slot

    system_prompt = textwrap.dedent("""
        You are a professional video production planner working with a text-to-video
        model that has NO memory between shots — every shot is generated from scratch,
        independently, with no awareness of any other shot's output.

        Step 1 — Build a character bible.
        Identify every named or recurring character in the storyboard. For each one,
        write ONE exhaustive physical description: age, build, hair, face, exact
        clothing (including color), and any distinguishing props or features. Avoid
        vague terms like "nice dress" — be specific enough that two different artists
        reading only this description would draw the same person.

        Step 2 — Break the story into shots.
        Each shot must have a duration between 4 and 15 seconds (integers only).
        The total of all shot durations must not exceed the target duration. For each
        shot, list which characters (by the exact same name used in the bible) appear
        in it. Do NOT redescribe a character's physical appearance inside the shot's
        `prompt` field — that gets attached automatically from the bible. The `prompt`
        field should only describe setting, lighting, action, and composition.

        Return ONLY valid JSON — no markdown fences, no commentary.
        The JSON must match this schema exactly:
        {
          "characters": [
            {"name": "<exact name, reused consistently>", "description": "<full canonical visual description>"}
          ],
          "shots": [
            {
              "index": <integer, 1-based>,
              "duration": <integer, 4-15>,
              "characters_in_shot": ["<exact name from characters[]>", ...],
              "prompt": "<setting, lighting, and action only — no character physical description>",
              "camera": "<camera movement and framing note>",
              "narration": "<voice-over line, or empty string if none>"
            }
          ],
          "music_prompt": "<one short instrumental music description for the WHOLE video (mood, instruments, tempo — e.g. 'sparse piano and strings, melancholic, slow tempo'), no lyrics, max 300 characters>"
        }
        Rules:
        - Every name in characters_in_shot must exactly match a name in characters[].
        - Keep narration lines short enough to fit the shot duration at normal speaking pace.
        - Do not reference shot numbers or metadata inside the prompt field.
    """).strip()

    user_prompt = textwrap.dedent(f"""
        Style preset: {config["style_preset"]}
        Target duration: {target} seconds (max {max_shots} shots)
        Aspect ratio: {config["aspect_ratio"]}
        Narration enabled: {config["narration_enabled"]}
        Consistency strength: {config["consistency_strength"]}

        Storyboard (prose):
        {storyboard_text[:12000]}
    """).strip()

    payload = {
        "model": config["planner_model"],
        "temperature": config["planner_temperature"],
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    body = json.dumps(payload).encode("utf-8")
    req = urllib_request.Request(
        f"{ARK_API_BASE}/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {ARK_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib_request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        raw_json = data["choices"][0]["message"]["content"].strip()
        parsed = json.loads(raw_json)

        character_bible = {}
        for c in parsed.get("characters", []):
            name = str(c.get("name", "")).strip()
            desc = str(c.get("description", "")).strip()
            if not (name and desc):
                continue
            character_bible[name] = {
                "description": desc,
                # Populated later by populate_character_reference_images, and
                # only AFTER run_job_async has confirmed the real cost (which
                # includes this generation) is something the user can actually
                # afford. Generating it here, before that check, would spend
                # real render money on jobs that fail the balance check anyway.
                "reference_image_url": None,
            }

        shots = parsed.get("shots", [])
        if not shots:
            raise ValueError("Planner returned zero shots.")

        # Validate, clamp, and lock the character bible into each shot's prompt.
        # Locking only needs the text description, not the (not-yet-generated)
        # image, so this doesn't need to wait for populate_character_reference_images.
        clean_shots = []
        for i, s in enumerate(shots):
            duration = int(s.get("duration", 5))
            duration = max(4, min(15, duration))  # Seedance supports 4–15 s
            characters_in_shot = [
                str(n).strip() for n in s.get("characters_in_shot", []) if str(n).strip()
            ]
            base_prompt = str(s.get("prompt", "")).strip()
            locked_prompt = lock_characters_into_prompt(
                base_prompt,
                characters_in_shot,
                character_bible,
                config["consistency_strength"],
            )
            clean_shots.append({
                "index": i + 1,
                "duration": duration,
                "characters_in_shot": characters_in_shot,
                "prompt": locked_prompt,
                "camera": str(s.get("camera", "")).strip(),
                "narration": str(s.get("narration", "")).strip(),
            })
        return {
            "status": "ok",
            "message": (
                f"Planner produced {len(clean_shots)} shots via ModelArk "
                f"({len(character_bible)} character(s) identified for consistency "
                f"locking; reference images generated separately once cost is confirmed)."
            ),
            "characters": character_bible,
            "shots": clean_shots,
            "music_prompt": str(parsed.get("music_prompt", "")).strip()[:450],
            "plan_text": json.dumps(
                {
                    "characters": character_bible,
                    "shots": clean_shots,
                    "music_prompt": str(parsed.get("music_prompt", "")).strip()[:450],
                },
                indent=2,
            ),
        }
    except (urllib_error.HTTPError, urllib_error.URLError) as exc:
        return {
            "status": "failed",
            "message": f"ModelArk request failed: {exc}",
            "characters": {},
            "shots": [],
            "plan_text": "",
        }
    except (KeyError, IndexError, json.JSONDecodeError, ValueError) as exc:
        return {
            "status": "failed",
            "message": f"Planner response parse error: {exc}",
            "characters": {},
            "shots": [],
            "plan_text": "",
        }


def serialize_job(row):
    return {
        "id": row["id"],
        "title": row["title"],
        "status": row["status"],
        "original_filename": row["original_filename"],
        "planner_model": row["planner_model"],
        "video_model": row["video_model"],
        "provider": row["provider"],
        "estimated_credits": row["estimated_credits"],
        "output_kind": row["output_kind"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "download_url": f"/api/jobs/{row['id']}/download",
        "config": json.loads(row["params_json"]),
    }


def run_job_async(job_id, user_id, config, settings, storyboard_text, storyboard_path, estimated_credits):
    """Runs in a background thread. Opens its own DB connection.

    `estimated_credits` arrives as the pre-planning guess (assumption-based
    character count — see estimate_job_cost). It gets reassigned below, once
    the planner has actually run, to the real cost based on the real character
    count. charge_credits() reads this name from its enclosing scope at call
    time, so it always charges whatever this was last set to — the recomputed
    real number, not the original guess — without needing `nonlocal`.
    """
    db = new_connection()
    # Tag this worker thread so every Ark task it submits (shots and
    # character-reference clips alike) records usage against this job.
    _usage_ctx.job_id = job_id
    _usage_ctx.kind = "shot"

    def set_status(status, output_path=None, output_kind=None, plan_path=None,
                    extra_params=None, new_estimated_credits=None):
        cur = db.cursor()
        updates = ["status = %s", "updated_at = %s"]
        values = [status, now_iso()]
        if output_path:
            updates += ["output_path = %s", "output_kind = %s"]
            values += [str(output_path), output_kind]
        if plan_path:
            updates += ["render_plan_path = %s"]
            values += [str(plan_path)]
        if new_estimated_credits is not None:
            updates += ["estimated_credits = %s"]
            values += [new_estimated_credits]
        values.append(job_id)
        cur.execute(f"UPDATE jobs SET {', '.join(updates)} WHERE id = %s", values)
        if extra_params:
            cur.execute("SELECT params_json FROM jobs WHERE id = %s", (job_id,))
            row = cur.fetchone()
            params = json.loads(row["params_json"])
            params.update(extra_params)
            cur.execute(
                "UPDATE jobs SET params_json = %s WHERE id = %s",
                (json.dumps(params), job_id),
            )
        db.commit()

    def charge_credits():
        """
        Deduct credits atomically. Uses UPDATE ... WHERE credit_balance >= cost
        so a concurrent job cannot overdraw the account (fixes TOCTOU).
        Raises RuntimeError if balance is now insufficient (race lost).
        """
        cur = db.cursor()
        cur.execute(
            """
            UPDATE users
            SET credit_balance = ROUND(CAST(credit_balance - %s AS numeric), 1)
            WHERE id = %s AND credit_balance >= %s
            """,
            (estimated_credits, user_id, estimated_credits),
        )
        if cur.rowcount == 0:
            db.rollback()
            raise RuntimeError("Insufficient credits at charge time (concurrent job may have raced).")
        cur.execute(
            """
            INSERT INTO credit_ledger (user_id, delta, reason, note, actor_user_id, created_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (user_id, -round(estimated_credits, 1), "job_charge",
             f"Charged for job {job_id}", user_id, now_iso()),
        )
        db.commit()

    try:
        # ── Phase 1: Planning ────────────────────────────────────────────────
        set_status("planning")
        planner = call_planner(storyboard_text, config)

        plan_path = OUTPUT_DIR / f"{job_id}-plan.json"
        plan_path.write_text(
            planner["plan_text"] or json.dumps({"error": planner["message"]}),
            encoding="utf-8",
        )
        set_status(
            "rendering",
            plan_path=plan_path,
            extra_params={
                "planner_status": planner["status"],
                "planner_message": planner["message"],
                "shot_count": len(planner["shots"]),
                "character_count": len(planner.get("characters", {})),
                # No reference images yet at this point — see the note on
                # populate_character_reference_images below for why. The
                # character_bible entry here gets overwritten once images are
                # actually populated (or once we know we're not generating any,
                # e.g. PUBLIC_BASE_URL unset).
                "character_bible": planner.get("characters", {}),
            },
        )

        # ── Phase 1.5: True up the cost now that the real character count is
        # known, and re-check the balance BEFORE spending any render money. ──
        # The pre-planning estimate this job was created with used an
        # admin-tunable guess (settings["quote_character_assumption"]); now
        # that call_planner has actually run, charge what was actually going
        # to happen instead. This also correctly charges LESS than the
        # original guess when the planner skipped/failed (zero characters
        # extracted, zero reference renders incurred) — recomputing here isn't
        # one-directional, it's just accurate.
        character_count = len(planner.get("characters", {}))
        estimated_credits, _provider = estimate_job_cost(config, settings, character_count=character_count)

        cur = db.cursor()
        cur.execute("SELECT credit_balance FROM users WHERE id = %s", (user_id,))
        current_balance = float(cur.fetchone()["credit_balance"])
        if current_balance < estimated_credits:
            raise RuntimeError(
                f"Insufficient credits once the real character count was known "
                f"({character_count} character(s) -> {estimated_credits} credits "
                f"needed, {round(current_balance, 1)} available). No shots were "
                f"rendered, no reference images were generated — failing before "
                f"any render spend, not after."
            )

        set_status(
            "rendering",
            extra_params={"actual_estimated_credits": estimated_credits},
            new_estimated_credits=estimated_credits,
        )

        # Only now — after confirming the user can actually afford the real
        # cost — do we spend money generating reference images. This ordering
        # is the whole point: an earlier version of this pipeline generated
        # images inside call_planner, before any balance recheck, which meant
        # a job could spend on N character renders and still fail the balance
        # check immediately afterward. populate_character_reference_images
        # mutates planner["characters"] in place, so render_shots below (which
        # reads that same dict) sees the populated URLs automatically.
        if planner.get("characters"):
            populate_character_reference_images(job_id, planner["characters"], config)
            set_status("rendering", extra_params={"character_bible": planner["characters"]})

        # ── Phase 2 & 3: Render shots then stitch ───────────────────────────
        # If planner succeeded and we have real shots, attempt real rendering.
        # If planner failed/skipped, fall through to placeholder immediately.
        output_kind = "mp4"
        output_path = OUTPUT_DIR / f"{job_id}.mp4"
        rendered_ok = False

        if planner["status"] == "ok" and planner["shots"]:
            clips, shot_log = render_shots(
                job_id, planner["shots"], config, planner.get("characters", {})
            )
            set_status("rendering", extra_params={"shot_log": shot_log})
            if clips:
                stitch_ok = stitch_shots(clips, output_path, config)
                if stitch_ok:
                    rendered_ok = True
                    # Clean up individual shot clips — final MP4 is all we need
                    for clip_path, _ in clips:
                        clip_path.unlink(missing_ok=True)

        if not rendered_ok:
            # Fallback: placeholder video (green screen + tone) or zip bundle
            try:
                generate_placeholder_video(output_path, config["title"], job_id, config, storyboard_text)
            except (subprocess.CalledProcessError, FileNotFoundError):
                output_kind = "zip"
                output_path = OUTPUT_DIR / f"{job_id}.zip"
                generate_placeholder_bundle(
                    output_path, config["title"], job_id, config, storyboard_text, planner
                )

        # ── Phase 3.5: Background music (best-effort) ────────────────────────
        # Only on a real render — placeholders don't get scored. Any failure
        # here ships the video without music AND refunds the flat music fee:
        # the fee is only kept when music actually made it into the file.
        if config.get("music_enabled"):
            music_note = "skipped: render fell back to placeholder"
            if rendered_ok:
                set_status("rendering", extra_params={"music_status": "generating"})
                music_prompt = config.get("music_prompt") or planner.get("music_prompt") or ""
                music_path, music_err = generate_background_music(job_id, music_prompt)
                if music_path:
                    mixed, mix_note = mix_music_into_video(output_path, music_path)
                    music_note = "ok" if mixed else mix_note
                    music_path.unlink(missing_ok=True)
                else:
                    music_note = music_err
            if music_note != "ok":
                estimated_credits = round(max(0.0, estimated_credits - MUSIC_FLAT_CREDITS), 1)
                set_status("rendering", new_estimated_credits=estimated_credits)
            set_status("rendering", extra_params={"music_status": music_note})

        # ── Phase 4: Charge credits only on success ──────────────────────────
        charge_credits()
        set_status("completed", output_path=output_path, output_kind=output_kind)

    except RuntimeError as exc:
        # Credit race or explicit business logic failure
        set_status("failed", extra_params={"error": str(exc)})
    except Exception as exc:
        set_status("failed", extra_params={"error": str(exc)})
    finally:
        _usage_ctx.job_id = None
        db.close()


def _is_image_url_400(reason):
    """True if a submit failure string is a 400 blaming an image_url entry.

    Matches the error shape observed live: HTTP 400 with
    param "content[N].image_url" / "resource download failed".
    """
    if not reason or "400" not in reason:
        return False
    return "image_url" in reason


def call_video_model(shot, config, clip_path, character_bible):
    """
    Submit one shot to Seedance 2.0, poll until done, download clip.
    Returns (True, "ok") on success, (False, reason_string) on any failure.

    Thin wrapper around submit_and_poll_video_task — the submit/poll/download
    plumbing itself lives there now so generate_character_reference_image can
    reuse it instead of duplicating it.

    character_bible supplies reference_image_url per character (see
    generate_character_reference_image). For each character tagged in
    shot["characters_in_shot"] that has a reference image, we attach it to the
    request's `content` array as an image_url entry alongside the text prompt —
    same pattern this codebase already uses for text content. Capped at 9
    images, matching Seedance 2.0's published reference-image limit.

    The image_url content type was verified against a live ModelArk call on
    2026-07-23: the schema is accepted and the output genuinely conditions on
    the reference image. One failure mode discovered in that test: ModelArk's
    servers download each image_url themselves at submit time, and if that
    download fails (host blocks their fetcher, LINYAN_PUBLIC_BASE_URL
    misconfigured/unreachable, stale URL) the API 400s with param
    "content[N].image_url" — killing the whole shot, not just the image. So on
    an image-related 400 we retry once with text-only content: character
    consistency degrades to text-locking for that shot instead of losing the
    shot entirely.
    """
    prompt_parts = [shot["prompt"]]
    if shot.get("camera"):
        prompt_parts.append(shot["camera"])
    full_prompt = ". ".join(p.strip().rstrip(".") for p in prompt_parts if p.strip())

    duration = max(4, min(15, int(shot.get("duration", 5))))

    content = [{"type": "text", "text": full_prompt}]
    attached_urls = set()
    for char_name in shot.get("characters_in_shot", []):
        if len(attached_urls) >= 9:
            break
        match = _find_character(char_name, character_bible)
        if not match:
            continue
        _, entry = match
        ref_url = entry.get("reference_image_url")
        if ref_url and ref_url not in attached_urls:
            content.append({"type": "image_url", "image_url": {"url": ref_url}})
            attached_urls.add(ref_url)

    video_bytes, reason = submit_and_poll_video_task(content, config, duration)
    if video_bytes is None and attached_urls and _is_image_url_400(reason):
        # A reference image couldn't be fetched by ModelArk — drop the images
        # and retry once so the shot still renders with text-only consistency.
        video_bytes, retry_reason = submit_and_poll_video_task(
            [content[0]], config, duration
        )
        if video_bytes is None:
            return False, f"{retry_reason} (after image_url retry; original: {reason})"
    if video_bytes is None:
        return False, reason
    try:
        clip_path.write_bytes(video_bytes)
    except OSError as exc:
        return False, f"Write failed: {exc}"
    return True, "ok"


def render_shots(job_id, shots, config, character_bible):
    """
    Render each shot via Seedance 2.0 on ModelArk.
    Processes shots sequentially with a 1-second gap to respect QPS=2.

    character_bible (from call_planner) is passed through to call_video_model so
    each shot can attach reference images for the characters it contains.

    Returns a tuple: (clips, shot_log)
      clips    — list of (Path, duration) for successfully rendered shots, in order
      shot_log — list of dicts with per-shot outcome for params_json storage

    If zero shots succeed, clips is [].
    If some shots fail/timeout, we stitch what we have and record the failures.
    """
    clips = []
    shot_log = []

    for shot in shots:
        clip_path = OUTPUT_DIR / f"{job_id}-shot-{shot['index']:03d}.mp4"
        ok, reason = call_video_model(shot, config, clip_path, character_bible)
        entry = {
            "index": shot["index"],
            "duration": shot["duration"],
            "status": "ok" if ok else "failed",
            "reason": reason,
        }
        shot_log.append(entry)
        if ok:
            clips.append((clip_path, shot["duration"]))
        else:
            # Log the failure but keep going — partial output is better than nothing
            pass
        time.sleep(1)  # respect QPS=2

    return clips, shot_log


def stitch_shots(shot_clips, output_path, config):
    """
    Concatenate shot clips into a single MP4 using ffmpeg concat demuxer.
    shot_clips: list of (Path, duration_seconds) tuples.
    Returns True on success, False on failure.
    """
    if not shot_clips:
        return False
    concat_list_path = output_path.parent / f"{output_path.stem}-concat.txt"
    try:
        lines = [f"file '{clip_path.resolve()}'\n" for clip_path, _ in shot_clips]
        concat_list_path.write_text("".join(lines), encoding="utf-8")
        cmd = [
            "ffmpeg", "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat_list_path),
            "-c", "copy",
            "-movflags", "+faststart",
            str(output_path),
        ]
        subprocess.run(cmd, check=True, capture_output=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False
    finally:
        if concat_list_path.exists():
            concat_list_path.unlink(missing_ok=True)


def generate_background_music(job_id, music_prompt):
    """
    Generate one instrumental track via kie.ai's Suno wrapper.
    Returns (mp3_path, None) on success, (None, reason) on any failure.
    Best-effort by design: a music failure must never fail the job — the
    caller ships the video without music and refunds the flat music fee.

    Request/poll/download shape verified live 2026-07-23:
      - callBackUrl is REQUIRED at submit even though we poll; a URL that
        does nothing useful is fine (SUCCESS was reached with one).
      - Poll statuses: PENDING → TEXT_SUCCESS → FIRST_SUCCESS → SUCCESS.
      - The audio CDN 403s Python's default User-Agent; send a browser UA.
    """
    if not KIE_API_KEY:
        return None, "KIE_API_KEY not set"

    prompt = (music_prompt or "").strip() or "cinematic instrumental underscore, gentle, atmospheric"
    if "instrumental" not in prompt.lower():
        prompt += ", instrumental, no lyrics"
    callback = (
        f"{PUBLIC_BASE_URL}/api/music/callback"
        if PUBLIC_BASE_URL
        else "https://example.com/api/music/callback"  # required field; polling is the source of truth
    )
    payload = {
        "prompt": prompt[:500],
        "customMode": False,
        "instrumental": True,
        "model": "V5",
        "callBackUrl": callback,
    }
    headers = {"Authorization": f"Bearer {KIE_API_KEY}", "Content-Type": "application/json"}

    try:
        req = urllib_request.Request(
            f"{KIE_API_BASE}/generate", data=json.dumps(payload).encode("utf-8"),
            headers=headers, method="POST",
        )
        with urllib_request.urlopen(req, timeout=30) as resp:
            submit = json.loads(resp.read().decode("utf-8"))
        if submit.get("code") != 200:
            return None, f"kie.ai submit rejected: {submit.get('msg')}"
        task_id = submit["data"]["taskId"]
    except Exception as exc:
        return None, f"kie.ai submit error: {exc}"

    deadline = time.monotonic() + MUSIC_POLL_TIMEOUT
    status = None
    while time.monotonic() < deadline:
        time.sleep(MUSIC_POLL_INTERVAL)
        try:
            poll = urllib_request.Request(
                f"{KIE_API_BASE}/generate/record-info?taskId={task_id}", headers=headers
            )
            with urllib_request.urlopen(poll, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception:
            continue  # transient; keep polling
        info = data.get("data") or {}
        status = info.get("status")
        tracks = ((info.get("response") or {}).get("sunoData")) or []
        # CALLBACK_EXCEPTION means their POST to our callback failed — the
        # tracks themselves may still exist, so treat it as terminal-with-hope.
        if status == "SUCCESS" or (status == "CALLBACK_EXCEPTION" and tracks):
            if not tracks:
                return None, "SUCCESS but no tracks in response"
            record_music_usage(job_id, task_id, status, tracks[0])
            try:
                dl = urllib_request.Request(
                    tracks[0]["audioUrl"], headers={"User-Agent": MUSIC_DL_USER_AGENT}
                )
                with urllib_request.urlopen(dl, timeout=120) as resp:
                    mp3_path = OUTPUT_DIR / f"{job_id}-music.mp3"
                    mp3_path.write_bytes(resp.read())
                return mp3_path, None
            except Exception as exc:
                return None, f"music download failed: {exc}"
        if status in ("CREATE_TASK_FAILED", "GENERATE_AUDIO_FAILED", "SENSITIVE_WORD_ERROR",
                      "CALLBACK_EXCEPTION"):
            record_music_usage(job_id, task_id, status, None)
            return None, f"kie.ai task {status}: {info.get('errorMessage') or 'no details'}"
    return None, f"music generation timed out after {MUSIC_POLL_TIMEOUT}s (status: {status})"


def record_music_usage(job_id, task_id, status, track):
    """provider_usage row for a kie.ai music task (kind='music', provider='kie').
    Same best-effort contract as record_provider_usage: never raises."""
    try:
        conn = new_connection()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO provider_usage (
                job_id, kind, provider, model, task_id, status,
                duration_seconds, resolution, completion_tokens, total_tokens, created_at
            )
            VALUES (%s, 'music', 'kie', 'suno-V5', %s, %s, %s, NULL, NULL, NULL, %s)
            """,
            (job_id, task_id, status,
             int((track or {}).get("duration") or 0) or None, now_iso()),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def mix_music_into_video(video_path, music_path):
    """
    Mux/mix the music under the video, in place (temp file + atomic replace).
    Seedance clips are rendered with generate_audio=False, so the stitched
    video normally has NO audio stream — in that case the music becomes the
    only audio track. If an audio stream ever exists (future narration), the
    music is mixed underneath it at reduced volume instead.
    Returns (True, "ok") or (False, reason).
    """
    tmp_path = video_path.parent / f"{video_path.stem}-music-tmp.mp4"
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=index", "-of", "csv=p=0", str(video_path)],
            capture_output=True, text=True,
        )
        has_audio = bool(probe.stdout.strip())
        if has_audio:
            cmd = [
                "ffmpeg", "-y", "-i", str(video_path), "-i", str(music_path),
                "-filter_complex",
                "[1:a]volume=0.2[music];[0:a][music]amix=inputs=2:duration=first[a]",
                "-map", "0:v", "-map", "[a]",
                "-c:v", "copy", "-c:a", "aac", "-shortest", str(tmp_path),
            ]
        else:
            cmd = [
                "ffmpeg", "-y", "-i", str(video_path), "-i", str(music_path),
                "-filter_complex", "[1:a]volume=0.5[a]",
                "-map", "0:v", "-map", "[a]",
                "-c:v", "copy", "-c:a", "aac", "-shortest", str(tmp_path),
            ]
        subprocess.run(cmd, check=True, capture_output=True)
        os.replace(tmp_path, video_path)
        return True, "ok"
    except (subprocess.CalledProcessError, FileNotFoundError, OSError) as exc:
        tmp_path.unlink(missing_ok=True)
        return False, f"ffmpeg mix failed: {exc}"


def create_job_record(user_id, original_filename, config, storyboard_text):
    settings = load_settings()
    # Pre-planning gate: character count isn't known yet (that's an LLM step
    # that hasn't run), so this uses settings["quote_character_assumption"].
    # run_job_async re-checks against the REAL character count once the
    # planner has actually run, before any shots are rendered — see there.
    estimated_credits, provider = estimate_job_cost(config, settings)
    cur = db_execute("SELECT credit_balance FROM users WHERE id = %s", (user_id,))
    user = cur.fetchone()
    if user["credit_balance"] < estimated_credits:
        return None, {
            "error": "Insufficient credits.",
            "needed": estimated_credits,
            "balance": round(user["credit_balance"], 1),
        }
    job_id = uuid.uuid4().hex[:12]
    safe_name = secure_filename(Path(original_filename).stem) or "storyboard"
    storyboard_path = UPLOAD_DIR / f"{job_id}-{safe_name}.md"
    storyboard_path.write_text(storyboard_text, encoding="utf-8")

    timestamp = now_iso()
    db_execute(
        """
        INSERT INTO jobs (
            id, user_id, title, status, original_filename, storyboard_path, render_plan_path,
            output_path, output_kind, planner_model, video_model, provider, estimated_credits,
            params_json, storyboard_text, created_at, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            job_id, user_id, config["title"], "queued", original_filename,
            str(storyboard_path), None,
            str(OUTPUT_DIR / f"{job_id}.mp4"), "mp4",
            config["planner_model"], config["video_model"], provider, estimated_credits,
            json.dumps(config), storyboard_text, timestamp, timestamp,
        ),
        commit=True,
    )

    t = threading.Thread(
        target=run_job_async,
        args=(job_id, user_id, config, settings, storyboard_text, storyboard_path, estimated_credits),
        daemon=True,
    )
    t.start()

    cur = db_execute("SELECT * FROM jobs WHERE id = %s", (job_id,))
    row = cur.fetchone()
    return serialize_job(row), None


@app.route("/")
def index():
    return render_template("index.html")

@app.route("/privacy")
def privacy():
    return render_template("privacy.html")

@app.route("/terms")
def terms():
    return render_template("terms.html")

# def index():
    return render_template("index.html")


@app.route("/studio")
def studio():
    user = current_user()
    if not user:
        return redirect("/")
    return render_template("inner.html")


@app.route("/api/music/callback", methods=["POST"])
def music_callback():
    """kie.ai requires a callBackUrl at submit time even though this app polls
    for results. This endpoint just acknowledges the delivery — polling in
    generate_background_music is the source of truth. Unauthenticated by
    necessity (kie.ai's servers carry no session); it stores nothing."""
    return jsonify({"success": True})


@app.route("/settings")
def settings():
    return render_template("settings.html")


@app.route("/api/ledger")
@login_required
def ledger():
    cur = db_execute(
        """
        SELECT delta, reason, note, created_at
        FROM credit_ledger
        WHERE user_id = %s
        ORDER BY created_at DESC
        LIMIT 50
        """,
        (g.current_user["id"],),
    )
    rows = cur.fetchall()
    return jsonify({"entries": [dict(r) for r in rows]})


@app.route("/api/session")
def session_status():
    user = current_user()
    settings = load_settings()
    return jsonify(
        {
            "user": serialize_user(user) if user else None,
            "settings": {
                "allow_signup": bool(settings["allow_signup"]),
                "margin_multiplier": float(settings["margin_multiplier"]),
                "signup_bonus_credits": float(settings["signup_bonus_credits"]),
                "credit_label": settings["credit_label"],
                "default_planner_model": settings["default_planner_model"],
                "default_video_model": settings["default_video_model"],
                "quote_character_assumption": int(settings["quote_character_assumption"]),
            },
            "catalog": MODEL_CATALOG,
            "ark_ready": bool(ARK_API_KEY),
            "billing": {
                # Buy button shows only when there's a link to send people to.
                "payment_link_enabled": bool(STRIPE_PAYMENT_LINK),
                # Honest flag: link without webhook = payments that credit nothing.
                "webhook_configured": bool(STRIPE_WEBHOOK_SECRET),
                "credits_per_dollar": clamp_float(
                    settings.get("credits_per_dollar"), 100.0, 0.0, 10000.0
                ),
            },
        }
    )


@app.route("/api/auth/signup", methods=["POST"])
def signup():
    settings = load_settings()
    if not settings["allow_signup"]:
        return jsonify({"error": "Signups are disabled by the host."}), 403
    payload = request.get_json(silent=True) or {}
    email = (payload.get("email") or "").strip().lower()
    display_name = (payload.get("display_name") or "").strip()
    password = payload.get("password") or ""
    if not email or not display_name or len(password) < 8:
        return jsonify({"error": "Display name, email, and an 8+ character password are required."}), 400
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT id FROM users WHERE email = %s", (email,))
    existing = cur.fetchone()
    if existing:
        return jsonify({"error": "Email already registered."}), 409
    cur.execute("SELECT COUNT(*) AS count FROM users")
    user_count = cur.fetchone()["count"]
    is_admin = user_count == 0
    starting_credits = 500.0 if is_admin else float(settings["signup_bonus_credits"])
    cur.execute(
        """
        INSERT INTO users (email, password_hash, display_name, is_admin, credit_balance, created_at)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            email,
            generate_password_hash(password, method="pbkdf2:sha256"),
            display_name,
            is_admin,
            starting_credits,
            now_iso(),
        ),
    )
    user_id = cur.fetchone()["id"]
    db.commit()
    apply_credit_delta(
        user_id,
        0,
        "signup",
        "Initial balance assigned on signup.",
        actor_user_id=user_id,
    )
    session["user_id"] = user_id
    return jsonify({"success": True, "user": serialize_user(current_user())})


@app.route("/api/auth/login", methods=["POST"])
def login():
    payload = request.get_json(silent=True) or {}
    email = (payload.get("email") or "").strip().lower()
    password = payload.get("password") or ""
    cur = db_execute(
        "SELECT id, password_hash FROM users WHERE email = %s",
        (email,),
    )
    row = cur.fetchone()
    if not row or not check_password_hash(row["password_hash"], password):
        return jsonify({"error": "Invalid email or password."}), 401
    session["user_id"] = row["id"]
    return jsonify({"success": True, "user": serialize_user(current_user())})


@app.route("/api/auth/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"success": True})


# ── Stripe billing ───────────────────────────────────────────────────────────


@app.route("/billing/checkout")
@login_required
def billing_checkout():
    """Create a Stripe Checkout Session and redirect the user to it.

    Uses Stripe's Checkout Sessions API for full control over the payment flow.
    The session includes client_reference_id=<user_id> so the checkout.session.completed
    webhook can attribute the payment to this account.

    Success URL: https://linyan.io/checkout/success?session_id={CHECKOUT_SESSION_ID}
    Cancel URL:  https://linyan.io/checkout/cancel

    Note: Requires STRIPE_SECRET_KEY to be set in environment variables.
    """
    if not STRIPE_SECRET_KEY:
        return jsonify({"error": "Stripe payments are not configured on this host. Please contact support."}), 503

    try:
        import stripe
        stripe.api_key = STRIPE_SECRET_KEY

        # Create a new Checkout Session
        checkout_session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[
                {
                    "price_data": {
                        "currency": "usd",
                        "product_data": {
                            "name": "Linyan Credits",
                            "description": "Credits for AI video generation",
                        },
                        "unit_amount": 1000,  # $10.00 in cents
                    },
                    "quantity": 1,
                },
            ],
            mode="payment",
            client_reference_id=str(g.current_user["id"]),
            customer_email=g.current_user["email"],
            success_url="https://linyan.io/checkout/success?session_id={CHECKOUT_SESSION_ID}",
            cancel_url="https://linyan.io/checkout/cancel",
            metadata={
                "user_id": str(g.current_user["id"]),
                "email": g.current_user["email"],
            },
        )

        print(f"[CHECKOUT] Created session: {checkout_session.id} for user {g.current_user['id']}")
        return redirect(checkout_session.url)
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"[CHECKOUT] ERROR: {type(e).__name__}: {e}")
        return jsonify({"error": f"Failed to create checkout session: {type(e).__name__}: {e}"}), 500


@app.route("/checkout/success")
@login_required
def checkout_success():
    """Handle successful checkout completion."""
    session_id = request.args.get("session_id", "")
    print(f"[CHECKOUT SUCCESS] User {g.current_user['id']} returned from Stripe, session_id: {session_id}")
    return """
    <html>
    <head><title>Payment Successful</title></head>
    <body>
        <h1>Payment Successful!</h1>
        <p>Thank you for your payment. Your credits have been added to your account.</p>
        <p><a href="/studio">Return to Studio</a></p>
        <script>
            // Refresh the session to update credit balance
            fetch('/api/session', { credentials: 'include' })
                .then(r => r.json())
                .then(data => {
                    if (data.user) {
                        console.log('Session refreshed:', data.user);
                    }
                })
                .catch(err => console.error('Failed to refresh session:', err));
            // Auto-redirect after 3 seconds
            setTimeout(() => {
                window.location.href = '/studio';
            }, 3000);
        </script>
    </body>
    </html>
    """


@app.route("/checkout/cancel")
@login_required
def checkout_cancel():
    """Handle cancelled checkout."""
    print(f"[CHECKOUT CANCEL] User {g.current_user['id']} cancelled checkout")
    return """
    <html>
    <head><title>Payment Cancelled</title></head>
    <body>
        <h1>Payment Cancelled</h1>
        <p>Your payment was cancelled. No charges were made to your account.</p>
        <p><a href="/studio">Return to Studio</a></p>
        <script>
            // Auto-redirect after 3 seconds
            setTimeout(() => {
                window.location.href = '/studio';
            }, 3000);
        </script>
    </body>
    </html>
    """


def verify_stripe_signature(payload_bytes, sig_header):
    """Verify a Stripe-Signature header against the raw request body.

    Implements Stripe's documented scheme (HMAC-SHA256 over
    "<timestamp>.<payload>") directly so we don't need the stripe SDK as a
    dependency. Handles multiple v1 signatures (present during secret
    rotation) and enforces a replay-protection timestamp window.
    """
    if not sig_header:
        return False
    timestamp = None
    candidate_sigs = []
    for part in sig_header.split(","):
        key, _, value = part.strip().partition("=")
        if key == "t":
            try:
                timestamp = int(value)
            except ValueError:
                return False
        elif key == "v1":
            candidate_sigs.append(value)
    if timestamp is None or not candidate_sigs:
        return False
    if abs(time.time() - timestamp) > STRIPE_SIGNATURE_TOLERANCE:
        return False
    signed_payload = str(timestamp).encode("utf-8") + b"." + payload_bytes
    expected = hmac.new(
        STRIPE_WEBHOOK_SECRET.encode("utf-8"), signed_payload, hashlib.sha256
    ).hexdigest()
    return any(hmac.compare_digest(expected, sig) for sig in candidate_sigs)


def record_stripe_event(cur, event, obj, status, note, user_id=None, credits=None,
                        amount_cents=None, currency=None):
    cur.execute(
        """
        INSERT INTO stripe_events
            (event_id, event_type, object_id, user_id, amount_cents, currency,
             credits, status, note, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            event.get("id"),
            event.get("type"),
            obj.get("id"),
            user_id,
            amount_cents,
            currency,
            credits,
            status,
            note,
            now_iso(),
        ),
    )


@app.route("/api/stripe/webhook", methods=["POST"])
def stripe_webhook():
    """Receive Stripe events and convert paid checkouts into credits.

    Handles:
      - checkout.session.completed / checkout.session.async_payment_succeeded
        (one-time Payment Link purchases, and the first payment of a
        subscription-mode link)
      - invoice.payment_succeeded (subscription renewals; the initial
        subscription invoice is skipped because the checkout event above
        already credited it)

    Idempotency: event_id is the stripe_events primary key. Stripe retries
    deliveries until it gets a 2xx; a duplicate insert conflicts and we ack
    without crediting again. Claim + credit + ledger write happen in one
    transaction on one connection, so a crash can't leave a claimed-but-
    uncredited event.
    """
    # Debug logging
    print(f"[WEBHOOK] Received POST at /api/stripe/webhook")
    print(f"[WEBHOOK] Headers: {dict(request.headers)}")
    payload = request.get_data()
    print(f"[WEBHOOK] Raw payload: {payload.decode('utf-8')[:500]}...")

    if not STRIPE_WEBHOOK_SECRET:
        print("[WEBHOOK] ERROR: Stripe webhook secret not configured.")
        return jsonify({"error": "Stripe webhook secret not configured."}), 503

    if not verify_stripe_signature(payload, request.headers.get("Stripe-Signature")):
        print("[WEBHOOK] ERROR: Invalid signature.")
        return jsonify({"error": "Invalid signature."}), 400
    print("[WEBHOOK] Signature verified successfully.")

    try:
        event = json.loads(payload)
        print(f"[WEBHOOK] Event type: {event.get('type', 'unknown')}")
        print(f"[WEBHOOK] Event ID: {event.get('id', 'unknown')}")
    except json.JSONDecodeError as e:
        print(f"[WEBHOOK] ERROR: Malformed payload - {e}")
        return jsonify({"error": "Malformed payload."}), 400

    event_type = event.get("type", "")
    obj = (event.get("data") or {}).get("object") or {}

    checkout_events = (
        "checkout.session.completed",
        "checkout.session.async_payment_succeeded",
    )
    if event_type not in checkout_events + ("invoice.payment_succeeded",):
        print(f"[WEBHOOK] Ignoring event type: {event_type}")
        return jsonify({"received": True, "ignored": event_type})

    # Extract who-paid-what per event shape.
    user_id = None
    email = None
    if event_type in checkout_events:
        payment_status = obj.get("payment_status")
        print(f"[WEBHOOK] Checkout payment_status: {payment_status}")
        if payment_status != "paid":
            # Delayed payment methods complete the session before money moves;
            # the async_payment_succeeded event will follow if it clears.
            print("[WEBHOOK] Payment not yet paid, will retry.")
            return jsonify({"received": True, "pending": True})
        ref = obj.get("client_reference_id")
        if ref and str(ref).isdigit():
            user_id = int(ref)
            print(f"[WEBHOOK] User ID from client_reference_id: {user_id}")
        email = ((obj.get("customer_details") or {}).get("email")
                 or obj.get("customer_email") or "").strip().lower()
        print(f"[WEBHOOK] Customer email: {email}")
        amount_cents = obj.get("amount_total")
        currency = (obj.get("currency") or "").lower()
        print(f"[WEBHOOK] Amount: {amount_cents} {currency}")
    else:  # invoice.payment_succeeded
        if obj.get("billing_reason") == "subscription_create":
            # First invoice of a subscription — already credited via the
            # checkout.session.completed event for the same purchase.
            print("[WEBHOOK] Skipping subscription_create invoice (already credited).")
            return jsonify({"received": True, "skipped": "subscription_create"})
        email = (obj.get("customer_email") or "").strip().lower()
        amount_cents = obj.get("amount_paid")
        currency = (obj.get("currency") or "").lower()

    settings = load_settings()
    credits_per_dollar = clamp_float(settings.get("credits_per_dollar"), 100.0, 0.0, 10000.0)

    db = get_db()
    cur = db.cursor()
    try:
        # Idempotency claim — a retry of an already-processed event conflicts
        # here and gets acked without re-crediting.
        cur.execute("SELECT 1 FROM stripe_events WHERE event_id = %s", (event.get("id"),))
        if cur.fetchone():
            print("[WEBHOOK] Duplicate event, skipping.")
            db.rollback()
            return jsonify({"received": True, "duplicate": True})

        if not amount_cents or amount_cents <= 0:
            record_stripe_event(cur, event, obj, "skipped", "Zero or missing amount.",
                                amount_cents=amount_cents, currency=currency)
            db.commit()
            print("[WEBHOOK] Skipped - zero/missing amount.")
            return jsonify({"received": True, "skipped": "zero_amount"})

        if currency != "usd":
            # credits_per_dollar is a USD rate; auto-crediting other currencies
            # at that rate would misprice. Park it for manual resolution.
            record_stripe_event(cur, event, obj, "unmatched",
                                f"Non-USD currency '{currency}' — resolve manually.",
                                amount_cents=amount_cents, currency=currency)
            db.commit()
            print(f"[WEBHOOK] Unmatched - non-USD currency: {currency}")
            return jsonify({"received": True, "unmatched": "currency"})

        # Resolve the user: explicit client_reference_id first, email fallback.
        target = None
        if user_id is not None:
            cur.execute("SELECT id FROM users WHERE id = %s", (user_id,))
            target = cur.fetchone()
            print(f"[WEBHOOK] Found user by ID: {target}")
        if not target and email:
            cur.execute("SELECT id FROM users WHERE email = %s", (email,))
            target = cur.fetchone()
            print(f"[WEBHOOK] Found user by email: {target}")
        if not target:
            record_stripe_event(cur, event, obj, "unmatched",
                                f"No account matched (email: {email or 'none'}). "
                                "Top up manually from the admin panel.",
                                amount_cents=amount_cents, currency=currency)
            db.commit()
            print(f"[WEBHOOK] Unmatched - no account for email: {email}")
            return jsonify({"received": True, "unmatched": "no_account"})

        credits = round((amount_cents / 100.0) * credits_per_dollar, 1)
        print(f"[WEBHOOK] Crediting {credits} credits to user {target['id']}")
        record_stripe_event(cur, event, obj, "credited", email or "",
                            user_id=target["id"], credits=credits,
                            amount_cents=amount_cents, currency=currency)
        cur.execute(
            "UPDATE users SET credit_balance = ROUND(CAST(credit_balance + %s AS numeric), 1) WHERE id = %s",
            (credits, target["id"]),
        )
        cur.execute(
            """
            INSERT INTO credit_ledger (user_id, delta, reason, note, actor_user_id, created_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                target["id"],
                credits,
                "stripe_purchase",
                f"Stripe payment ${amount_cents / 100.0:.2f} ({obj.get('id') or event.get('id')})",
                None,
                now_iso(),
            ),
        )
        db.commit()
        print("[WEBHOOK] SUCCESS - credits added.")
    except Exception as e:
        db.rollback()
        print(f"[WEBHOOK] ERROR: {e}")
        raise
    return jsonify({"received": True, "credited": True})


@app.route("/api/quote", methods=["POST"])
def quote_job():
    settings = load_settings()
    payload = request.get_json(silent=True) or {}
    config = normalize_config(payload, settings)
    estimated_credits, provider = estimate_job_cost(config, settings)
    return jsonify(
        {
            "estimated_credits": estimated_credits,
            "provider": provider,
            "credit_label": settings["credit_label"],
            # character_count is unknown pre-planning (it's an LLM extraction
            # step); this quote assumes settings["quote_character_assumption"]
            # characters. The real charge, applied after planning actually
            # runs, can be higher or lower than this number.
            "assumed_character_count": clamp_int(
                settings.get("quote_character_assumption"), 2, 0, 20
            ),
        }
    )


@app.route("/api/jobs", methods=["GET"])
@login_required
def list_jobs():
    cur = db_execute(
        """
        SELECT *
        FROM jobs
        WHERE user_id = %s
        ORDER BY created_at DESC
        """,
        (g.current_user["id"],),
    )
    rows = cur.fetchall()
    return jsonify({"jobs": [serialize_job(row) for row in rows]})


@app.route("/api/jobs", methods=["POST"])
@login_required
def create_job():
    if "file" not in request.files:
        return jsonify({"error": "Storyboard markdown file is required."}), 400
    raw_config = request.form.get("config")
    if not raw_config:
        return jsonify({"error": "Missing config payload."}), 400
    storyboard_file = request.files["file"]
    if not storyboard_file.filename.lower().endswith(".md"):
        return jsonify({"error": "Only .md storyboard files are accepted."}), 400
    settings = load_settings()
    try:
        config = normalize_config(json.loads(raw_config), settings)
    except json.JSONDecodeError:
        return jsonify({"error": "Config must be valid JSON."}), 400
    try:
        storyboard_text = storyboard_file.read().decode("utf-8")
    except UnicodeDecodeError:
        return jsonify({"error": "Storyboard must be UTF-8 markdown."}), 400
    job, error_payload = create_job_record(
        g.current_user["id"],
        storyboard_file.filename,
        config,
        storyboard_text,
    )
    if error_payload:
        return jsonify(error_payload), 402
    refreshed = current_user()
    return jsonify(
        {
            "success": True,
            "job": job,
            "user": serialize_user(refreshed),
        }
    )


@app.route("/api/jobs/<job_id>/status")
@login_required
def job_status(job_id):
    cur = db_execute(
        "SELECT id, status, output_kind, updated_at FROM jobs WHERE id = %s AND user_id = %s",
        (job_id, g.current_user["id"]),
    )
    row = cur.fetchone()
    if not row:
        return jsonify({"error": "Job not found."}), 404
    return jsonify({
        "id": row["id"],
        "status": row["status"],
        "output_kind": row["output_kind"],
        "updated_at": row["updated_at"],
        "download_url": f"/api/jobs/{row['id']}/download" if row["status"] == "completed" else None,
    })


@app.route("/api/jobs/<job_id>/download")
@login_required
def download_job(job_id):
    cur = db_execute(
        "SELECT * FROM jobs WHERE id = %s AND user_id = %s",
        (job_id, g.current_user["id"]),
    )
    row = cur.fetchone()
    if not row:
        return jsonify({"error": "Job not found."}), 404
    path = Path(row["output_path"])
    if not path.exists():
        return jsonify({"error": "Artifact missing on disk."}), 404
    download_name = f"{secure_filename(row['title']) or 'linyan-render'}.{row['output_kind']}"
    return send_file(path, as_attachment=True, download_name=download_name)


@app.route("/api/jobs/<job_id>/source")
@login_required
def job_source(job_id):
    """
    Returns the original storyboard text and full config a job was created
    from, so the frontend can reload them into the render form for editing
    and resubmitting as a new job ("reprompt"). Available for a job in any
    status, including failed — reprompting from a failure is the most likely
    reason someone would use this. This doesn't persist anything new: the
    storyboard markdown was already written to disk at storyboard_path and
    the config was already stored in params_json at job creation time; this
    is just the first endpoint that actually exposes either of them.
    """
    cur = db_execute(
        "SELECT id, title, storyboard_path, storyboard_text, params_json FROM jobs WHERE id = %s AND user_id = %s",
        (job_id, g.current_user["id"]),
    )
    row = cur.fetchone()
    if not row:
        return jsonify({"error": "Job not found."}), 404
    storyboard_text = row.get("storyboard_text")
    if not storyboard_text:
        # Legacy job created before storyboard_text was stored in the DB —
        # fall back to the on-disk file, which survives only until a redeploy.
        path = Path(row["storyboard_path"])
        if not path.exists():
            return jsonify({"error": "Original storyboard file is missing on disk."}), 404
        try:
            storyboard_text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return jsonify({"error": "Could not read the original storyboard file."}), 500
    return jsonify({
        "id": row["id"],
        "title": row["title"],
        "config": json.loads(row["params_json"]),
        "storyboard_text": storyboard_text,
    })


@app.route("/api/admin/overview")
@admin_required
def admin_overview():
    cur = db_execute(
        """
        SELECT u.id, u.display_name, u.email, u.is_admin, u.credit_balance, u.created_at,
               COUNT(j.id) AS job_count
        FROM users u
        LEFT JOIN jobs j ON j.user_id = u.id
        GROUP BY u.id
        ORDER BY u.created_at ASC
        """
    )
    users = cur.fetchall()
    settings = load_settings()
    cur = db_execute(
        """
        SELECT j.*, u.display_name
        FROM jobs j
        JOIN users u ON u.id = j.user_id
        ORDER BY j.created_at DESC
        LIMIT 12
        """
    )
    recent_jobs = cur.fetchall()
    return jsonify(
        {
            "users": [
                {
                    "id": row["id"],
                    "display_name": row["display_name"],
                    "email": row["email"],
                    "is_admin": bool(row["is_admin"]),
                    "credit_balance": round(row["credit_balance"], 1),
                    "job_count": row["job_count"],
                    "created_at": row["created_at"],
                }
                for row in users
            ],
            "settings": {
                "allow_signup": bool(settings["allow_signup"]),
                "margin_multiplier": float(settings["margin_multiplier"]),
                "signup_bonus_credits": float(settings["signup_bonus_credits"]),
                "default_planner_model": settings["default_planner_model"],
                "default_video_model": settings["default_video_model"],
                "quote_character_assumption": int(settings["quote_character_assumption"]),
                "credits_per_dollar": clamp_float(
                    settings.get("credits_per_dollar"), 100.0, 0.0, 10000.0
                ),
            },
            "stripe": {
                "payment_link_enabled": bool(STRIPE_PAYMENT_LINK),
                "webhook_configured": bool(STRIPE_WEBHOOK_SECRET),
                "recent_events": stripe_recent_events(),
            },
            "recent_jobs": [
                {
                    "id": row["id"],
                    "title": row["title"],
                    "status": row["status"],
                    "display_name": row["display_name"],
                    "estimated_credits": row["estimated_credits"],
                    "created_at": row["created_at"],
                }
                for row in recent_jobs
            ],
        }
    )


def stripe_recent_events(limit=20):
    """Recent Stripe webhook outcomes for the admin panel — most importantly
    'unmatched' rows, which are real money received that credited nobody and
    need a manual top-up."""
    cur = db_execute(
        """
        SELECT event_id, event_type, object_id, user_id, amount_cents,
               currency, credits, status, note, created_at
        FROM stripe_events
        ORDER BY created_at DESC
        LIMIT %s
        """,
        (limit,),
    )
    return [dict(r) for r in cur.fetchall()]


@app.route("/api/admin/topups", methods=["POST"])
@admin_required
def admin_topup():
    payload = request.get_json(silent=True) or {}
    user_id = payload.get("user_id")
    delta = clamp_float(payload.get("delta"), 0.0, -10000.0, 10000.0)
    note = (payload.get("note") or "").strip() or "Admin adjustment"
    cur = db_execute("SELECT id FROM users WHERE id = %s", (user_id,))
    target = cur.fetchone()
    if not target:
        return jsonify({"error": "Target user not found."}), 404
    apply_credit_delta(
        target["id"],
        delta,
        "admin_adjustment",
        note,
        actor_user_id=g.current_user["id"],
    )
    cur = db_execute(
        "SELECT id, email, display_name, is_admin, credit_balance, created_at FROM users WHERE id = %s",
        (target["id"],),
    )
    updated = cur.fetchone()
    return jsonify({"success": True, "user": serialize_user(updated)})


@app.route("/api/admin/settings", methods=["POST"])
@admin_required
def admin_settings():
    payload = request.get_json(silent=True) or {}
    # Every setting is saved ONLY if its key is present in the payload.
    # The previous unconditional saves silently reset any omitted setting to
    # its default on every POST — which is how allow_signup ended up False
    # (locking out signups) without anyone unchecking the box.
    if "allow_signup" in payload:
        save_setting("allow_signup", bool(payload.get("allow_signup")))
    if "margin_multiplier" in payload:
        save_setting(
            "margin_multiplier",
            clamp_float(payload.get("margin_multiplier"), 1.5, 0.5, 5.0),
        )
    if "signup_bonus_credits" in payload:
        save_setting(
            "signup_bonus_credits",
            clamp_float(payload.get("signup_bonus_credits"), 120.0, 0.0, 5000.0),
        )
    if "quote_character_assumption" in payload:
        save_setting(
            "quote_character_assumption",
            clamp_int(payload.get("quote_character_assumption"), 2, 0, 20),
        )
    if "credits_per_dollar" in payload:
        save_setting(
            "credits_per_dollar",
            clamp_float(payload.get("credits_per_dollar"), 100.0, 0.0, 10000.0),
        )
    if payload.get("default_planner_model"):
        save_setting("default_planner_model", payload["default_planner_model"])
    if payload.get("default_video_model"):
        save_setting("default_video_model", payload["default_video_model"])
    return jsonify({"success": True, "settings": load_settings()})


# Initialize schema at import time (runs under gunicorn too)
init_db()

if __name__ == "__main__":
    print("Linyan app ready at http://0.0.0.0:8080")
    app.run(debug=False, host="0.0.0.0", port=8080)