import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from actions import osint_scan
from core.osint_store import OsintStore


class OsintScanTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.database = self.temp_dir / "cases.sqlite3"

    def test_requires_authorization_and_rejects_active_mode(self):
        missing = osint_scan.osint_scan({"target": "example.com", "database_path": str(self.database)})
        active = osint_scan.osint_scan({
            "target": "example.com",
            "authorization": "owned test domain",
            "mode": "active",
            "database_path": str(self.database),
        })
        self.assertIn("authorization note", missing)
        self.assertIn("Only passive", active)

    def test_normalizes_email_and_exports_local_evidence(self):
        result = osint_scan.osint_scan({
            "target": "Person@Example.COM",
            "authorization": "I am authorized to investigate this address.",
            "database_path": str(self.database),
            "case_name": "Email review",
        })
        self.assertIn("Passive OSINT case", result)
        case_id = result.split("case ", 1)[1].split(":", 1)[0]
        output = self.temp_dir / "report.json"
        exported = osint_scan.osint_scan({
            "operation": "export",
            "case_id": case_id,
            "database_path": str(self.database),
            "output_path": str(output),
        })
        self.assertIn("exported", exported)
        report = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(report["targets"][0]["normalized_value"], "person@example.com")
        self.assertEqual(report["findings"][0]["source"], "Target normalization")

    def test_domain_scan_stores_dns_rdap_and_certificate_findings(self):
        with patch.object(osint_scan, "_add_dns_findings", return_value=2) as dns, \
                patch.object(osint_scan, "_add_rdap_finding", return_value=1) as rdap, \
                patch.object(osint_scan, "_add_certificate_findings", return_value=3) as cert:
            result = osint_scan.osint_scan({
                "target": "https://Example.COM/path",
                "authorization": "I own this domain and authorize passive discovery.",
                "database_path": str(self.database),
            })

        self.assertIn("6 new finding(s)", result)
        dns.assert_called_once()
        rdap.assert_called_once()
        cert.assert_called_once()
        store = OsintStore(self.database)
        report = store.report(result.split("case ", 1)[1].split(":", 1)[0])
        self.assertEqual(report["targets"][0]["normalized_value"], "example.com")


if __name__ == "__main__":
    unittest.main()