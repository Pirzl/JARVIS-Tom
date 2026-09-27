"""
Google Home / Nest / Chromecast control action for JARVIS.

Allows discovery of local Google Cast speakers and groups, sending audio/music streams,
making voice announcements across the house, stopping media, and adjusting volume.
"""

import time
import urllib.parse
from typing import Dict, Any, List, Optional
import pychromecast

# Common radio stations & music stream shortcuts
PRESET_STATIONS = {
    "lofi": ("https://stream.zeno.fm/f3wvbbqmdg8uv", "Lofi Hip Hop Radio", "audio/mp3"),
    "chill": ("https://stream.zeno.fm/f3wvbbqmdg8uv", "Chillout Radio", "audio/mp3"),
    "jazz": ("https://stream.zeno.fm/0r0a7zs912quv", "Jazz Radio", "audio/mp3"),
    "rock": ("https://stream.zeno.fm/rk1b089531zuv", "Rock Radio", "audio/mp3"),
    "pop": ("https://stream.zeno.fm/912m8q4912quv", "Pop Radio", "audio/mp3"),
    "top40": ("https://stream.zeno.fm/912m8q4912quv", "Top 40 Hits", "audio/mp3"),
}


def _discover_cast_devices(timeout: float = 3.0):
    """Discover Google Cast / Nest devices on the local network."""
    chromecasts, browser = pychromecast.get_chromecasts(timeout=timeout)
    return chromecasts, browser


def _find_device(chromecasts: List[Any], target_name: str):
    """Find a chromecast matching name (case-insensitive substring match)."""
    target = target_name.strip().lower()
    
    # Check for "all devices" in English, Spanish, German
    all_keywords = ["all", "todos", "alle", "allen", "toda la casa", "everywhere", "tutti", "geräten", "geräte"]
    if target in ("all", "todos", "alle") or any(k in target for k in all_keywords):
        # Look for known speaker groups first
        for cc in chromecasts:
            cname = cc.name.lower()
            if any(g in cname for g in ["grupo de la casa", "play all", "dispositivos de casa", "standardgruppe", "todos"]):
                return cc
        # Return first group found
        groups = [cc for cc in chromecasts if "group" in str(cc.model_name).lower()]
        if groups:
            return groups[0]

    # Exact match
    for cc in chromecasts:
        if target == cc.name.lower():
            return cc

    # Substring match
    for cc in chromecasts:
        if target in cc.name.lower() or cc.name.lower() in target:
            return cc

    return None


def google_home_action(parameters: dict, player=None) -> str:
    """Action handler for Google Home / Nest / Chromecast control."""
    operation = str(parameters.get("operation", "list")).strip().lower()
    device_name = str(parameters.get("device", "all")).strip()
    url = str(parameters.get("url", "")).strip()
    preset = str(parameters.get("preset", "")).strip().lower()
    text = str(parameters.get("text", "")).strip()
    volume = parameters.get("volume")

    try:
        if operation == "list" or operation == "discover":
            chromecasts, browser = _discover_cast_devices(timeout=4.0)
            if not chromecasts:
                msg = "No Google Home / Nest devices found on the local network."
                pychromecast.discovery.stop_discovery(browser)
                return msg

            dev_info = []
            for cc in chromecasts:
                dev_info.append(f"- **{cc.name}** ({cc.model_name})")
            
            pychromecast.discovery.stop_discovery(browser)
            return "Found the following Google Home / Nest devices on your network:\n" + "\n".join(dev_info)

        elif operation in ("play_music", "play_media", "play"):
            chromecasts, browser = _discover_cast_devices(timeout=4.0)
            target_cc = _find_device(chromecasts, device_name)

            if not target_cc:
                pychromecast.discovery.stop_discovery(browser)
                return f"Could not find Google device matching '{device_name}'."

            stream_url = url
            title = "JARVIS Music Stream"
            content_type = "audio/mp3"

            # Default to 'chill' preset if no URL or specific preset given
            if not preset and not stream_url:
                preset = "chill"

            if preset in PRESET_STATIONS:
                stream_url, title, content_type = PRESET_STATIONS[preset]

            if not stream_url:
                pychromecast.discovery.stop_discovery(browser)
                return "Please provide a valid stream URL or preset (lofi, chill, jazz, rock, pop) to play."

            target_cc.wait()
            mc = target_cc.media_controller
            mc.play_media(stream_url, content_type, title=title)
            mc.block_until_active(timeout=5)
            
            pychromecast.discovery.stop_discovery(browser)
            return f"Playing '{title}' on **{target_cc.name}**, sir."

        elif operation in ("announce", "speak", "tts"):
            if not text:
                return "Please provide the text message to announce."

            chromecasts, browser = _discover_cast_devices(timeout=4.0)
            target_cc = _find_device(chromecasts, device_name)

            if not target_cc:
                pychromecast.discovery.stop_discovery(browser)
                return f"Could not find Google device matching '{device_name}'."

            # Encode Google Translate TTS URL for Spanish/English
            encoded_text = urllib.parse.quote(text)
            tts_url = f"https://translate.google.com/translate_tts?ie=UTF-8&q={encoded_text}&tl=es&client=tw-ob"

            target_cc.wait()
            mc = target_cc.media_controller
            mc.play_media(tts_url, "audio/mp3", title="JARVIS Announcement")
            mc.block_until_active(timeout=5)

            pychromecast.discovery.stop_discovery(browser)
            return f"Announced message on **{target_cc.name}**: '{text}'"

        elif operation in ("stop", "pause"):
            chromecasts, browser = _discover_cast_devices(timeout=4.0)
            if device_name.lower() in ("all", "todos", "alle", "everywhere"):
                for cc in chromecasts:
                    try:
                        cc.wait()
                        cc.media_controller.stop()
                    except Exception:
                        pass
                pychromecast.discovery.stop_discovery(browser)
                return "Stopped playback on all Google devices."

            target_cc = _find_device(chromecasts, device_name)
            if target_cc:
                target_cc.wait()
                target_cc.media_controller.stop()
                pychromecast.discovery.stop_discovery(browser)
                return f"Stopped playback on **{target_cc.name}**."
            else:
                pychromecast.discovery.stop_discovery(browser)
                return f"Could not find Google device matching '{device_name}'."

        elif operation == "set_volume":
            if volume is None:
                return "Please specify a volume level (0 to 100)."

            vol_level = max(0.0, min(1.0, float(volume) / 100.0))
            chromecasts, browser = _discover_cast_devices(timeout=4.0)
            target_cc = _find_device(chromecasts, device_name)

            if target_cc:
                target_cc.wait()
                target_cc.set_volume(vol_level)
                pychromecast.discovery.stop_discovery(browser)
                return f"Set volume on **{target_cc.name}** to {int(vol_level * 100)}%."
            else:
                pychromecast.discovery.stop_discovery(browser)
                return f"Could not find Google device matching '{device_name}'."

        else:
            return f"Unknown operation '{operation}'. Supported operations: list, play_music, announce, stop, set_volume."

    except Exception as e:
        return f"Error executing Google Home action: {e}"


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "google_home",
    "description": "Control Google Home devices, Google Nest speakers, and Chromecasts on the local network. Discovers, lists, and streams audio to local Google devices. Operation 'list' discovers/lists local Google devices; 'play_music' plays music or radio streams across all devices or specific speakers; 'announce' broadcasts voice messages; 'stop' stops playback; 'set_volume' adjusts volume. Use this tool whenever the user asks to play music, play radio, or list/control Google Home or Nest devices in German ('Google Home', 'Geräte auflisten', 'Musik machen', 'Musik abspielen', 'Google Home Sachen'), Spanish ('dispositivos Google', 'poner música'), or English.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "operation": {
                "type": "STRING",
                "description": "Operation: 'play_music' (play audio/radio), 'list' (discover devices), 'announce' (voice message), 'stop' (stop playback), 'set_volume' (0-100)."
            },
            "device": {
                "type": "STRING",
                "description": "Target device or group name (e.g. 'all', 'Living Room speaker', 'Mini ofi', 'Dormitorio', 'Grupo de la casa'). Defaults to 'all'."
            },
            "preset": {
                "type": "STRING",
                "description": "Music genre preset for play_music: 'lofi', 'chill', 'jazz', 'rock', 'pop', 'top40'. Defaults to 'chill'."
            },
            "url": {
                "type": "STRING",
                "description": "Custom stream URL to play when operation is 'play_music'."
            },
            "text": {
                "type": "STRING",
                "description": "Text message to speak when operation is 'announce'."
            },
            "volume": {
                "type": "INTEGER",
                "description": "Volume level from 0 to 100 when operation is 'set_volume'."
            }
        },
        "required": ["operation"]
    },
    "handler": google_home_action,
}


if __name__ == "__main__":
    import sys
    op = sys.argv[1] if len(sys.argv) > 1 else "list"
    print(google_home_action({"operation": op, "device": "all"}))

