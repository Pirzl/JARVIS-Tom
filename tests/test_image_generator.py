import unittest
from unittest.mock import patch

from actions import image_generator


class _QuotaClient:
    class _Models:
        calls = 0

        def generate_content(self, **kwargs):
            self.calls += 1
            raise RuntimeError("429 RESOURCE_EXHAUSTED: image quota exceeded")

    def __init__(self):
        self.models = self._Models()


class ImageGeneratorTests(unittest.TestCase):
    def test_quota_error_is_not_retried(self):
        client = _QuotaClient()
        image_generator._quota_blocked_until = 0
        with patch.object(image_generator, "get_gemini_key", return_value="test-key"), \
                patch.object(image_generator.gemini, "client", return_value=client):
            first = image_generator.generate_image({"prompt": "a beach"})
            second = image_generator.generate_image({"prompt": "a beach"})

        self.assertIn("out of quota (429)", first)
        self.assertEqual(first, second)
        self.assertEqual(client.models.calls, 1)


if __name__ == "__main__":
    unittest.main()