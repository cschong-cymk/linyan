"""
Linyan prompt preprocessing helper.

Analyzes per-shot prompts for complexity/overload risk and, when requested,
rewrites an overloaded prompt into:
  1. a clean, static still-image prompt suitable for generating a first frame, and
  2. a stripped motion/camera prompt for the video model.

This keeps the existing pipeline untouched when auto-preprocessing is disabled.
"""

import json
import re
from typing import Dict, List, Optional, Tuple
from urllib import error as urllib_error
from urllib import request as urllib_request


# Keywords used to score overload risk. These are intentionally broad: the
# goal is to flag prompts that are asking the video model to do too many
# independent things at once.
CAMERA_KEYWORDS: List[str] = [
    "pan", "pans", "panning", "zoom", "zooms", "zooming", "dolly", "dolly in",
    "dolly out", "track", "tracks", "tracking", "tilt", "tilts", "tilting",
    "orbit", "orbits", "orbiting", "crane", "cranes", "craning", "drone",
    "drones", "aerial", "handheld", "steadicam", "push in", "pull out",
    "rack focus", "whip pan", "whips", "whipping", "follow", "follows",
    "following", "rotating", "rotate", "rotates", "spin", "spins", "spinning",
    "swirling", "swirl", "swirls", "move left", "move right", "moving left",
    "moving right", "low angle", "high angle", "wide shot", "wider shot",
    "close up", "closeup", "closes in", "extreme close", "medium shot",
    "establishing shot", "point of view", "pov shot", "over the shoulder",
    "rack focuses", "pulling out", "pushing in",
    # Hyphenated variants that the literal matcher above would miss.
    "push-in", "whip-pan", "whip-pans", "close-up", "close-ups",
]

ACTION_KEYWORDS: List[str] = [
    "run", "runs", "running", "walk", "walks", "walking", "jump", "jumps",
    "jumping", "dance", "dances", "dancing", "wave", "waves", "waving",
    "gesture", "gestures", "gesturing", "turn", "turns", "turning",
    "look", "looks", "looking", "point", "points", "pointing", "reach",
    "reaches", "reaching", "grab", "grabs", "grabbing", "throw", "throws",
    "throwing", "fall", "falls", "falling", "rise", "rises", "rising",
    "approach", "approaches", "approaching", "enter", "enters", "entering",
    "exit", "exits", "exiting", "swing", "swings", "swinging", "spin",
    "spins", "roll", "rolls", "rolling", "slide", "slides", "sliding",
    "crouch", "crouches", "crouching", "leap", "leaps", "leaping",
    "sprint", "sprints", "sprinting", "march", "marches", "marching",
    "stroll", "strolls", "strolling",
    "slam", "slams", "slammed", "slamming",
    "jab", "jabs", "jabbed", "jabbing",
    "flick", "flicks", "flicked", "flicking",
    "snap", "snaps", "snapped", "snapping",
    "shut", "shuts",
    "pulse", "pulses", "pulsing",
    "hiss", "hisses", "hissing",
    "flicker", "flickers", "flickering",
    "pop", "pops", "popping",
    "scroll", "scrolls", "scrolling",
    "cover", "covers", "covered",
]

# Subset of ACTION_KEYWORDS used for heuristic staticization. Weather/natural
# motion words are excluded so we don't turn "rain begins to fall" into
# "rain begins to standing still".
STATIC_ACTION_KEYWORDS: List[str] = [
    k for k in ACTION_KEYWORDS
    if k not in {"fall", "falls", "falling", "rise", "rises", "rising"}
]

LIGHTING_EFFECT_KEYWORDS: List[str] = [
    "lightning", "explosion", "exploding", "fire", "flames", "smoke", "fog",
    "mist", "rain", "snow", "sparks", "embers", "strobe", "glitter",
    "particles", "lens flare", "flash", "flashing", "flicker", "flickering",
    "shimmer", "shimmering", "volumetric", "backlit", "rim light",
]

# Phrases that imply temporal progression; they belong in the motion prompt,
# not the still-image prompt.
TEMPORAL_KEYWORDS: List[str] = [
    "slowly", "gradually", "then", "suddenly", "as", "while", "over time",
    "throughout", "meanwhile", "next", "finally", "beginning", "ending",
    "transition", "fades in", "fades out", "cut to",
]


def _normalize_for_match(text: str) -> str:
    """Collapse punctuation variants so hyphenated forms match plain forms."""
    return text.lower().replace("-", " ")


def _count_keywords(text: str, keywords: List[str]) -> int:
    """Count whole-word/phrase keyword hits (case-insensitive)."""
    total = 0
    lowered = _normalize_for_match(text)
    for kw in keywords:
        kw_norm = _normalize_for_match(kw)
        # Use word boundaries for single-word keywords, literal matching for
        # multi-word phrases.
        if " " in kw_norm:
            total += lowered.count(kw_norm)
        else:
            total += len(re.findall(rf"\b{re.escape(kw_norm)}\b", lowered))
    return total


def analyze_prompt(prompt: str) -> Dict:
    """
    Score a prompt for overload risk.

    Returns:
        {
            "metrics": {char_count, word_count, action_count,
                        camera_move_count, lighting_effect_count},
            "score": <float>,
            "risk_level": "low" | "medium" | "high",
            "overload": <bool>,
        }
    """
    text = (prompt or "").strip()
    metrics = {
        "char_count": len(text),
        "word_count": len(text.split()),
        "action_count": _count_keywords(text, ACTION_KEYWORDS),
        "camera_move_count": _count_keywords(text, CAMERA_KEYWORDS),
        "lighting_effect_count": _count_keywords(text, LIGHTING_EFFECT_KEYWORDS),
    }

    # Tunable scoring. Camera moves are the most expensive source of failure,
    # so they carry the heaviest weight. Action density is next, then raw
    # length and lighting effects.
    score = (
        (metrics["char_count"] / 250.0)
        + (metrics["word_count"] / 40.0)
        + (metrics["action_count"] * 1.5)
        + (metrics["camera_move_count"] * 2.5)
        + (metrics["lighting_effect_count"] * 1.0)
    )

    if score <= 6.0:
        risk_level = "low"
    elif score <= 14.0:
        risk_level = "medium"
    else:
        risk_level = "high"

    return {
        "metrics": metrics,
        "score": round(score, 2),
        "risk_level": risk_level,
        "overload": risk_level == "high",
    }


def _split_clauses(prompt: str) -> List[str]:
    """Split a prompt into rough clauses/sentences."""
    normalized = prompt.replace("--", ", ").replace("—", ", ")
    parts = re.split(r"[.,;]\s*", normalized)
    return [p.strip() for p in parts if p.strip()]


def _heuristic_simplify(prompt: str, max_still_chars: int = 500) -> Tuple[str, str]:
    """Rule-based split into (still_image_prompt, motion_prompt).

    This is a last-resort fallback when no LLM is available. It drops clauses
    that are clearly about camera moves, temporal progression, or character
    actions, and keeps setting/mood/composition descriptors. The result is
    shorter and cleaner than verb-replacement heuristics, at the cost of
    losing some scene detail.
    """
    clauses = _split_clauses(prompt)
    kept: List[str] = []
    dropped: List[str] = []

    for clause in clauses:
        lowered = clause.lower()
        if any(kw in lowered for kw in CAMERA_KEYWORDS):
            dropped.append(clause)
            continue
        if any(kw in lowered for kw in TEMPORAL_KEYWORDS):
            dropped.append(clause)
            continue
        if _count_keywords(clause, ACTION_KEYWORDS) > 0:
            # Keep the clause only if it is mostly descriptive (<=1 short action
            # word) and contains setting/appearance information we want to keep.
            words = clause.split()
            action_hits = _count_keywords(clause, ACTION_KEYWORDS)
            if action_hits >= 2 or len(words) < 5:
                dropped.append(clause)
                continue
        kept.append(clause)

    simplified = ". ".join(kept).strip()
    if len(simplified) < 30:
        # Fallback: strip camera/temporal words from the original and keep it.
        camera_pattern = r"\b(" + "|".join(re.escape(k) for k in CAMERA_KEYWORDS) + r")[\w\-]*\b"
        simplified = re.sub(camera_pattern, "", prompt, flags=re.IGNORECASE)
        temporal_pattern = r"\b(" + "|".join(re.escape(k) for kw in TEMPORAL_KEYWORDS) + r")\b"
        simplified = re.sub(temporal_pattern, "", simplified, flags=re.IGNORECASE)
        simplified = re.sub(r"\s+", " ", simplified).strip(",. ")

    anchor = "static composition, no camera movement, single frozen moment"
    if anchor.lower() not in simplified.lower():
        simplified = f"{simplified}. {anchor}."
    simplified = simplified.strip()

    # Cap still-image prompt length; keep the anchor.
    if len(simplified) > max_still_chars:
        simplified = simplified[:max_still_chars].rsplit(" ", 1)[0] + f"... {anchor}."

    motion = extract_motion_prompt(prompt)
    return simplified, motion


def extract_motion_prompt(prompt: str, camera: str = "") -> str:
    """
    Extract motion/camera instructions from a prompt for the video model.

    Returns the original camera note plus any clauses that contain camera or
    action keywords. If nothing actionable is found, falls back to the camera
    note alone or an empty string.
    """
    if not prompt:
        return camera or ""

    clauses = _split_clauses(prompt)
    motion_clauses: List[str] = []
    for clause in clauses:
        lowered = clause.lower()
        if any(kw in lowered for kw in CAMERA_KEYWORDS) or any(kw in lowered for kw in ACTION_KEYWORDS):
            motion_clauses.append(clause)

    parts = motion_clauses[:]
    if camera and camera.strip():
        parts.append(camera.strip())

    motion = ". ".join(parts).strip(". ")
    return motion or (camera or "")


def rewrite_prompt(
    prompt: str,
    api_base: Optional[str] = None,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    timeout: int = 30,
) -> Dict[str, str]:
    """
    Rewrite an overloaded prompt with an LLM when credentials are supplied;
    otherwise fall back to the rule-based simplifier.

    Returns {"still": "...", "motion": "..."}
    """
    if not api_base or not api_key or not model:
        still, motion = _heuristic_simplify(prompt)
        return {"still": still, "motion": motion}

    system_text = (
        "You split video-generation shot prompts into two parts. "
        "Return ONLY valid JSON with keys still_image_prompt and motion_prompt. "
        "The still_image_prompt must be a clean, static, single-frame image prompt: "
        "describe the subject, setting, lighting, mood, and style, but NO camera movement and NO motion. "
        "The motion_prompt must contain only camera moves and subject actions/motion. "
        "Keep both concise."
    )
    user_text = f"Shot prompt:\n{prompt[:2000]}"
    payload = {
        "model": model,
        "temperature": 0.3,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system_text},
            {"role": "user", "content": user_text},
        ],
    }
    req = urllib_request.Request(
        f"{api_base.rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib_request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        raw = data["choices"][0]["message"]["content"].strip()
        parsed = json.loads(raw)
        still = str(parsed.get("still_image_prompt") or "").strip()
        motion = str(parsed.get("motion_prompt") or "").strip()
        if not still:
            raise ValueError("LLM returned empty still_image_prompt")
        return {"still": still, "motion": motion or extract_motion_prompt(prompt)}
    except (urllib_error.HTTPError, urllib_error.URLError, json.JSONDecodeError, KeyError, IndexError, ValueError):
        still, motion = _heuristic_simplify(prompt)
        return {"still": still, "motion": motion}


def simplify_prompt(prompt: str) -> str:
    """Convenience wrapper: returns only the still-image prompt (rule-based)."""
    still, _ = _heuristic_simplify(prompt)
    return still
