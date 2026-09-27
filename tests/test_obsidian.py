import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from actions import obsidian


class ObsidianSearchTests(unittest.TestCase):
    def test_search_finds_accented_note_and_returns_location_and_date(self):
        vault = Path(tempfile.mkdtemp())
        note = vault / "projects" / "viajes" / "Reunion Madrid.md"
        note.parent.mkdir(parents=True)
        note.write_text(
            "---\n"
            "date: 2024-06-14\n"
            "location: Madrid\n"
            "---\n\n"
            "# Reunión de proyecto\n\n"
            "La reunión trató el lanzamiento del proyecto.\n",
            encoding="utf-8",
        )
        with patch.object(obsidian, "_vault", return_value=vault):
            result = obsidian._search_vault(vault, "dónde fue la reunión")
        self.assertIn("projects/viajes/Reunion Madrid.md", result)
        self.assertIn("location: Madrid", result)

        with patch.object(obsidian, "_vault", return_value=vault):
            result = obsidian._search_vault(vault, "cuándo fue la reunion")
        self.assertIn("projects/viajes/Reunion Madrid.md", result)
        self.assertIn("date: 2024-06-14", result)


if __name__ == "__main__":
    unittest.main()