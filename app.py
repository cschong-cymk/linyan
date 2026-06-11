import json
import os
import threading
import subprocess
import textwrap
import uuid
import zipfile
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from urllib import error as urllib_error
from urllib import request as urllib_request

import psycopg2
import psycopg2.extras
from flask import Flask, g, jsonify, redirect, render_template, request, send_file, session
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

APP_ROOT = Path(__file__).resolve().parent
TEMPLATE_DIR = APP_ROOT / "templates"
DATA_DIR = APP_ROOT / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
OUTPUT_DIR = DATA_DIR / "outputs"
DATA_DIR.mkdir(exist_ok=True)
UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

APP_SECRET = os.environ.get("LINYAN_SECRET_KEY", "linyan-dev-secret-change-me")
DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgres://postgres:postgres@localhost:5432/flask"
)
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
OPENROUTER_HTTP_REFERER = os.environ.get("OPENROUTER_HTTP_REFERER", "https://linyan.io")
OPENROUTER_APP_NAME = os.environ.get("OPENROUTER_APP_NAME", "Linyan")

ASPECT_PRESETS = {
    "16:9": (1280, 720),
    "9:16": (720, 1280),
    "1:1": (1080, 1080),
    "4:5": (864, 1080),
}

MODEL_CATALOG = {
    "planner_models": [
        {
            "id": "openai/gpt-4.1-mini",
            "label": "GPT-4.1 Mini",
            "family": "openrouter",
            "recommended": True,
            "summary": "Fast story breakdown and shot planning.",
        },
        {
            "id": "anthropic/claude-3.7-sonnet",
            "label": "Claude 3.7 Sonnet",
            "family": "openrouter",
            "recommended": False,
            "summary": "Stronger scene logic, more costly.",
        },
        {
            "id": "google/gemini-2.5-pro",
            "label": "Gemini 2.5 Pro",
            "family": "openrouter",
            "recommended": False,
            "summary": "Good long-context planning when storyboards are messy.",
        },
    ],
    "video_models": [
        {
            "id": "google/veo-3-fast",
            "label": "Veo 3 Fast",
            "provider": "google",
            "cost_factor": 1.0,
            "summary": "Balanced default for fast preview renders.",
        },
        {
            "id": "kling/kling-2.1-master",
            "label": "Kling 2.1 Master",
            "provider": "kling",
            "cost_factor": 1.2,
            "summary": "More stylized motion and camera moves.",
        },
        {
            "id": "runway/gen-4-turbo",
            "label": "Runway Gen-4 Turbo",
            "provider": "runway",
            "cost_factor": 1.3,
            "summary": "Higher-end commercial feel, more costly.",
        },
        {
            "id": "qwen/wan-2.6-t2v",
            "label": "Wan 2.6 T2V",
            "provider": "qwen",
            "cost_factor": 0.9,
            "summary": "Budget-friendly exploration and rough ideation.",
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
    "margin_multiplier": 1.35,
    "signup_bonus_credits": 120.0,
    "base_job_credits": 18.0,
    "credit_label": "Linyan credits",
    "default_planner_model": "openai/gpt-4.1-mini",
    "default_video_model": "google/veo-3-fast",
}

# Linux container font candidates (macOS paths removed)
DRAW_TEXT_FONTS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
]

app = Flask(__name__, template_folder=str(TEMPLATE_DIR))
app.secret_key = APP_SECRET
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024


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


def get_video_cost_factor(video_model_id):
    for item in MODEL_CATALOG["video_models"]:
        if item["id"] == video_model_id:
            return item["cost_factor"], item["provider"]
    return 1.0, "custom"


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
    duration = config["target_duration"]
    resolution_factor = {
        "720p": 0.9,
        "1080p": 1.2,
        "4k": 1.8,
    }.get(config["resolution"], 1.2)
    model_factor, provider = get_video_cost_factor(config["video_model"])
    temperature_bundle = (
        config["planner_temperature"]
        + config["direction_temperature"]
        + config["motion_temperature"]
        + config["dialogue_temperature"]
    ) / 4.0
    consistency_factor = 1.0 + ((1.0 - config["consistency_strength"]) * 0.35)
    narration_bonus = 5.0 if config["narration_enabled"] else 0.0
    base = float(settings["base_job_credits"])
    cost = (base + duration * 0.45 + narration_bonus + temperature_bundle * 6.0) * resolution_factor
    cost = cost * model_factor * consistency_factor * float(settings["margin_multiplier"])
    return round(cost, 1), provider


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


def call_openrouter_planner(storyboard_text, config):
    if not OPENROUTER_API_KEY:
        return {
            "enabled": False,
            "status": "skipped",
            "message": "OPENROUTER_API_KEY not configured. Saved local placeholder render only.",
            "plan_text": "",
        }
    prompt = textwrap.dedent(
        f"""
        You are a storyboard-to-video planner.
        Convert the markdown storyboard below into a compact production brief.
        Return plain text with these sections:
        1. Story spine
        2. Character continuity
        3. Scene list
        4. Camera and motion notes
        5. Narration direction
        Constraints:
        - Style preset: {config["style_preset"]}
        - Target duration: {config["target_duration"]} seconds
        - Aspect ratio: {config["aspect_ratio"]}
        - Consistency strength: {config["consistency_strength"]}
        """
    ).strip()
    payload = {
        "model": config["planner_model"],
        "temperature": config["planner_temperature"],
        "messages": [
            {"role": "system", "content": "Be concise. Optimize for production clarity."},
            {"role": "user", "content": prompt + "\n\nStoryboard:\n" + storyboard_text[:12000]},
        ],
    }
    body = json.dumps(payload).encode("utf-8")
    request_headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": OPENROUTER_HTTP_REFERER,
        "X-Title": OPENROUTER_APP_NAME,
    }
    req = urllib_request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=body,
        headers=request_headers,
        method="POST",
    )
    try:
        with urllib_request.urlopen(req, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
        text_value = data["choices"][0]["message"]["content"].strip()
        return {
            "enabled": True,
            "status": "ok",
            "message": "Planner brief generated via OpenRouter.",
            "plan_text": text_value,
        }
    except (urllib_error.HTTPError, urllib_error.URLError, KeyError, IndexError, json.JSONDecodeError) as exc:
        return {
            "enabled": True,
            "status": "failed",
            "message": f"Planner call failed: {exc}",
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
    """Runs in a background thread. Opens its own connection."""
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

    try:
        set_status("planning")
        planner_payload = call_openrouter_planner(storyboard_text, config)
        plan_path = OUTPUT_DIR / f"{job_id}-plan.txt"
        plan_path.write_text(
            planner_payload["plan_text"] or planner_payload["message"],
            encoding="utf-8",
        )
        set_status(
            "rendering",
            plan_path=plan_path,
            extra_params={
                "planner_status": planner_payload["status"],
                "planner_message": planner_payload["message"],
            },
        )

        output_kind = "mp4"
        output_path = OUTPUT_DIR / f"{job_id}.mp4"
        try:
            generate_placeholder_video(output_path, config["title"], job_id, config, storyboard_text)
        except (subprocess.CalledProcessError, FileNotFoundError):
            output_kind = "zip"
            output_path = OUTPUT_DIR / f"{job_id}.zip"
            generate_placeholder_bundle(
                output_path, config["title"], job_id, config, storyboard_text, planner_payload
            )

        cur = db.cursor()
        cur.execute(
            "UPDATE users SET credit_balance = ROUND(CAST(credit_balance - %s AS numeric), 1) WHERE id = %s",
            (estimated_credits, user_id),
        )
        cur.execute(
            """INSERT INTO credit_ledger (user_id, delta, reason, note, actor_user_id, created_at)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (user_id, -round(estimated_credits, 1), "job_charge", f"Charged for job {job_id}", user_id, now_iso()),
        )
        db.commit()

        set_status("completed", output_path=output_path, output_kind=output_kind)
    except Exception as exc:
        set_status("failed", extra_params={"error": str(exc)})
    finally:
        db.close()


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
            "openrouter_ready": bool(OPENROUTER_API_KEY),
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
                "base_job_credits": float(settings["base_job_credits"]),
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
        clamp_float(payload.get("margin_multiplier"), 1.35, 0.5, 5.0),
    )
    save_setting(
        "signup_bonus_credits",
        clamp_float(payload.get("signup_bonus_credits"), 120.0, 0.0, 5000.0),
    )
    save_setting(
        "base_job_credits",
        clamp_float(payload.get("base_job_credits"), 18.0, 1.0, 500.0),
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
