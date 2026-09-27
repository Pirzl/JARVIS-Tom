"""Understand YouTube videos from captions and public metadata only."""
from __future__ import annotations

import html
import re
import time
from urllib.parse import urlparse

from actions.youtube_video import (
    HEADERS,
    _REQUESTS_OK,
    _extract_video_id,
    _get_transcript,
    _is_valid_youtube_url,
    _scrape_video_info,
)

_MAX_TRANSCRIPT_CHARS = 70000
_RESULT_CACHE: dict[str, tuple[float, str]] = {}
_SOURCE_CACHE: dict[str, dict] = {}
_CACHE_TTL = 300.0


def get_cached_source(url: str) -> dict:
    """Return the last fetched source payload for a URL in this process."""
    video_id = _extract_video_id(str(url or ""))
    return dict(_SOURCE_CACHE.get(video_id, {})) if video_id else {}


def _description(video_id: str) -> str:
    if not _REQUESTS_OK:
        return ""
    try:
        import requests
        response = requests.get(
            f"https://www.youtube.com/watch?v={video_id}",
            headers=HEADERS,
            timeout=12,
        )
        match = re.search(r'"shortDescription":"(.*?)"', response.text)
        if not match:
            return ""
        text = bytes(match.group(1), "utf-8").decode("unicode_escape", errors="replace")
        return html.unescape(text).strip()[:5000]
    except Exception as exc:
        print(f"[YouTube] Description fetch failed: {exc}")
        return ""


def _thumbnail(video_id: str) -> str:
    return f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg"


def youtube_understand(parameters: dict, player=None, speak=None) -> str:
    url = str(parameters.get("url", "")).strip()
    question = str(parameters.get("question", "What is this video about?")).strip()
    if not url or not _is_valid_youtube_url(url):
        return "Please provide a valid YouTube URL."
    video_id = _extract_video_id(url)
    if not video_id:
        return "I could not extract a YouTube video ID from that URL."
    cached = _RESULT_CACHE.get(video_id)
    if cached and time.monotonic() - cached[0] < _CACHE_TTL:
        return "[YOUTUBE_COMPLETE] " + cached[1]

    if player:
        player.write_log(f"[YouTube] Understanding: {url}")
    if speak:
        speak("I will read the transcript and public metadata, not download or play the video.")

    transcript = _get_transcript(video_id)
    info = _scrape_video_info(video_id)
    description = _description(video_id)
    if not transcript and not info and not description:
        return "I could not retrieve captions or public metadata for that video."

    from core import gemini
    from google.genai import types

    transcript_text = (transcript or "No transcript was available.")[:_MAX_TRANSCRIPT_CHARS]
    _SOURCE_CACHE[video_id] = {
        "url": url,
        "title": info.get("title", "YouTube video"),
        "transcript": transcript or "",
        "description": description,
        "metadata": info,
    }
    source = (
        "YouTube understanding request. This is public metadata and a transcript, "
        "not the video stream. Do not claim to have watched the video. If the answer "
        "depends on purely visual content, say that the transcript cannot establish it.\n\n"
        f"URL: {url}\n"
        f"Video ID: {video_id}\n"
        f"Title: {info.get('title', 'unknown')}\n"
        f"Channel: {info.get('channel', 'unknown')}\n"
        f"Duration: {info.get('duration', 'unknown')}\n"
        f"Views: {info.get('views', 'unknown')}\n"
        f"Thumbnail URL (not analysed): {_thumbnail(video_id)}\n"
        f"Description:\n{description or 'Not available'}\n\n"
        f"Transcript:\n{transcript_text}\n\n"
        f"User question: {question}"
    )
    try:
        response = gemini.call(
            source,
            tier=gemini.SMART,
            timeout_ms=60000,
            config=types.GenerateContentConfig(
                system_instruction=(
                    "Answer in the user's language. Give a concise overview followed by "
                    "key points and directly answer the question. State clearly when "
                    "captions are missing or when a claim cannot be known from transcript "
                    "and metadata. Never say you watched, played, or downloaded the video."
                )
            ),
        )
        if response is None:
            return "I retrieved the YouTube sources but Gemini was unavailable for the summary."
        result = (response.text or "").strip()
        if not result:
            return "I retrieved the YouTube sources but received an empty summary."
        _RESULT_CACHE[video_id] = (time.monotonic(), result)
        return "[YOUTUBE_COMPLETE] " + result
    except Exception as exc:
        return f"YouTube understanding failed: {exc}"


TOOL = {
    "name": "youtube_understand",
    "description": (
        "Understand a YouTube video from its URL using captions, public metadata, and "
        "a thumbnail link. It does not play or download the video. Use for summaries, "
        "talks, tutorials, recipes, and questions about spoken content. Be honest when "
        "an answer requires purely visual information."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "url": {"type": "STRING", "description": "The YouTube video URL."},
            "question": {"type": "STRING", "description": "What the user wants to know."},
        },
        "required": ["url"],
    },
    "handler": youtube_understand,
}
