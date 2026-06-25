from flask import Flask, request, jsonify
from flask_cors import CORS
from youtube_transcript_api import YouTubeTranscriptApi
from google import genai
from google.api_core.exceptions import ServiceUnavailable, ResourceExhausted
import re
import json
import os
import time
from functools import lru_cache

try:
    from google.api_core.exceptions import ServiceUnavailable, ResourceExhausted
except ImportError:
    ServiceUnavailable = Exception
    ResourceExhausted = Exception

app = Flask(__name__)
CORS(app)

# ─────────────────────────────────────────
# CONFIG — all values come from environment variables
# Set these in Koyeb dashboard under Environment
# ─────────────────────────────────────────

GEMINI_API_KEY  = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL    = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")

if not GEMINI_API_KEY:
    print("[STARTUP] WARNING — GEMINI_API_KEY is not set!", flush=True)

client = genai.Client(api_key=GEMINI_API_KEY)

GOOGLE_SHEET_ID   = os.getenv("GOOGLE_SHEET_ID",   "").strip()
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "Sheet1").strip()

# On Koyeb, paste the entire contents of your .json file into this env var
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()

# Local file fallback (only used when running locally)
GOOGLE_SERVICE_ACCOUNT_FILE = os.path.join(
    os.path.dirname(__file__),
    "yt-timestamp-project-770a409c15aa.json"
)

COOKIES_FILE = os.path.join(os.path.dirname(__file__), "cookies.txt")

SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
BASE_COLUMNS  = [
    "topic_id", "file_name", "Class", "Chapter",
    "topic_number", "teacher_name", "teacher_id", "video_url",
]

URL_COLUMN_CANDIDATES    = ["youtube_url", "video_url", "video", "url", "youtube_link", "link"]
STATUS_COLUMN_CANDIDATES = ["status"]

# ─────────────────────────────────────────
# SKIP THRESHOLDS
# ─────────────────────────────────────────

MIN_DURATION_SECONDS = 60

# ─────────────────────────────────────────
# ERROR CLASSIFICATION STRINGS
# ─────────────────────────────────────────

NO_CAPTION_ERRORS = [
    "no transcript",
    "transcripts are disabled",
    "could not retrieve a transcript",
    "no captions",
    "subtitles are disabled",
    "transcript unavailable",
    "notranscriptfound",
    "notranscriptavailable",
    "transcriptsdisabled",
    "could not find a transcript",
    "no transcript found",
    "subtitlesdisabled",
    "videosunavailable",
    "video unavailable",
    "this video is unavailable",
]

IP_BAN_ERRORS = [
    "429",
    "too many requests",
    "ipaddressblocked",
    "ip address blocked",
    "ip has been blocked",
    "access denied",
    "403",
    "youtubetranscriptapi.errors.ipblocked",
    "youtube is blocking",
    "sign in to confirm",
    "confirm you're not a bot",
    "bot detection",
    "robotcheck",
    "unusual traffic",
    "automated queries",
]

QUOTA_ERRORS = [
    "resource_exhausted",
    "resourceexhausted",
    "quota exceeded",
    "rate limit",
    "ratelimit",
    "quota limit",
    "429",
    "too many requests",
    "daily limit",
    "per_day",
    "per_minute",
    "quota_exceeded",
    "userratelimitexceeded",
]

# ─────────────────────────────────────────
# DEBUG HELPER
# ─────────────────────────────────────────

def dbg(section: str, msg: str):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}][{section}] {msg}", flush=True)

def dbg_error(section: str, e: Exception):
    import traceback
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}][{section}][ERROR] {type(e).__name__}: {e}", flush=True)
    print(f"[{ts}][{section}][TRACEBACK]\n{traceback.format_exc()}", flush=True)

# ─────────────────────────────────────────
# ERROR CLASSIFICATION
# ─────────────────────────────────────────

def classify_transcript_error(error_str: str) -> dict:
    lower = error_str.lower().replace(" ", "").replace("_", "")
    for needle in IP_BAN_ERRORS:
        if needle.replace(" ", "").replace("_", "") in lower:
            dbg("CLASSIFY", f"IP BAN detected in: {error_str[:120]}")
            return {
                "type": "error",
                "reason": "ip_banned",
                "user_message": (
                    "YouTube has temporarily blocked this server's IP address due to too many requests. "
                    "Please wait 10–30 minutes before trying again, or update your cookies.txt file."
                ),
            }
    for needle in NO_CAPTION_ERRORS:
        if needle.replace(" ", "") in lower:
            dbg("CLASSIFY", f"NO_CAPTION detected in: {error_str[:120]}")
            return {
                "type": "skip",
                "reason": "no_caption",
                "user_message": "No captions/transcript available for this video.",
            }
    if any(x in lower for x in ["connectionerror", "timeout", "networkerror", "socket", "connectionrefused", "httperror"]):
        dbg("CLASSIFY", f"NETWORK error detected: {error_str[:120]}")
        return {
            "type": "error",
            "reason": "network",
            "user_message": "Network error while fetching transcript. Check your internet connection and try again.",
        }
    dbg("CLASSIFY", f"UNKNOWN transcript error: {error_str[:120]}")
    return {
        "type": "error",
        "reason": "unknown",
        "user_message": f"Transcript fetch failed: {error_str}",
    }

def classify_gemini_error(error_str: str) -> dict:
    lower = error_str.lower().replace(" ", "").replace("_", "")
    for needle in QUOTA_ERRORS:
        if needle.replace(" ", "").replace("_", "") in lower:
            dbg("CLASSIFY", f"GEMINI QUOTA exceeded: {error_str[:120]}")
            return {
                "reason": "quota_exceeded",
                "user_message": (
                    "Gemini API quota limit reached. "
                    "Please wait 1–2 minutes and try again. "
                    "If this keeps happening, your daily quota may be exhausted — try tomorrow."
                ),
            }
    if any(x in lower for x in ["invalidapikey", "api_key_invalid", "apikey", "permission_denied", "permissiondenied"]):
        return {
            "reason": "invalid_key",
            "user_message": "Gemini API key is invalid or missing permissions. Check your GEMINI_API_KEY in Koyeb environment variables.",
        }
    if any(x in lower for x in ["serviceunavailable", "unavailable", "overloaded", "503"]):
        return {
            "reason": "service_down",
            "user_message": "Gemini service is temporarily unavailable. Please try again in a few minutes.",
        }
    return {
        "reason": "unknown",
        "user_message": f"Gemini generation failed: {error_str}",
    }

# ─────────────────────────────────────────
# GEMINI RETRY WRAPPER
# ─────────────────────────────────────────

def gemini_generate(prompt: str, max_retries: int = 5) -> str:
    last_error = None
    for attempt in range(max_retries):
        try:
            dbg("GEMINI", f"Attempt {attempt+1}/{max_retries} — model={GEMINI_MODEL}")
            response = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
            text = (response.text or "").strip()
            dbg("GEMINI", f"SUCCESS — Response length: {len(text)} chars")
            return text
        except (ServiceUnavailable, ResourceExhausted) as e:
            last_error = e
            wait = 2 ** (attempt + 1)
            dbg("GEMINI", f"Attempt {attempt+1} FAILED ({type(e).__name__}: {e}). Retrying in {wait}s…")
            if attempt < max_retries - 1:
                time.sleep(wait)
            else:
                classification = classify_gemini_error(str(e))
                raise RuntimeError(classification["user_message"]) from e
        except Exception as e:
            dbg_error("GEMINI", e)
            classification = classify_gemini_error(str(e))
            raise RuntimeError(classification["user_message"]) from e
    raise RuntimeError("Gemini generation failed unexpectedly.") from last_error

# ─────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────

def clean_url(url: str) -> str:
    junk = ['\u200b', '\u200c', '\u200d', '\ufeff', '\u00a0', '\r', '\t']
    cleaned = url.strip()
    for ch in junk:
        cleaned = cleaned.replace(ch, '')
    return cleaned.strip()

def extract_video_id(url: str):
    original = url
    url = clean_url(url)
    if url != original.strip():
        dbg("EXTRACT_ID", f"URL was cleaned — original repr: {repr(original)}")
    else:
        dbg("EXTRACT_ID", f"URL repr: {repr(url)}")
    patterns = [
        r"(?:v=|\/)([0-9A-Za-z_-]{11})(?:[&?#]|$)",
        r"(?:youtu\.be\/)([0-9A-Za-z_-]{11})",
        r"(?:embed\/)([0-9A-Za-z_-]{11})",
        r"(?:shorts\/)([0-9A-Za-z_-]{11})",
        r"([0-9A-Za-z_-]{11})(?:\?|$)",
    ]
    for i, p in enumerate(patterns):
        m = re.search(p, url)
        if m:
            vid = m.group(1)
            dbg("EXTRACT_ID", f"Matched pattern #{i+1} → video_id={vid}")
            return vid
    dbg("EXTRACT_ID", f"NO MATCH for URL: {repr(url)}")
    return None

def format_time(seconds: float) -> str:
    s = int(seconds)
    h, m, s = s // 3600, (s % 3600) // 60, s % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

def _make_ytt_client():
    if os.path.exists(COOKIES_FILE):
        dbg("YTT_CLIENT", f"cookies.txt found at: {COOKIES_FILE} — will use for requests")
        try:
            from youtube_transcript_api._http_client import CookieJar
            dbg("YTT_CLIENT", "Using new CookieJar API")
            return YouTubeTranscriptApi(http_client=CookieJar(COOKIES_FILE))
        except (ImportError, TypeError):
            pass
        try:
            dbg("YTT_CLIENT", "Using legacy cookies= API")
            return YouTubeTranscriptApi(cookies=COOKIES_FILE)
        except TypeError:
            pass
        dbg("YTT_CLIENT", "WARNING — could not pass cookies to API, trying without")
        return YouTubeTranscriptApi()
    else:
        dbg("YTT_CLIENT", f"WARNING — cookies.txt NOT found at {COOKIES_FILE}.")
        return YouTubeTranscriptApi()

def get_transcript(video_id: str):
    dbg("TRANSCRIPT", f"Fetching transcript for video_id={video_id}")
    ytt = _make_ytt_client()
    try:
        transcript = ytt.fetch(video_id, languages=["en", "hi", "en-IN", "en-US"])
        result = [{"start": s.start, "text": s.text} for s in transcript]
        dbg("TRANSCRIPT", f"SUCCESS — Fetched {len(result)} segments for video_id={video_id}")
        return result
    except Exception as e:
        dbg_error("TRANSCRIPT", e)
        raise

def build_transcript_text(transcript) -> str:
    return "\n".join(f"[{format_time(e['start'])}] {e['text']}" for e in transcript)

def get_video_duration_minutes(transcript) -> float:
    return transcript[-1]["start"] / 60 if transcript else 10

def get_timestamp_count(duration_minutes: float) -> tuple[int, int]:
    if duration_minutes <= 5:   return 3, 5
    if duration_minutes <= 10:  return 5, 8
    if duration_minutes <= 20:  return 8, 12
    if duration_minutes <= 30:  return 12, 16
    if duration_minutes <= 40:  return 16, 20
    if duration_minutes <= 60:  return 20, 25
    return 25, 30

def parse_response(raw: str) -> tuple[str, list]:
    dbg("PARSE_TS", f"Raw Gemini response (first 300 chars): {raw[:300]}")
    raw = re.sub(r"```(?:json)?", "", raw).strip().rstrip("```").strip()
    try:
        obj_start = raw.find("{")
        obj_end   = raw.rfind("}") + 1
        if obj_start != -1 and obj_end > obj_start:
            obj = json.loads(raw[obj_start:obj_end])
            if isinstance(obj, dict) and "timestamps" in obj:
                intro_title = str(obj.get("intro_title", "")).strip()
                timestamps  = obj["timestamps"]
                dbg("PARSE_TS", f"New format parsed — intro_title={intro_title!r}, {len(timestamps)} timestamps")
                return intro_title, timestamps
    except Exception as e:
        dbg("PARSE_TS", f"Object parse failed ({e}), trying array fallback")
    arr_start = raw.find("[")
    arr_end   = raw.rfind("]")
    if arr_start == -1 or arr_end == -1:
        dbg("PARSE_TS", f"ERROR — No JSON found. Full raw:\n{raw}")
        raise ValueError("No JSON array in Gemini response")
    parsed = json.loads(raw[arr_start:arr_end + 1])
    dbg("PARSE_TS", f"Array fallback: {len(parsed)} timestamps")
    return "", parsed

def time_str_to_seconds(t: str) -> int:
    parts = [int(p) for p in t.strip().split(":")]
    return parts[0] * 3600 + parts[1] * 60 + parts[2] if len(parts) == 3 else parts[0] * 60 + parts[1]

# ─────────────────────────────────────────
# QUALITY FILTER
# ─────────────────────────────────────────

def filter_timestamps(timestamps: list, min_gap_seconds: int = 60) -> list:
    if not timestamps:
        return timestamps
    filtered = [t for t in timestamps if t.get("confidence", 10) >= 7] or timestamps[:]
    dbg("FILTER_TS", f"After confidence filter: {len(filtered)} timestamps")
    result = [filtered[0]]
    for ts in filtered[1:]:
        try:
            if time_str_to_seconds(ts["time"]) - time_str_to_seconds(result[-1]["time"]) >= min_gap_seconds:
                result.append(ts)
        except Exception:
            result.append(ts)
    deduped = [result[0]]
    for ts in result[1:]:
        prev = set(deduped[-1].get("title", "").lower().split()[:3])
        curr = set(ts.get("title", "").lower().split()[:3])
        if len(prev & curr) < 2:
            deduped.append(ts)
    dbg("FILTER_TS", f"After dedup filter: {len(deduped)} timestamps")
    return deduped

# ─────────────────────────────────────────
# PROMPT & GENERATION
# ─────────────────────────────────────────

def build_prompt(min_ts: int, max_ts: int, duration_minutes: float) -> str:
    return f"""You are an expert YouTube chapter creator. The video is ~{int(duration_minutes)} minutes long.

TASK 1 — Identify the main topic/subject of this video in 3–6 words (used for the intro title).
TASK 2 — Find only major topic shifts in the video, like a table of contents.

RULES for timestamps:
1. Only timestamp when the MAIN TOPIC genuinely changes
2. Look for: "now let's talk about", "moving on to", "next we have", "let's discuss"
3. Each topic must be CLEARLY DIFFERENT from the previous
4. Merge closely related points into one timestamp
5. Min 60 seconds gap between any two timestamps
6. Cover entire video — beginning, middle, and end
7. Do NOT include 0:00 in the timestamps array
8. Generate between {min_ts} and {max_ts} timestamps total
9. Confidence 1–10; only include if 7 or above

Return ONLY a raw JSON object (no markdown, no backticks):
{{
  "intro_title": "Main Topic in 3-6 words",
  "timestamps": [
    {{
      "time": "2:15",
      "title": "Short title (3-5 words)",
      "desc": "One sentence about this new topic",
      "confidence": 9
    }}
  ]
}}

QUALITY OVER QUANTITY.
"""

def generate_from_transcript(transcript_text: str, transcript: list) -> list:
    duration_minutes = get_video_duration_minutes(transcript)
    min_ts, max_ts = get_timestamp_count(duration_minutes)
    limit = 40000 if duration_minutes > 30 else 25000 if duration_minutes > 15 else 12000
    dbg("GENERATE", f"duration={duration_minutes:.1f}min, target={min_ts}-{max_ts} timestamps, transcript_limit={limit}")
    prompt = build_prompt(min_ts, max_ts, duration_minutes)
    prompt += f"\n\nTranscript:\n{transcript_text[:limit]}"
    raw = gemini_generate(prompt)
    intro_title, timestamps = parse_response(raw)
    dbg("GENERATE", f"intro_title={intro_title!r}, raw_timestamps={len(timestamps)}")
    timestamps = [t for t in timestamps if t.get("time", "").strip() not in ("0:00", "0:00:00")]
    min_gap = max(60, int(duration_minutes * 60 / (max_ts * 2)))
    dbg("GENERATE", f"min_gap_seconds={min_gap}, timestamps before filter={len(timestamps)}")
    timestamps = filter_timestamps(timestamps, min_gap_seconds=min_gap)
    for t in timestamps:
        t.pop("confidence", None)
    intro_label = f"Introduction: {intro_title}" if intro_title else "Introduction"
    timestamps.insert(0, {
        "time":  "0:00",
        "title": intro_label,
        "desc":  "The video begins.",
    })
    dbg("GENERATE", f"Final timestamp count (incl. intro): {len(timestamps)}")
    return timestamps

# ─────────────────────────────────────────
# GOOGLE SHEETS — CREDENTIALS
# ─────────────────────────────────────────

def _get_sheet_credentials():
    if gspread is None or Credentials is None:
        raise RuntimeError("gspread / google-auth not installed.")
    if GOOGLE_SERVICE_ACCOUNT_JSON:
        dbg("SHEETS_CREDS", "Using env-var service account JSON")
        return Credentials.from_service_account_info(
            json.loads(GOOGLE_SERVICE_ACCOUNT_JSON), scopes=SHEETS_SCOPES
        )
    if GOOGLE_SERVICE_ACCOUNT_FILE and os.path.exists(GOOGLE_SERVICE_ACCOUNT_FILE):
        dbg("SHEETS_CREDS", f"Using service account file: {GOOGLE_SERVICE_ACCOUNT_FILE}")
        return Credentials.from_service_account_file(GOOGLE_SERVICE_ACCOUNT_FILE, scopes=SHEETS_SCOPES)
    raise RuntimeError("Missing Google service account credentials.")

# ─────────────────────────────────────────
# GOOGLE SHEETS — SINGLE VIDEO SAVE
# ─────────────────────────────────────────

@lru_cache(maxsize=1)
def get_worksheet():
    if not GOOGLE_SHEET_ID:
        return None
    creds = _get_sheet_credentials()
    gc = gspread.authorize(creds)
    return gc.open_by_key(GOOGLE_SHEET_ID).worksheet(GOOGLE_SHEET_NAME)

def _build_required_headers(timestamps: list) -> list:
    headers = BASE_COLUMNS[:]
    for i in range(1, len(timestamps) + 1):
        headers.extend([f"TT{i}", f"TD{i}"])
    return headers

def ensure_headers(worksheet, required_headers: list) -> list:
    existing = worksheet.row_values(1)
    if not existing:
        if worksheet.col_count < len(required_headers):
            worksheet.add_cols(len(required_headers) - worksheet.col_count)
        worksheet.update(values=[required_headers], range_name="A1")
        return required_headers
    if worksheet.col_count < len(required_headers):
        worksheet.add_cols(len(required_headers) - worksheet.col_count)
    headers = existing[:] + [""] * max(0, len(required_headers) - len(existing))
    changed = any(headers[i] != h for i, h in enumerate(required_headers))
    if changed:
        for i, h in enumerate(required_headers):
            headers[i] = h
        worksheet.update(values=[headers], range_name="A1")
    return headers

def save_to_google_sheet(video_url: str, timestamps: list) -> tuple[bool, str, int | None]:
    worksheet = get_worksheet()
    if worksheet is None:
        return False, "Google Sheets sync is not configured.", None
    required_headers = _build_required_headers(timestamps)
    headers = ensure_headers(worksheet, required_headers)
    row_map = {h: "" for h in headers}
    row_map["video_url"] = video_url
    for i, ts in enumerate(timestamps, start=1):
        row_map[f"TT{i}"] = str(ts.get("time", "")).strip()
        row_map[f"TD{i}"] = str(ts.get("title", "")).strip()
    row_values = [row_map.get(h, "") for h in headers]
    response = worksheet.append_row(row_values, value_input_option="RAW")
    try:
        updated_range = response.get("updates", {}).get("updatedRange", "")
        m = re.search(r"!A(\d+):", updated_range)
        row_num = int(m.group(1)) if m else None
    except Exception:
        row_num = None
    msg = f"Saved to Google Sheets (row {row_num})." if row_num else "Saved to Google Sheets."
    dbg("SAVE_SHEET", msg)
    return True, msg, row_num

# ─────────────────────────────────────────
# GOOGLE SHEETS — BATCH
# ─────────────────────────────────────────

def open_sheet_by_id(sheet_id: str, sheet_name: str):
    dbg("OPEN_SHEET", f"Opening sheet_id={sheet_id!r}, sheet_name={sheet_name!r}")
    creds = _get_sheet_credentials()
    gc = gspread.authorize(creds)
    return gc.open_by_key(sheet_id).worksheet(sheet_name)

def _col_map_0(headers: list) -> dict:
    return {h.strip(): i for i, h in enumerate(headers)}

def _col_map_1(headers: list) -> dict:
    return {h.strip(): i + 1 for i, h in enumerate(headers)}

def _find_col_0(col_map: dict, candidates: list):
    lower_map = {k.strip().lower(): v for k, v in col_map.items()}
    for cand in candidates:
        if cand.lower() in lower_map:
            return lower_map[cand.lower()]
    return None

def _find_col_1(col_map: dict, candidates: list):
    lower_map = {k.strip().lower(): v for k, v in col_map.items()}
    for cand in candidates:
        if cand.lower() in lower_map:
            return lower_map[cand.lower()]
    return None

def _tt_td_0(col_map: dict) -> tuple[dict, dict]:
    tt, td = {}, {}
    for h, i in col_map.items():
        h_norm = h.strip()
        m = re.match(r"^TT(\d+)$", h_norm, re.IGNORECASE)
        if m:
            tt[int(m.group(1))] = i
        m = re.match(r"^TD(\d+)$", h_norm, re.IGNORECASE)
        if m:
            td[int(m.group(1))] = i
    return tt, td

def _tt_td_1(col_map: dict) -> tuple[dict, dict]:
    tt, td = {}, {}
    for h, c in col_map.items():
        h_norm = h.strip()
        m = re.match(r"^TT(\d+)$", h_norm, re.IGNORECASE)
        if m:
            tt[int(m.group(1))] = c
        m = re.match(r"^TD(\d+)$", h_norm, re.IGNORECASE)
        if m:
            td[int(m.group(1))] = c
    return tt, td

def _ensure_batch_sheet_columns(ws, max_ts_needed: int):
    headers = ws.row_values(1)
    lower_existing = {h.strip().lower(): i + 1 for i, h in enumerate(headers)}
    to_append = []
    if "status" not in lower_existing:
        to_append.append("status")
    existing_tt_nums = []
    for h in headers:
        m = re.match(r"^TT(\d+)$", h.strip(), re.IGNORECASE)
        if m:
            existing_tt_nums.append(int(m.group(1)))
    existing_max = max(existing_tt_nums) if existing_tt_nums else 0
    for n in range(existing_max + 1, max_ts_needed + 1):
        to_append.append(f"TT{n}")
        to_append.append(f"TD{n}")
    if to_append:
        new_headers = headers + to_append
        if ws.col_count < len(new_headers):
            ws.add_cols(len(new_headers) - ws.col_count)
        ws.update(values=[new_headers], range_name="A1")
        headers = new_headers
        dbg("ENSURE_BATCH_COLS", f"Appended missing columns: {to_append}")
    return headers, _col_map_1(headers)

# ─────────────────────────────────────────
# ROUTES — SINGLE VIDEO
# ─────────────────────────────────────────

@app.route("/api/timestamps", methods=["POST"])
def get_timestamps():
    data = request.get_json(silent=True) or {}
    dbg("ROUTE/timestamps", f"━━━ NEW REQUEST ━━━ payload keys: {list(data.keys())}")
    raw_url = data.get("url", "")
    url = clean_url(raw_url)
    if not url:
        return jsonify({"error": "No URL provided"}), 400
    video_id = extract_video_id(url)
    if not video_id:
        return jsonify({"error": f"Could not extract video ID from URL: {url}"}), 400
    try:
        transcript = get_transcript(video_id)
    except Exception as e:
        err_str = str(e)
        classification = classify_transcript_error(err_str)
        if classification["type"] == "skip" and classification["reason"] == "no_caption":
            return jsonify({
                "skipped": True,
                "skip_reason": "no_caption",
                "skip_label": "No Captions",
                "error": classification["user_message"],
            }), 200
        return jsonify({
            "error": classification["user_message"],
            "error_reason": classification["reason"],
            "raw_error": err_str,
        }), 400
    duration_seconds = transcript[-1]["start"] if transcript else 0
    if duration_seconds < MIN_DURATION_SECONDS:
        return jsonify({
            "skipped": True,
            "skip_reason": "too_short",
            "skip_label": "Too Short",
            "error": f"Video is only {int(duration_seconds)}s long (minimum {MIN_DURATION_SECONDS}s required)",
        }), 200
    transcript_text = build_transcript_text(transcript)
    duration = round(duration_seconds / 60, 1)
    try:
        timestamps = generate_from_transcript(transcript_text, transcript)
    except RuntimeError as e:
        err_str = str(e)
        gemini_cls = classify_gemini_error(err_str)
        return jsonify({
            "error": err_str,
            "error_reason": gemini_cls["reason"],
        }), 503
    except Exception as e:
        err_str = str(e)
        dbg_error("ROUTE/timestamps", e)
        gemini_cls = classify_gemini_error(err_str)
        return jsonify({
            "error": gemini_cls["user_message"],
            "error_reason": gemini_cls["reason"],
            "raw_error": err_str,
        }), 500
    return jsonify({
        "timestamps": timestamps,
        "video_id":   video_id,
        "duration_minutes": duration,
    })

@app.route("/api/save-sheet", methods=["POST"])
def save_sheet():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or data.get("video_url") or "").strip()
    timestamps = data.get("timestamps") or []
    if not url:
        return jsonify({"error": "No video URL provided"}), 400
    if not isinstance(timestamps, list) or not timestamps:
        return jsonify({"error": "No timestamps provided"}), 400
    try:
        saved, message, row_num = save_to_google_sheet(url, timestamps)
        return jsonify({"sheet_saved": saved, "sheet_message": message, "sheet_row": row_num})
    except Exception as e:
        dbg_error("ROUTE/save-sheet", e)
        return jsonify({"error": f"Google Sheets save failed: {e}"}), 500

# ─────────────────────────────────────────
# ROUTES — BATCH
# ─────────────────────────────────────────

@app.route("/api/batch-load", methods=["GET"])
def batch_load():
    sheet_id   = request.args.get("sheet_id",   "").strip()
    sheet_name = request.args.get("sheet_name", "Sheet1").strip() or "Sheet1"
    if not sheet_id:
        return jsonify({"error": "sheet_id is required"}), 400
    try:
        ws = open_sheet_by_id(sheet_id, sheet_name)
        all_rows = ws.get_all_values()
        if not all_rows:
            return jsonify({"error": "Sheet is empty"}), 400
        headers = all_rows[0]
        col = _col_map_0(headers)
        url_i = _find_col_0(col, URL_COLUMN_CANDIDATES)
        if url_i is None:
            return jsonify({
                "error": (
                    f"Could not find a video URL column. Expected one of: "
                    f"{', '.join(URL_COLUMN_CANDIDATES)}. "
                    f"Found columns: {', '.join(headers)}"
                )
            }), 400
        stat_i = _find_col_0(col, STATUS_COLUMN_CANDIDATES)
        tt_i, td_i = _tt_td_0(col)
        videos = []
        for ri, row in enumerate(all_rows[1:], start=2):
            padded  = row + [""] * max(0, len(headers) - len(row))
            raw_url = padded[url_i]
            url     = clean_url(raw_url)
            if not url:
                continue
            raw_status = (padded[stat_i].strip() if stat_i is not None else "") or "pending"
            status = raw_status
            existing = []
            if status == "done":
                for n in sorted(tt_i):
                    t_val = padded[tt_i[n]].strip() if tt_i[n] < len(padded) else ""
                    d_val = padded[td_i[n]].strip() if n in td_i and td_i[n] < len(padded) else ""
                    if t_val:
                        existing.append({"time": t_val, "title": d_val, "desc": ""})
            videos.append({
                "row":                 ri,
                "url":                 url,
                "status":              status,
                "existing_timestamps": existing,
            })
        pending_count = sum(1 for v in videos if v["status"] not in ("done", "no_caption", "too_short"))
        done_count    = sum(1 for v in videos if v["status"] == "done")
        skipped_count = sum(1 for v in videos if v["status"] in ("no_caption", "too_short"))
        return jsonify({
            "videos":     videos,
            "total":      len(videos),
            "pending":    pending_count,
            "done":       done_count,
            "skipped":    skipped_count,
            "sheet_name": sheet_name,
        })
    except Exception as e:
        dbg_error("ROUTE/batch-load", e)
        return jsonify({"error": str(e)}), 500

@app.route("/api/batch-save", methods=["POST"])
def batch_save():
    data       = request.get_json(silent=True) or {}
    sheet_id   = data.get("sheet_id",   "").strip()
    sheet_name = data.get("sheet_name", "Sheet1").strip() or "Sheet1"
    results    = data.get("results", [])
    if not sheet_id:
        return jsonify({"error": "sheet_id required"}), 400
    if not results:
        return jsonify({"error": "No results to save"}), 400
    try:
        ws = open_sheet_by_id(sheet_id, sheet_name)
        skip_results   = [r for r in results if r.get("skip_reason")]
        normal_results = [r for r in results if not r.get("skip_reason")]
        max_ts_needed = max((len(r.get("timestamps", []) or []) for r in normal_results), default=0)
        headers, col = _ensure_batch_sheet_columns(ws, max_ts_needed)
        stat_c = _find_col_1(col, STATUS_COLUMN_CANDIDATES)
        if stat_c is None:
            return jsonify({"error": "Could not create or find a 'status' column in the sheet."}), 400
        tt_c, td_c = _tt_td_1(col)
        all_updates = []
        saved = 0
        skipped_saved = 0
        for r in skip_results:
            row_num     = r.get("row")
            skip_reason = r.get("skip_reason", "no_caption")
            if not row_num:
                continue
            all_updates.append({
                "range":  gspread.utils.rowcol_to_a1(row_num, stat_c),
                "values": [[skip_reason]],
            })
            skipped_saved += 1
        for r in normal_results:
            row_num    = r.get("row")
            timestamps = r.get("timestamps", [])
            if not row_num or not timestamps:
                continue
            all_updates.append({
                "range":  gspread.utils.rowcol_to_a1(row_num, stat_c),
                "values": [["done"]],
            })
            for i, ts in enumerate(timestamps, 1):
                if i in tt_c:
                    all_updates.append({
                        "range":  gspread.utils.rowcol_to_a1(row_num, tt_c[i]),
                        "values": [[str(ts.get("time",  "")).strip()]],
                    })
                if i in td_c:
                    all_updates.append({
                        "range":  gspread.utils.rowcol_to_a1(row_num, td_c[i]),
                        "values": [[str(ts.get("title", "")).strip()]],
                    })
            saved += 1
        if all_updates:
            ws.batch_update(all_updates)
        return jsonify({
            "saved":         saved,
            "skipped_saved": skipped_saved,
            "message":       f"Saved {saved} video(s) to Google Sheets." + (f" Marked {skipped_saved} as skipped." if skipped_saved else ""),
        })
    except Exception as e:
        dbg_error("ROUTE/batch-save", e)
        return jsonify({"error": f"Batch save failed: {e}"}), 500

@app.route("/api/health", methods=["GET"])
def health():
    cookies_ok = os.path.exists(COOKIES_FILE)
    return jsonify({
        "status":      "ok",
        "model":       GEMINI_MODEL,
        "sheet_id":    GOOGLE_SHEET_ID[:10] + "…" if GOOGLE_SHEET_ID else "NOT SET",
        "gemini_key":  "SET ✓" if GEMINI_API_KEY else "MISSING ✗",
        "sa_json":     "SET ✓" if GOOGLE_SERVICE_ACCOUNT_JSON else ("file found ✓" if os.path.exists(GOOGLE_SERVICE_ACCOUNT_FILE) else "MISSING ✗"),
        "cookies_txt": "found ✓" if cookies_ok else "missing (IP blocks possible)",
    })

if __name__ == "__main__":
    print("=" * 60, flush=True)
    print(f"[STARTUP] GEMINI_MODEL   = {GEMINI_MODEL}", flush=True)
    print(f"[STARTUP] GEMINI_KEY     = {'SET ✓' if GEMINI_API_KEY else 'MISSING ✗'}", flush=True)
    print(f"[STARTUP] SHEET_ID       = {GOOGLE_SHEET_ID[:20] + '…' if GOOGLE_SHEET_ID else 'NOT SET'}", flush=True)
    print(f"[STARTUP] SA_JSON_ENV    = {'SET ✓' if GOOGLE_SERVICE_ACCOUNT_JSON else 'NOT SET'}", flush=True)
    print(f"[STARTUP] COOKIES_FILE   = {COOKIES_FILE} ({'FOUND ✓' if os.path.exists(COOKIES_FILE) else 'MISSING ✗'})", flush=True)
    print("=" * 60, flush=True)
    app.run(debug=False, host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
