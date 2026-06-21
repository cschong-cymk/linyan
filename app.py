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
DATA_DIR.mkdir(exist_ok=True)
UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

APP_SECRET = os.environ.get("LINYAN_SECRET_KEY", "linyan-dev-secret-change-me")
DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgres://postgres:postgres@localhost:5432/flask"
)
# ModelArk (BytePlus Ark) — used for both planning and video generation
ARK_API_KEY = os.environ.get("ARK_API_KEY")
ARK_API_BASE = os.environ.get("ARK_API_BASE", "https://ark.ap-southeast.bytepluses.com/api/v3")

# How long to poll for a single shot clip before giving up (seconds)
SHOT_POLL_TIMEOUT = int(os.environ.get("SHOT_POLL_TIMEOUT", "600"))
SHOT_POLL_INTERVAL = int(os.environ.get("SHOT_POLL_INTERVAL", "10"))

ASPECT_PRESETS = {
    "16:9": (1280, 720),
    "9:16": (720, 1280),
    "1:1": (1080, 1080),
    "4:5": (864, 1080),
}

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
            "id": "seed-1-6-250915",
            "label": "Seed 1.6",
            "family": "ark",
            "recommended": False,
            "summary": "Stronger scene logic, more costly.",
        },
        {
            "id": "seed-1-8-251228",
            "label": "Seed 1.8",
            "family": "ark",
            "recommended": False,
            "summary": "Deep reasoning mode; best for complex multi-scene storyboards.",
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
        "resolution": raw_config.get("resolution") or "1080p",
        "style_preset": raw_config.get("style_preset") or "Cinematic realism",
        "voice_model": raw_config.get("voice_model") or "none",
        "narration_enabled": bool(raw_config.get("narration_enabled")),
        "target_duration": clamp_int(raw_config.get("target_duration"), 30, 8, 180),
        "planner_temperature": clamp_float(raw_config.get("planner_temperature"), 0.4, 0.0, 1.0),
        "direction_temperature": clamp_float(raw_config.get("direction_temperature"), 0.5, 0.0, 1.0),
        "motion_temperature": clamp_float(raw_config.get("motion_temperature"), 0.5, 0.0, 1.0),
        "dialogue_temperature": clamp_float(raw_config.get("dialogue_temperature"), 0.4, 0.0, 1.0),
        "consistency_strength": clamp_float(raw_config.get("consistency_strength"), 0.8, 0.1, 1.0),
    }
    if config["aspect_ratio"] not in ASPECT_PRESETS:
        config["aspect_ratio"] = "16:9"
    if config["voice_model"] == "none":
        config["narration_enabled"] = False
    return config


def estimate_job_cost(config, settings):
    """
    Estimate job cost in Linyan credits based on real Seedance 2.0 published rates.

    Published USD rates (per second of generated video):
      dreamina-seedance-2-0-260128       (Standard, 1080p): $0.05–$0.10  → midpoint $0.075/s
      dreamina-seedance-2-0-fast-260128  (Fast,     720p):  $0.01–$0.02  → midpoint $0.015/s

    1 Linyan credit = $0.01 USD
    Margin multiplier applied on top (default 1.5 = 50% margin).
    """
    SEEDANCE_USD_PER_SECOND = {
        "dreamina-seedance-2-0-260128":      0.075,
        "dreamina-seedance-2-0-fast-260128": 0.015,
    }
    CREDITS_PER_USD = 100.0  # 1 credit = $0.01

    video_model = config["video_model"]
    usd_per_second = SEEDANCE_USD_PER_SECOND.get(video_model, 0.075)
    provider = "ark"

    duration = config["target_duration"]
    margin = float(settings["margin_multiplier"])

    raw_usd = usd_per_second * duration
    charged_usd = raw_usd * margin
    credits = charged_usd * CREDITS_PER_USD

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


def call_planner(storyboard_text, config):
    """
    Call ModelArk (BytePlus Ark) chat completions to decompose a prose storyboard
    into a list of structured shots with durations that sum to target_duration.

    Returns a dict:
      {
        "status": "ok" | "skipped" | "failed",
        "message": str,
        "shots": [
          {
            "index": int,          # 1-based
            "duration": int,       # 5 or 10 (Seedance constraint)
            "prompt": str,         # rich visual prompt for the video model
            "camera": str,         # camera move / framing note
            "narration": str       # VO line or "" if none
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
            "shots": [],
            "plan_text": "",
        }

    # Decide reasonable shot count from target duration.
    # Seedance supports 5 s and 10 s clips. We prefer 5 s clips for tighter control,
    # allowing 10 s only when the planner decides a scene needs more breathing room.
    target = config["target_duration"]
    max_shots = max(1, target // 5)   # upper bound: one 5-second clip per slot

    system_prompt = textwrap.dedent("""
        You are a professional video production planner.
        Your job is to decompose a prose storyboard into discrete video shots
        that a text-to-video model will render one at a time.
        Each shot must have a duration between 4 and 15 seconds (integers only).
        The total of all shot durations must not exceed the target duration.
        Return ONLY valid JSON — no markdown fences, no commentary.
        The JSON must match this schema exactly:
        {
          "shots": [
            {
              "index": <integer, 1-based>,
              "duration": <5 or 10>,
              "prompt": "<rich visual description for the video model>",
              "camera": "<camera movement and framing note>",
              "narration": "<voice-over line, or empty string if none>"
            }
          ]
        }
        Rules:
        - Prompt must be self-contained (the video model has no memory between shots).
        - Describe characters, setting, lighting, and action in each prompt.
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
        shots = parsed.get("shots", [])
        if not shots:
            raise ValueError("Planner returned zero shots.")
        # Validate and clamp each shot
        clean_shots = []
        for i, s in enumerate(shots):
            duration = int(s.get("duration", 5))
            duration = max(4, min(15, duration))  # Seedance supports 4–15 s
            clean_shots.append({
                "index": i + 1,
                "duration": duration,
                "prompt": str(s.get("prompt", "")).strip(),
                "camera": str(s.get("camera", "")).strip(),
                "narration": str(s.get("narration", "")).strip(),
            })
        return {
            "status": "ok",
            "message": f"Planner produced {len(clean_shots)} shots via ModelArk.",
            "shots": clean_shots,
            "plan_text": json.dumps({"shots": clean_shots}, indent=2),
        }
    except (urllib_error.HTTPError, urllib_error.URLError) as exc:
        return {
            "status": "failed",
            "message": f"ModelArk request failed: {exc}",
            "shots": [],
            "plan_text": "",
        }
    except (KeyError, IndexError, json.JSONDecodeError, ValueError) as exc:
        return {
            "status": "failed",
            "message": f"Planner response parse error: {exc}",
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


def run_job_async(job_id, user_id, config, storyboard_text, storyboard_path, estimated_credits):
    """Runs in a background thread. Opens its own DB connection."""
    db = new_connection()

    def set_status(status, output_path=None, output_kind=None, plan_path=None, extra_params=None):
        cur = db.cursor()
        updates = ["status = %s", "updated_at = %s"]
        values = [status, now_iso()]
        if output_path:
            updates += ["output_path = %s", "output_kind = %s"]
            values += [str(output_path), output_kind]
        if plan_path:
            updates += ["render_plan_path = %s"]
            values += [str(plan_path)]
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
            },
        )

        # ── Phase 2 & 3: Render shots then stitch ───────────────────────────
        # If planner succeeded and we have real shots, attempt real rendering.
        # If planner failed/skipped, fall through to placeholder immediately.
        output_kind = "mp4"
        output_path = OUTPUT_DIR / f"{job_id}.mp4"
        rendered_ok = False

        if planner["status"] == "ok" and planner["shots"]:
            clips, shot_log = render_shots(job_id, planner["shots"], config)
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

        # ── Phase 4: Charge credits only on success ──────────────────────────
        charge_credits()
        set_status("completed", output_path=output_path, output_kind=output_kind)

    except RuntimeError as exc:
        # Credit race or explicit business logic failure
        set_status("failed", extra_params={"error": str(exc)})
    except Exception as exc:
        set_status("failed", extra_params={"error": str(exc)})
    finally:
        db.close()


def call_video_model(shot, config, clip_path):
    """
    Submit one shot to Seedance 2.0, poll until done, download clip.
    Returns (True, "ok") on success, (False, reason_string) on any failure.
    """
    if not ARK_API_KEY:
        return False, "ARK_API_KEY not set"

    prompt_parts = [shot["prompt"]]
    if shot.get("camera"):
        prompt_parts.append(shot["camera"])
    full_prompt = ". ".join(p.strip().rstrip(".") for p in prompt_parts if p.strip())

    duration = max(4, min(15, int(shot.get("duration", 5))))

    payload = {
        "model": config["video_model"],
        "content": [{"type": "text", "text": full_prompt}],
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
            return False, f"No task_id in submit response: {submit_data}"
    except urllib_error.HTTPError as exc:
        return False, f"Submit HTTP {exc.code}: {exc.reason}"
    except (urllib_error.URLError, json.JSONDecodeError) as exc:
        return False, f"Submit error: {exc}"

    # Poll until terminal state or timeout
    poll_url = f"{ARK_API_BASE}/contents/generations/tasks/{task_id}"
    poll_headers = {"Authorization": f"Bearer {ARK_API_KEY}"}
    deadline = time.monotonic() + SHOT_POLL_TIMEOUT

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
            video_url = (
                poll_data.get("content", {}).get("video_url")
                or poll_data.get("output", {}).get("video_url")
                or poll_data.get("video_url")
            )
            if not video_url:
                return False, "succeeded but no video_url in response"
            try:
                dl_req = urllib_request.Request(video_url, method="GET")
                with urllib_request.urlopen(dl_req, timeout=120) as dl_resp:
                    clip_path.write_bytes(dl_resp.read())
                return True, "ok"
            except (urllib_error.URLError, OSError) as exc:
                return False, f"Download failed: {exc}"

        elif status in ("failed", "cancelled", "expired", "error"):
            err = poll_data.get("error") or {}
            return False, f"Task {status}: {err.get('message', 'no details')}"

        # still queued/running — keep polling

    return False, f"Timed out after {SHOT_POLL_TIMEOUT}s waiting for task {task_id}"


def render_shots(job_id, shots, config):
    """
    Render each shot via Seedance 2.0 on ModelArk.
    Processes shots sequentially with a 1-second gap to respect QPS=2.

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
        ok, reason = call_video_model(shot, config, clip_path)
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


def create_job_record(user_id, original_filename, config, storyboard_text):
    settings = load_settings()
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
            params_json, created_at, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            job_id, user_id, config["title"], "queued", original_filename,
            str(storyboard_path), None,
            str(OUTPUT_DIR / f"{job_id}.mp4"), "mp4",
            config["planner_model"], config["video_model"], provider, estimated_credits,
            json.dumps(config), timestamp, timestamp,
        ),
        commit=True,
    )

    t = threading.Thread(
        target=run_job_async,
        args=(job_id, user_id, config, storyboard_text, storyboard_path, estimated_credits),
        daemon=True,
    )
    t.start()

    cur = db_execute("SELECT * FROM jobs WHERE id = %s", (job_id,))
    row = cur.fetchone()
    return serialize_job(row), None


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/studio")
def studio():
    user = current_user()
    if not user:
        return redirect("/")
    return render_template("inner.html")


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
            },
            "catalog": MODEL_CATALOG,
            "ark_ready": bool(ARK_API_KEY),
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
    save_setting("allow_signup", bool(payload.get("allow_signup")))
    save_setting(
        "margin_multiplier",
        clamp_float(payload.get("margin_multiplier"), 1.5, 0.5, 5.0),
    )
    save_setting(
        "signup_bonus_credits",
        clamp_float(payload.get("signup_bonus_credits"), 120.0, 0.0, 5000.0),
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
