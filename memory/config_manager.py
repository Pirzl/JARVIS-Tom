import json
import sys
from pathlib import Path

def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent

BASE_DIR = get_base_dir()
CONFIG_DIR = BASE_DIR / "config"
CONFIG_FILE = CONFIG_DIR / "api_keys.json"
CONFIG_VERSION = 1

# ── 4-value contract ─────────────────────────────────────────────
# OFF = 0 = hard guarantee, never auto-sleep
# Only valid: 10, 20, 30, 0
# Old 120, 900 etc are migrated to DEFAULT
ALLOWED_WAKE_SLEEP_TIMEOUTS = {10, 20, 30, 0}
DEFAULT_WAKE_SLEEP_TIMEOUT = 20.0

def _migrate_config(data: dict) -> dict:
    """Apply config migrations while preserving unknown/plugin-owned keys."""
    version = data.get("config_version", 0)
    try:
        version = int(version)
    except (TypeError, ValueError):
        version = 0

    if version < 1:
        data["config_version"] = CONFIG_VERSION
    elif version > CONFIG_VERSION:
        return data

    # Migrate old hidden timeouts (120, 900...) to DEFAULT
    if "wake_sleep_timeout" in data:
        raw = data["wake_sleep_timeout"]
        try:
            if isinstance(raw, str) and raw.strip().lower() in ("off","never","∞","inf"):
                iv = 0
            else:
                iv = int(float(raw))
            if iv not in ALLOWED_WAKE_SLEEP_TIMEOUTS:
                data["wake_sleep_timeout"] = float(DEFAULT_WAKE_SLEEP_TIMEOUT)
        except Exception:
            data["wake_sleep_timeout"] = float(DEFAULT_WAKE_SLEEP_TIMEOUT)

    return data

def ensure_config_dir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

def config_exists() -> bool:
    return CONFIG_FILE.exists()

def save_api_keys(gemini_api_key: str) -> None:
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data["gemini_api_key"] = gemini_api_key.strip()
    CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")

def load_api_keys() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        before_version = data.get("config_version")
        migrated = _migrate_config(data)
        if before_version!= migrated.get("config_version"):
            CONFIG_FILE.write_text(json.dumps(migrated, indent=4), encoding="utf-8")
        return migrated
    except Exception as e:
        print(f"❌ Failed to load api_keys.json: {e}")
        return {}

def get_gemini_key() -> str | None:
    return load_api_keys().get("gemini_api_key")

def is_configured() -> bool:
    key = get_gemini_key()
    return bool(key and len(key) > 15)

def get_assistant_name() -> str:
    return load_api_keys().get("assistant_name", "JARVIS") or "JARVIS"

def get_user_name() -> str:
    return load_api_keys().get("user_name", "")

def save_assistant_config(assistant_name: str, user_name: str) -> None:
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data["assistant_name"] = assistant_name.strip() or "JARVIS"
    data["user_name"] = user_name.strip()
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")

AVAILABLE_VOICES = ["Charon", "Puck", "Kore", "Fenrir", "Aoede"]
DEFAULT_VOICE = "Charon"

def get_voice() -> str:
    v = load_api_keys().get("voice_name", DEFAULT_VOICE) or DEFAULT_VOICE
    return v if v in AVAILABLE_VOICES else DEFAULT_VOICE

def save_voice(voice_name: str) -> None:
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    v = (voice_name or "").strip()
    data["voice_name"] = v if v in AVAILABLE_VOICES else DEFAULT_VOICE
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")

def get_wake_word_enabled() -> bool:
    return load_api_keys().get("wake_word_enabled", False)

def save_wake_word_enabled(enabled: bool) -> None:
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data["wake_word_enabled"] = bool(enabled)
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")

def get_wake_sleep_timeout() -> float:
    """Only 10,20,30,0 valid. OFF=0 = hard guarantee never auto-sleep.
       Old 120/900 etc -> DEFAULT (20). Missing -> DEFAULT."""
    raw = load_api_keys().get("wake_sleep_timeout", None)
    if raw is None:
        return float(DEFAULT_WAKE_SLEEP_TIMEOUT)
    if isinstance(raw, str):
        s = raw.strip().lower()
        if s in ("off","never","∞","inf","none",""):
            return 0.0
        try:
            raw = float(s)
        except (TypeError, ValueError):
            return float(DEFAULT_WAKE_SLEEP_TIMEOUT)
    try:
        iv = int(float(raw))
    except (TypeError, ValueError):
        return float(DEFAULT_WAKE_SLEEP_TIMEOUT)
    if iv not in ALLOWED_WAKE_SLEEP_TIMEOUTS:
        return float(DEFAULT_WAKE_SLEEP_TIMEOUT)
    return float(iv)

def save_wake_sleep_timeout(seconds: float) -> None:
    """Save only allowed values. OFF string -> 0."""
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    if isinstance(seconds, str):
        if seconds.strip().lower() in ("off","never","∞","inf"):
            seconds = 0
    try:
        iv = int(float(seconds))
    except (TypeError, ValueError):
        iv = int(DEFAULT_WAKE_SLEEP_TIMEOUT)
    if iv not in ALLOWED_WAKE_SLEEP_TIMEOUTS:
        iv = int(DEFAULT_WAKE_SLEEP_TIMEOUT)
    data["wake_sleep_timeout"] = float(iv)
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")

def get_push_to_talk_enabled() -> bool:
    return load_api_keys().get("push_to_talk_enabled", False)

def save_push_to_talk_enabled(enabled: bool) -> None:
    _save_flag("push_to_talk_enabled", enabled)

HUD_STYLES = ("face", "core")

def get_hud_style() -> str:
    v = str(load_api_keys().get("hud_style", "face")).strip().lower()
    return v if v in HUD_STYLES else "face"

def save_hud_style(style: str) -> None:
    s = str(style or "").strip().lower()
    _save_flag("hud_style", s if s in HUD_STYLES else "face")

def get_thinking_enabled() -> bool:
    return bool(load_api_keys().get("thinking_enabled", False))

def save_thinking_enabled(enabled: bool) -> None:
    _save_flag("thinking_enabled", enabled)

def get_turn_tuning() -> dict:
    cfg = load_api_keys().get("turn_tuning")
    cfg = cfg if isinstance(cfg, dict) else {}
    def _int(key, default, lo, hi):
        try:
            return max(lo, min(hi, int(cfg.get(key, default))))
        except (TypeError, ValueError):
            return default
    return {
        "enabled": bool(cfg.get("enabled", False)),
        "silence_ms": _int("silence_ms", 550, 200, 3000),
        "prefix_ms": _int("prefix_ms", 150, 0, 1000),
        "end_sensitivity": str(cfg.get("end_sensitivity", "high")).lower(),
        "start_sensitivity": str(cfg.get("start_sensitivity", "default")).lower(),
    }

def save_turn_tuning(values: dict) -> None:
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    cur = data.get("turn_tuning")
    cur = dict(cur) if isinstance(cur, dict) else {}
    cur.update(values or {})
    data["turn_tuning"] = cur
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")

def get_proactive_audio_enabled() -> bool:
    return bool(load_api_keys().get("proactive_audio", True))

def save_proactive_audio_enabled(enabled: bool) -> None:
    _save_flag("proactive_audio", enabled)

MEDIA_RESOLUTIONS = ("default", "low", "medium", "high")

def get_media_resolution() -> str:
    v = str(load_api_keys().get("media_resolution", "medium")).strip().lower()
    return v if v in MEDIA_RESOLUTIONS else "medium"

def save_media_resolution(value: str) -> None:
    v = str(value or "").strip().lower()
    _save_flag("media_resolution", v if v in MEDIA_RESOLUTIONS else "medium")

def _save_flag(key: str, value) -> None:
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data[key] = bool(value) if isinstance(value, bool) else value
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")

def get_brief_enabled() -> bool:
    return load_api_keys().get("morning_brief_enabled", True)

def save_brief_enabled(enabled: bool) -> None:
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data["morning_brief_enabled"] = enabled
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")

def _patch_config(**fields) -> None:
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data.update(fields)
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")

def get_input_device() -> str:
    return (load_api_keys().get("input_device", "") or "").strip()

def save_input_device(name: str) -> None:
    _patch_config(input_device=(name or "").strip())

def get_output_device() -> str:
    return (load_api_keys().get("output_device", "") or "").strip()

def save_output_device(name: str) -> None:
    _patch_config(output_device=(name or "").strip())

def get_plugin_enabled(plugin_name: str) -> bool:
    return load_api_keys().get("plugins_enabled", {}).get(plugin_name, True)

def get_plugin_config(namespace: str) -> dict:
    cfg = load_api_keys().get("plugin_config")
    val = cfg.get(namespace) if isinstance(cfg, dict) else None
    return dict(val) if isinstance(val, dict) else {}

def get_plugin_setting(namespace: str, key: str, default=None):
    return get_plugin_config(namespace).get(key, default)

def save_plugin_config(namespace: str, values: dict) -> None:
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    pc = data.get("plugin_config")
    if not isinstance(pc, dict):
        pc = {}
    cur = pc.get(namespace)
    if not isinstance(cur, dict):
        cur = {}
    cur.update(values)
    pc[namespace] = cur
    data["plugin_config"] = pc
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")

def save_plugin_enabled(plugin_name: str, enabled: bool) -> None:
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    plugins_cfg = data.get("plugins_enabled")
    if not isinstance(plugins_cfg, dict):
        plugins_cfg = {}
    plugins_cfg[plugin_name] = enabled
    data["plugins_enabled"] = plugins_cfg
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")

def get_file_access_roots() -> list[str]:
    roots = load_api_keys().get("file_access_roots", [])
    return [str(root) for root in roots] if isinstance(roots, list) else []

def save_file_access_root(path: str) -> None:
    root = str(path or "").strip()
    if not root:
        return
    roots = get_file_access_roots()
    if root not in roots:
        roots.append(root)
    _patch_config(file_access_roots=roots)
