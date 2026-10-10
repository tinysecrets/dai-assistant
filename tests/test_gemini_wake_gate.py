"""Regression tests for the Gemini wake gate without loading audio dependencies."""

import ast
import os
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "bin" / "dai-gemini.py"


def load_wake_gate():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    names = {"_W_GREET", "_W_NAMES", "WAKE_PATTERNS", "WAKE_MAX_LEAD_WORDS", "extract_wake_and_command"}
    nodes = []
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Name) and t.id in names for t in targets):
                nodes.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in names:
            nodes.append(node)
    namespace = {"os": os, "re": re}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), "exec"), namespace)
    return namespace["extract_wake_and_command"]


class TestGeminiWakeGate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.extract = staticmethod(load_wake_gate())

    def test_explicit_summons_activate(self):
        for phrase in ("hey day open browser", "okay dai check status", "come here day"):
            with self.subTest(phrase=phrase):
                woke, command = self.extract(phrase)
                self.assertTrue(woke)
                self.assertTrue(command)

    def test_ambient_mentions_do_not_activate(self):
        for phrase in ("we had a great day yesterday", "save the date", "the day was long"):
            with self.subTest(phrase=phrase):
                self.assertFalse(self.extract(phrase)[0])

    def test_summons_buried_in_sentence_do_not_activate(self):
        self.assertFalse(self.extract("I was talking about lunch and hey day")[0])

    def test_short_transcription_lead_in_is_tolerated(self):
        woke, command = self.extract("so hey day check status")
        self.assertTrue(woke)
        self.assertEqual(command, "check status")


if __name__ == "__main__":
    unittest.main()
