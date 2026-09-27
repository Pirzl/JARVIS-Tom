"""MARK LIV environment diagnostics.

Run with ``python mark.py doctor`` or ``python doctor.py``.
The doctor never prints secrets and never changes configuration.
"""
from __future__ import annotations

import importlib
import json
import platform
import ssl
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


class Doctor:
    def __init__(self):
        self.failures = 0
        self.warnings = 0

    def check(self, label: str, ok: bool, detail: str, warning: bool = False):
        if ok:
            mark, colour = "OK", "32"
        elif warning:
            mark, colour = "WARN", "33"
            self.warnings += 1
        else:
            mark, colour = "FAIL", "31"
            self.failures += 1
        print(f"\033[{colour}m[{mark}]\033[0m {label}: {detail}")

    def run(self) -> int:
        print(f"MARK LIV doctor | {platform.system()} | Python {sys.version.split()[0]}")
        print("This check is read-only; secrets are never displayed.\n")
        self.python()
        self.config()
        self.dependencies()
        self.audio()
        self.registries()
        self.tls()
        self.gmail()
        print(f"\nSummary: {self.failures} failure(s), {self.warnings} warning(s)")
        return 1 if self.failures else 0

    def python(self):
        version = sys.version_info[:2]
        ver_str = f"{version[0]}.{version[1]}"
        if version < (3, 11):
            self.check("Python", False,
                       f"{ver_str} is too old (supported: 3.11–3.13). Upgrade to Python 3.13.")
        elif version > (3, 13):
            self.check("Python", False,
                       f"{ver_str} is pre-release / unsupported (supported: 3.11–3.13). "
                       "Downgrade to Python 3.13 for full stability.",
                       warning=True)
        else:
            self.check("Python", True, f"{ver_str} (supported: 3.11–3.13)")

    def config(self):
        path = BASE_DIR / "config" / "api_keys.json"
        if not path.exists():
            self.check("Configuration", False, "config/api_keys.json is missing")
            return
        try:
            from memory.config_manager import load_api_keys
            data = load_api_keys()
            key = str(data.get("gemini_api_key", ""))
            version = data.get("config_version", 0)
            self.check("Configuration", isinstance(data, dict),
                       f"config_version={version}")
            self.check("Gemini API key", len(key) > 15, "configured" if len(key) > 15 else "missing or too short")
        except Exception as exc:
            self.check("Configuration", False, f"cannot parse JSON: {exc}")

    def dependencies(self):
        for label, module in (("Gemini SDK", "google.genai"),
                              ("PyQt6", "PyQt6"),
                              ("Audio backend", "sounddevice")):
            try:
                importlib.import_module(module)
                self.check(label, True, "importable")
            except Exception as exc:
                self.check(label, False, f"not available ({type(exc).__name__})")

    def audio(self):
        try:
            import sounddevice as sd
            devices = sd.query_devices()
            inputs = sum(int(d.get("max_input_channels", 0)) > 0 for d in devices)
            outputs = sum(int(d.get("max_output_channels", 0)) > 0 for d in devices)
            self.check("Audio input", inputs > 0, f"{inputs} device(s)")
            self.check("Audio output", outputs > 0, f"{outputs} device(s)")
        except Exception as exc:
            self.check("Audio devices", False, f"cannot query ({exc})")

    def registries(self):
        try:
            from core.action_loader import discover_actions
            actions = discover_actions(BASE_DIR / "actions", set(), logger=lambda _: None)
            rejected = sum(not item.valid for item in actions._all_records)
            self.check("Actions", rejected == 0, f"{len(actions.names())} loaded, {rejected} rejected")
        except Exception as exc:
            self.check("Actions", False, f"diagnostic failed ({exc})")
        try:
            from core.plugin_loader import discover_plugins
            plugins = discover_plugins(BASE_DIR / "plugins", set(), logger=lambda _: None)
            rejected = sum(not item.valid for item in plugins._all_records)
            self.check("Plugins", rejected == 0, f"{len(plugins._plugins)} loaded, {rejected} rejected")
        except Exception as exc:
            self.check("Plugins", False, f"diagnostic failed ({exc})")

    def tls(self):
        cert_dir = BASE_DIR / "config" / "certs"
        cert = cert_dir / "jarvis.crt"
        key = cert_dir / "jarvis.key"
        self.check("TLS certificate", cert.exists() and key.exists(),
                   "certificate and private key present" if cert.exists() and key.exists()
                   else "certificate or private key missing")
        if cert.exists():
            try:
                pem = cert.read_text(encoding="ascii")
                ssl.PEM_cert_to_DER_cert(pem)
                self.check("TLS certificate format", True, "readable")
            except Exception as exc:
                self.check("TLS certificate format", False, f"invalid ({exc})")

    def gmail(self):
        token = Path.home() / ".config" / "google" / "google_token.json"
        if not token.exists():
            self.check("Gmail OAuth", False, "token not found")
            return
        try:
            data = json.loads(token.read_text(encoding="utf-8"))
            scopes = set(data.get("scopes", []))
            ok = "https://www.googleapis.com/auth/gmail.readonly" in scopes
            self.check("Gmail OAuth", ok, "read-only scope present" if ok else "read-only scope missing")
        except Exception as exc:
            self.check("Gmail OAuth", False, f"token cannot be read ({exc})")


if __name__ == "__main__":
    raise SystemExit(Doctor().run())
