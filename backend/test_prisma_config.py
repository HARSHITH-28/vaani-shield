"""Offline validation tests for Prisma v3 optional request configuration."""

import json
import os
import unittest
from unittest.mock import patch

from services.prisma_config import (
    DEFAULT_SPEAKER_NAMES,
    PrismaConfigurationError,
    get_prisma_request_settings,
)


class PrismaRequestSettingsTests(unittest.TestCase):
    def test_defaults_match_vanishield_playground_configuration(self):
        with patch.dict(os.environ, {}, clear=True):
            settings = get_prisma_request_settings()

        self.assertEqual(settings.output_mode, "transcribe")
        self.assertEqual(settings.language_candidates, ("en-IN", "kn-IN", "hi-IN"))
        self.assertEqual(settings.language_code_for("auto"), "en-IN,kn-IN,hi-IN")
        self.assertIn("VaaniShield", settings.keyterms)
        self.assertIn("Financial advisor", settings.keyterms)
        self.assertLessEqual(len(settings.keyterms), 200)
        self.assertTrue(settings.topic)
        self.assertEqual(settings.speaker_names, DEFAULT_SPEAKER_NAMES)

    def test_overrides_are_read_from_environment(self):
        env = {
            "VAANISHIELD_PRISMA_FORMAT": "codemix",
            "VAANISHIELD_PRISMA_LANGUAGE_CANDIDATES": "hi-IN,en-IN",
            "VAANISHIELD_PRISMA_KEYTERMS": json.dumps(["Demo Bank", "UPI"]),
            "VAANISHIELD_PRISMA_TOPIC": "A custom banking topic.",
            "VAANISHIELD_PRISMA_SPEAKER_NAMES": json.dumps(["Arjun Sharma"]),
        }
        with patch.dict(os.environ, env, clear=True):
            settings = get_prisma_request_settings()

        self.assertEqual(settings.output_mode, "codemix")
        self.assertEqual(settings.language_code_for("auto"), "hi-IN,en-IN")
        self.assertEqual(settings.language_code_for("kn-IN"), "kn-IN")
        self.assertEqual(settings.keyterms, ("Demo Bank", "UPI"))
        self.assertEqual(settings.topic, "A custom banking topic.")
        self.assertEqual(settings.speaker_names, ("Arjun Sharma",))

    def test_blank_optional_hints_are_omitted_and_language_uses_full_auto(self):
        env = {
            "VAANISHIELD_PRISMA_LANGUAGE_CANDIDATES": "",
            "VAANISHIELD_PRISMA_KEYTERMS": "",
            "VAANISHIELD_PRISMA_TOPIC": "",
            "VAANISHIELD_PRISMA_SPEAKER_NAMES": "",
        }
        with patch.dict(os.environ, env, clear=True):
            settings = get_prisma_request_settings()

        self.assertEqual(settings.language_code_for("auto"), "indic-auto")
        self.assertEqual(settings.keyterms, ())
        self.assertIsNone(settings.topic)
        self.assertEqual(settings.speaker_names, ())

    def test_configured_lists_and_topic_enforce_provider_limits(self):
        cases = (
            {"VAANISHIELD_PRISMA_KEYTERMS": json.dumps([f"term-{i}" for i in range(201)])},
            {"VAANISHIELD_PRISMA_SPEAKER_NAMES": json.dumps(["A", "B", "C", "D", "E"])},
            {"VAANISHIELD_PRISMA_TOPIC": "x" * 1601},
            {"VAANISHIELD_PRISMA_LANGUAGE_CANDIDATES": "en-IN,kn-IN,hi-IN,ta-IN,ml-IN"},
        )
        for overrides in cases:
            with self.subTest(config=next(iter(overrides))):
                with patch.dict(os.environ, overrides, clear=True):
                    with self.assertRaises(PrismaConfigurationError):
                        get_prisma_request_settings()

    def test_invalid_json_and_output_mode_are_rejected(self):
        for overrides in (
            {"VAANISHIELD_PRISMA_KEYTERMS": "not-json"},
            {"VAANISHIELD_PRISMA_FORMAT": "streaming"},
        ):
            with self.subTest(config=next(iter(overrides))):
                with patch.dict(os.environ, overrides, clear=True):
                    with self.assertRaises(PrismaConfigurationError):
                        get_prisma_request_settings()


if __name__ == "__main__":
    unittest.main()
