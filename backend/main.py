import os
import re
import time

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import (
    TranscriptsDisabled,
    NoTranscriptFound,
    VideoUnavailable,
    CouldNotRetrieveTranscript,
)
import google.generativeai as genai
from google.api_core.exceptions import ResourceExhausted

load_dotenv()  # reads backend/.env automatically, no manual export needed

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

if GEMINI_API_KEY:
    print("[startup] GEMINI_API_KEY loaded")
else:
    print("[startup] GEMINI_API_KEY is NOT set — check backend/.env")
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

app = FastAPI(title="YouTube Summarizer")

# Wide open for local dev. Lock this down to your frontend's real origin
# before deploying anywhere public.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def catch_all_exception_handler(request: Request, exc: Exception):
    # Without this, an unexpected error (e.g. from the Gemini call) returns
    # FastAPI's default plain-text 500 page, which breaks the frontend's
    # res.json() call and shows a misleading "can't reach backend" message.
    return JSONResponse(status_code=500, content={"detail": f"Unexpected server error: {exc}"})

# Simple in-memory cache: "{video_id}:{style}" -> result dict.
# Swap for Redis or a DB table if you need it to survive a restart
# or be shared across multiple backend instances.
_cache: dict[str, dict] = {}

YOUTUBE_ID_PATTERNS = [
    r"(?:v=|/)([0-9A-Za-z_-]{11}).*",
    r"youtu\.be/([0-9A-Za-z_-]{11})",
]


class SummarizeRequest(BaseModel):
    url: str
    style: str = "tldr_bullets"  # tldr_bullets | chapters | study_notes


def extract_video_id(url: str) -> str:
    for pattern in YOUTUBE_ID_PATTERNS:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    raise HTTPException(status_code=400, detail="Could not find a YouTube video ID in that URL.")


def fetch_transcript(video_id: str) -> list:
    """
    Fetch captions for a video.

    NOTE: as of 2026, YouTube frequently requires a "PoToken" to serve
    caption data, and blocks unauthenticated/scripted requests without one.
    When that happens, this raises a clear 502 rather than letting the raw
    XML-parse crash take the request down (which is what an unpatched
    version of this function used to do).
    """
    try:
        return YouTubeTranscriptApi().fetch(video_id).snippets
    except TranscriptsDisabled:
        raise HTTPException(
            status_code=422,
            detail="This video has captions disabled, so it can't be summarized.",
        )
    except NoTranscriptFound:
        raise HTTPException(status_code=422, detail="No transcript was found for this video.")
    except VideoUnavailable:
        raise HTTPException(status_code=422, detail="That video doesn't exist or is private/unavailable.")
    except CouldNotRetrieveTranscript:
        raise HTTPException(
            status_code=502,
            detail=(
                "YouTube blocked this transcript request. This is a known, common issue "
                "in 2026 (YouTube now often requires a 'PoToken' for caption access) and "
                "isn't specific to this video. See the README's troubleshooting section."
            ),
        )
    except Exception:
        # Catch-all so a library-internal failure (e.g. an XML parse error
        # from an empty/blocked response) returns JSON instead of crashing
        # the request with an unhandled 500.
        raise HTTPException(
            status_code=502,
            detail=(
                "Couldn't fetch or parse this video's transcript. YouTube may be blocking "
                "the request — see the README's troubleshooting section."
            ),
        )


def format_transcript(snippets: list) -> str:
    """Turn raw caption snippets into '[MM:SS] text' lines the model can cite."""
    lines = []
    for s in snippets:
        minutes, seconds = divmod(int(s.start), 60)
        timestamp = f"{minutes:02d}:{seconds:02d}"
        lines.append(f"[{timestamp}] {s.text}")
    return "\n".join(lines)


PROMPTS = {
    "tldr_bullets": (
        "You are summarizing a YouTube video transcript with timestamps. "
        "Write a 2-3 sentence TL;DR, then 5-8 bullet points of key ideas. "
        "Reference a relevant timestamp (MM:SS) next to each bullet when useful. "
        "Be concise and skip filler."
    ),
    "chapters": (
        "You are summarizing a YouTube video transcript with timestamps. "
        "Break the video into logical chapters. For each chapter, give a short "
        "title, its starting timestamp (MM:SS), and a 1-2 sentence description."
    ),
    "study_notes": (
        "You are summarizing a YouTube video transcript with timestamps for a "
        "student. Write key takeaways as notes, then 3 short quiz questions "
        "(with answers) testing understanding of the material."
    ),
}

# Rough char budget for a single-pass call on gemini-2.0-flash's context
# window. Well past this, chunk-then-reduce instead of sending it all at
# once. ~4 chars/token, leaving headroom for the prompt and response.
SINGLE_PASS_CHAR_LIMIT = 500_000


def summarize_with_gemini(transcript_text: str, style: str) -> str:
    if not GEMINI_API_KEY:
        raise HTTPException(status_code=500, detail="GEMINI_API_KEY is not configured on the server.")

    if len(transcript_text) > SINGLE_PASS_CHAR_LIMIT:
        raise HTTPException(
            status_code=413,
            detail="This video's transcript is too long for single-pass summarization.",
        )

    model = genai.GenerativeModel("gemini-3.6-flash")
    system_prompt = PROMPTS.get(style, PROMPTS["tldr_bullets"])

    try:
        response = model.generate_content(
            f"{system_prompt}\n\nTranscript:\n{transcript_text}"
        )
        return response.text
    except ResourceExhausted:
        raise HTTPException(
            status_code=429,
            detail="Gemini API quota exceeded. Please wait and try again later.",
        )


@app.post("/api/summarize")
def summarize(req: SummarizeRequest):
    video_id = extract_video_id(req.url)
    cache_key = f"{video_id}:{req.style}"

    if cache_key in _cache:
        return {**_cache[cache_key], "cached": True}

    transcript = fetch_transcript(video_id)
    transcript_text = format_transcript(transcript)
    summary = summarize_with_gemini(transcript_text, req.style)

    result = {
        "video_id": video_id,
        "style": req.style,
        "summary": summary,
        "generated_at": time.time(),
    }
    _cache[cache_key] = result
    return {**result, "cached": False}


@app.get("/api/health")
def health():
    return {"status": "ok"}
