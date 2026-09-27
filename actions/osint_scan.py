"""Authorized, passive-only OSINT collection for domains, IPs, emails, and phones."""

from __future__ import annotations

import ipaddress
import json
import re
import socket
from pathlib import Path
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from core.osint_store import DEFAULT_DB, OsintStore

_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$", re.IGNORECASE)
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE_RE = re.compile(r"^\+[1-9]\d{6,14}$")
_TIMEOUT = 8


def _normalize_target(raw: str) -> tuple[str, str]:
    value = raw.strip()
    if not value:
        raise ValueError("Target cannot be empty")
    if _EMAIL_RE.match(value):
        return "email", value.lower()
    if _PHONE_RE.match(re.sub(r"[\s().-]", "", value)):
        return "phone", re.sub(r"[\s().-]", "", value)
    try:
        return "ip", str(ipaddress.ip_address(value))
    except ValueError:
        pass
    parsed = urlparse(value if "://" in value else f"https://{value}")
    domain = (parsed.hostname or "").rstrip(".").lower()
    if not _DOMAIN_RE.match(domain):
        raise ValueError("Target must be a domain, IP address, email, or international phone number")
    return "domain", domain


def _fetch_json(url: str) -> dict | list | None:
    request = Request(url, headers={"User-Agent": "JARVIS-passive-osint/1.0"})
    with urlopen(request, timeout=_TIMEOUT) as response:
        if response.status >= 400:
            return None
        return json.loads(response.read().decode("utf-8"))


def _add_dns_findings(store: OsintStore, case_id: str, domain: str) -> int:
    count = 0
    try:
        addresses = sorted({item[4][0] for item in socket.getaddrinfo(domain, 443, type=socket.SOCK_STREAM)})
    except OSError:
        addresses = []
    for address in addresses:
        store.add_finding(case_id, "ip", address, "DNS A/AAAA", f"https://{domain}", "HIGH", raw_data={"domain": domain})
        count += 1
    return count


def _add_rdap_finding(store: OsintStore, case_id: str, entity_type: str, value: str) -> int:
    endpoint = f"https://rdap.org/{'domain' if entity_type == 'domain' else 'ip'}/{quote(value, safe='')}"
    try:
        data = _fetch_json(endpoint)
    except Exception:
        return 0
    if not data:
        return 0
    store.add_finding(case_id, entity_type, value, "RDAP", endpoint, "HIGH", raw_data={
        "handle": data.get("handle"),
        "name": data.get("name"),
        "status": data.get("status", []),
        "ldhName": data.get("ldhName"),
    })
    return 1


def _add_certificate_findings(store: OsintStore, case_id: str, domain: str) -> int:
    endpoint = f"https://crt.sh/?q=%25.{quote(domain)}&output=json"
    try:
        records = _fetch_json(endpoint)
    except Exception:
        return 0
    if not isinstance(records, list):
        return 0
    names: set[str] = set()
    for record in records[:100]:
        for name in str(record.get("name_value", "")).splitlines():
            normalized = name.strip().lower().lstrip("*.")
            if _DOMAIN_RE.match(normalized):
                names.add(normalized)
    for name in sorted(names):
        store.add_finding(case_id, "domain", name, "Certificate Transparency", endpoint, "MEDIUM", raw_data={"base_domain": domain})
    return len(names)


def _add_phone_finding(store: OsintStore, case_id: str, phone: str) -> int:
    try:
        import phonenumbers
        parsed = phonenumbers.parse(phone, None)
        if not phonenumbers.is_possible_number(parsed):
            return 0
        data = {
            "country_code": parsed.country_code,
            "national_number": str(parsed.national_number),
            "region": phonenumbers.region_code_for_number(parsed),
            "valid": phonenumbers.is_valid_number(parsed),
        }
    except Exception:
        data = {"format": phone, "parser": "unavailable"}
    store.add_finding(case_id, "phone", phone, "Local phone normalization", "", "MEDIUM", raw_data=data)
    return 1


def _scan_target(store: OsintStore, case_id: str, entity_type: str, value: str) -> int:
    store.add_target(case_id, entity_type, value, value)
    if entity_type == "domain":
        return _add_dns_findings(store, case_id, value) + _add_rdap_finding(store, case_id, entity_type, value) + _add_certificate_findings(store, case_id, value)
    if entity_type == "ip":
        return _add_rdap_finding(store, case_id, entity_type, value)
    if entity_type == "phone":
        return _add_phone_finding(store, case_id, value)
    store.add_finding(case_id, entity_type, value, "Target normalization", "", "MEDIUM", raw_data={"note": "No external lookup performed for this entity type."})
    return 1


def osint_scan(parameters: dict, player=None, speak=None) -> str:
    params = parameters or {}
    operation = str(params.get("operation", "scan")).strip().lower()
    case_id = str(params.get("case_id", "")).strip()
    db_path = Path(str(params.get("database_path", DEFAULT_DB))).expanduser()
    store = OsintStore(db_path)

    def progress(message: str) -> None:
        if player and hasattr(player, "write_log"):
            player.write_log(f"OSINT: {message}")

    if operation == "export":
        if not case_id:
            return "Please provide a case_id to export."
        output_value = str(params.get("output_path", "")).strip()
        output = Path(output_value).expanduser() if output_value else None
        if output is None:
            output = db_path.parent / f"osint-{case_id}.json"
        try:
            progress(f"Exporting case {case_id}...")
            return f"OSINT report exported to {store.export_json(case_id, output)}"
        except ValueError as exc:
            return str(exc)

    target = str(params.get("target", "")).strip()
    authorization = str(params.get("authorization", "")).strip()
    if not authorization:
        return "Please provide an authorization note, such as: I own this domain and authorize passive discovery."
    if str(params.get("mode", "passive")).lower() != "passive":
        return "Only passive OSINT mode is enabled. Active scanning requires a separate reviewed feature."
    try:
        entity_type, normalized = _normalize_target(target)
    except ValueError as exc:
        return f"Invalid OSINT target: {exc}"

    if not case_id:
        case_id = store.create_case(str(params.get("case_name", f"Passive OSINT: {normalized}")), authorization)
    try:
        progress(f"Case {case_id} started for {entity_type} {normalized} (passive mode).")
        if entity_type == "domain":
            progress("Collecting DNS, RDAP, and Certificate Transparency evidence...")
        elif entity_type == "ip":
            progress("Collecting RDAP evidence...")
        elif entity_type == "phone":
            progress("Normalizing phone metadata locally...")
        else:
            progress("Recording normalized target and provenance...")
        findings = _scan_target(store, case_id, entity_type, normalized)
        report = store.report(case_id)
    except ValueError as exc:
        return str(exc)
    message = f"Passive OSINT case {case_id}: {findings} new finding(s), {len(report['findings'])} total."
    if speak:
        speak(message)
    return message + f"\nUse operation=export with case_id={case_id} to save the evidence report."


TOOL = {
    "name": "osint_scan",
    "description": "Performs authorized passive OSINT on a domain, IP, email, or phone number and stores source-linked evidence locally. No active scanning.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "operation": {"type": "STRING", "description": "scan or export (default: scan)"},
            "target": {"type": "STRING", "description": "Domain, IP address, email address, or international phone number"},
            "authorization": {"type": "STRING", "description": "User authorization note for this passive investigation"},
            "case_name": {"type": "STRING", "description": "Optional case name"},
            "case_id": {"type": "STRING", "description": "Existing case ID for additional scans or export"},
            "mode": {"type": "STRING", "description": "passive only"},
            "database_path": {"type": "STRING", "description": "Optional local SQLite database path"},
            "output_path": {"type": "STRING", "description": "Optional JSON export path"},
        },
        "required": ["target", "authorization"],
    },
    "handler": osint_scan,
}