"""
Plugin discovery, validation, collision detection, and dispatch.
Mark LIV+ - compatible with original API.
FIX: registry._plugins = valid borraba lo registrado por ensure_registered()
"""
from __future__ import annotations

import importlib.util
import inspect
import re
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from memory.config_manager import get_plugin_enabled, get_plugin_config

_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")
_DEFAULT_PARAMS = {"type": "OBJECT", "properties": {}}
_BEHAVIORS = ("BLOCKING", "NON_BLOCKING")
_SCHEDULING = ("WHEN_IDLE", "SILENT", "INTERRUPT")

def _opt_upper(value, allowed: tuple[str,...]) -> Optional[str]:
    v = str(value or "").strip().upper()
    return v if v in allowed else None

@dataclass
class PluginRecord:
    name: str
    description: str = ""
    parameters: dict = field(default_factory=lambda: dict(_DEFAULT_PARAMS))
    run: Optional[Callable] = None
    file: str = ""
    valid: bool = False
    error: str = ""
    settings: Optional[dict] = None
    behavior: Optional[str] = None
    scheduling: Optional[str] = None
    dangerous: bool = False
    capabilities: list = field(default_factory=list)
    limits: list = field(default_factory=list)
    requires: list = field(default_factory=list)
    version: str = "0.0.0"

class PluginRegistry:
    def __init__(self, plugins: dict[str, PluginRecord], logger: Callable[[str], None],
                 notify: Callable[[str], None] | None = None):
        self._plugins = plugins
        self._all_records: list[PluginRecord] = []
        self._logger = logger
        self._notify = notify or (lambda _msg: None)

    def register(self, plugin_meta: dict, run_fn: Callable, file: str = "<dynamic>") -> PluginRecord:
        rec = _validate_meta(plugin_meta, run_fn, file)
        if not rec.valid:
            self._logger(f"Plugin rejected: {file} — {rec.error}")
            if rec not in self._all_records:
                self._all_records.append(rec)
            return rec
        if rec.name in self._plugins and self._plugins[rec.name].file!= file:
            dup = PluginRecord(name=rec.name, file=file,
                error=f"Name '{rec.name}' already used by '{self._plugins[rec.name].file}' — rejected.")
            self._all_records.append(dup)
            return dup
        self._plugins[rec.name] = rec
        if rec not in self._all_records:
            self._all_records.append(rec)
        self._logger(f"Plugin loaded: {rec.name} ({file})")
        try:
            from core.events import EventBus
            EventBus.emit("plugin.loaded", {"name": rec.name, "file": file})
        except Exception:
            pass
        return rec

    def clear(self):
        self._plugins.clear()
        self._all_records.clear()

    def get_tool_declarations(self) -> list[dict]:
        decls = []
        for name, rec in self._plugins.items():
            if get_plugin_enabled(name):
                decl = {"name": rec.name, "description": rec.description, "parameters": rec.parameters}
                if rec.behavior:
                    decl["behavior"] = rec.behavior
                decls.append(decl)
        return decls

    def has(self, name: str) -> bool:
        return name in self._plugins

    def scheduling(self, name: str) -> Optional[str]:
        rec = self._plugins.get(name)
        return rec.scheduling if rec else None

    def run(self, name: str, parameters: dict, player=None, session_memory=None) -> str:
        rec = self._plugins.get(name)
        if rec is None or not rec.valid:
            return f"Plugin '{name}' is not available."
        if not get_plugin_enabled(name):
            return f"The '{name}' plugin is currently disabled."

        if rec.dangerous:
            try:
                from core.confirm import is_token_valid, request_confirmation_banner
                token = parameters.get("_ui_confirm_token") or parameters.get("_ui_token")
                if not is_token_valid(name, token):
                    if player and hasattr(player, "show_confirmation_banner"):
                        try: player.show_confirmation_banner(name)
                        except Exception: pass
                    return request_confirmation_banner(name)
            except ImportError:
                return f"Sir, '{name}' requires confirmation but the confirmation system is missing."

        if rec.run and inspect.iscoroutinefunction(rec.run):
            self._logger(f"Plugin '{name}' is async but registry is sync — rejected")
            return f"Sir, plugin '{name}' is async and cannot run here."

        try:
            return _call_run(rec.run, parameters, player, session_memory) or "Done."
        except Exception as e:
            self._logger(f"Plugin '{name}' crashed during run(): {e}\n{traceback.format_exc()}")
            self._notify(f"Plugin '{name}' failed — see the console for details.")
            return f"Sir, the '{name}' plugin failed: {e}"

    def settings_schemas(self) -> list[dict]:
        seen: set[str] = set()
        out: list[dict] = []
        for name, rec in self._plugins.items():
            if not rec.settings or not get_plugin_enabled(name):
                continue
            ns = rec.settings.get("namespace") or rec.name
            if ns in seen: continue
            seen.add(ns)
            out.append({
                "plugin": rec.name, "namespace": ns,
                "title": rec.settings.get("title") or rec.name,
                "fields": rec.settings.get("fields", []),
                "values": get_plugin_config(ns),
                "action": rec.settings.get("action"),
            })
        return out

    def list_for_ui(self) -> list[dict]:
        out = []
        for rec in self._all_records:
            out.append({
                "name": rec.name, "description": rec.description, "file": rec.file,
                "valid": rec.valid, "error": rec.error,
                "enabled": get_plugin_enabled(rec.name) if rec.valid else False,
            })
        return out

def _call_run(run_fn, parameters, player, session_memory):
    sig = inspect.signature(run_fn)
    has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
    kwargs = {}
    if has_var_kw or "player" in sig.parameters:
        kwargs["player"] = player
    if has_var_kw or "session_memory" in sig.parameters:
        kwargs["session_memory"] = session_memory
    return run_fn(parameters, **kwargs)

def _validate_meta(plugin_meta, run_fn, filename: str) -> PluginRecord:
    if not isinstance(plugin_meta, dict):
        return PluginRecord(name=Path(filename).stem, file=filename, error="Missing PLUGIN dict constant.")
    name = plugin_meta.get("name")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        return PluginRecord(name=str(name or Path(filename).stem), file=filename,
            error="PLUGIN['name'] missing or not valid identifier.")
    description = plugin_meta.get("description")
    if not isinstance(description, str) or not description.strip():
        return PluginRecord(name=name, file=filename, error="PLUGIN['description'] missing or empty.")
    parameters = plugin_meta.get("parameters", _DEFAULT_PARAMS)
    if not isinstance(parameters, dict) or parameters.get("type")!= "OBJECT":
        return PluginRecord(name=name, file=filename, error="PLUGIN['parameters'] must be dict with \"type\": \"OBJECT\".")
    if not callable(run_fn):
        return PluginRecord(name=name, file=filename, error="Missing callable run(parameters,...) function.")
    return PluginRecord(
        name=name, description=description.strip(), parameters=parameters,
        run=run_fn, file=filename, valid=True,
        behavior=_opt_upper(plugin_meta.get("behavior"), _BEHAVIORS),
        scheduling=_opt_upper(plugin_meta.get("scheduling"), _SCHEDULING),
        dangerous=bool(plugin_meta.get("dangerous", False)),
        capabilities=list(plugin_meta.get("capabilities", [])),
        limits=list(plugin_meta.get("limits", [])),
        requires=list(plugin_meta.get("requires", [])),
        version=str(plugin_meta.get("version", "0.0.0")),
    )

def _validate(module, filename: str) -> PluginRecord:
    plugin_meta = getattr(module, "PLUGIN", None)
    run_fn = getattr(module, "run", None)
    rec = _validate_meta(plugin_meta, run_fn, filename)
    if not rec.valid:
        return rec
    settings = getattr(module, "PLUGIN_SETTINGS", None)
    if isinstance(settings, dict) and isinstance(settings.get("fields"), list):
        rec.settings = settings
    return rec

def _load_error(path: Path, plugins_dir: Path, exc: Exception) -> str:
    if isinstance(exc, ModuleNotFoundError):
        missing = (getattr(exc, "name", "") or "").split(".")
        if len(missing) == 2 and missing[0] == "plugins" and missing[1].startswith("_"):
            helper = missing[1] + ".py"
            return f"Needs shared file '{helper}' not in {plugins_dir.name}/. Download '{helper}' next to {path.name} and restart."
        if missing and missing[0] not in ("plugins",):
            return f"Needs package not installed: pip install {missing[0]}"
    return f"Failed to load: {exc}"

_REGISTRY: Optional[PluginRegistry] = None
def get_registry() -> PluginRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = PluginRegistry({}, print)
    return _REGISTRY

def discover_plugins(plugins_dir: Path, core_tool_names: set[str],
                      logger: Callable[[str], None] = print,
                      notify: Callable[[str], None] | None = None) -> PluginRegistry:
    plugins_dir.mkdir(parents=True, exist_ok=True)
    registry = PluginRegistry({}, logger, notify)
    global _REGISTRY
    _REGISTRY = registry

    # FIX: usar las mismas referencias que el registry, no dicts nuevos
    # Así lo que registra ensure_registered() no se pierde al hacer = valid al final
    valid: dict[str, PluginRecord] = registry._plugins
    all_records: list[PluginRecord] = registry._all_records
    files = sorted(plugins_dir.glob("*.py"), key=lambda p: p.name)

    for path in files:
