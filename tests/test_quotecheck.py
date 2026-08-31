import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


quotecheck = load_module(
    "bu_tools_quotecheck", ROOT / "register" / "quotecheck.py")


class NormalisationTests(unittest.TestCase):
    def test_unicode_is_preserved_and_accents_are_ocr_tolerant(self):
        self.assertEqual(quotecheck.norm("Café déjà vu"), "cafedejavu")
        self.assertEqual(quotecheck.norm("Ελλάδα"), "ελλαδα")

    def test_repeated_shingles_are_distinct(self):
        text = "one two three four five one two three four five"
        all_shingles = quotecheck.shingles(text)
        distinct = quotecheck.distinct_shingles(text)
        self.assertGreater(len(all_shingles), len(distinct))
        self.assertEqual(distinct.count("onetwothreefourfive"), 1)


class QuotationTests(unittest.TestCase):
    def test_mixed_quote_marks_are_not_accepted(self):
        text = '“This quotation contains enough words to pass the minimum threshold"'
        self.assertIsNone(quotecheck.QUOTE.search(text))

    def test_long_quotation_is_not_silently_ignored(self):
        text = '"' + ("long quotation words " * 80) + '"'
        self.assertIsNotNone(quotecheck.QUOTE.search(text))

    def test_contractions_do_not_create_single_quote_spans(self):
        text = "It's actually useful when it isn't overdone."
        self.assertEqual(quotecheck.quotation_spans(text), [])

    def test_html_attribute_quotes_are_not_extracted(self):
        text = ('<div data-note="This quotation-shaped attribute contains enough '
                'words to clear every extraction threshold">Body.</div>')
        self.assertIsNone(quotecheck.QUOTE.search(quotecheck.mask_tags(text)))


class SourceIndexTests(unittest.TestCase):
    def test_best_continuation_checks_every_occurrence(self):
        quotation = "the same sufficiently long quotation appears in both held sources"
        continuation = " and this exact continuation belongs to the second source only"
        index = quotecheck.SourceIndex([
            ("a.md", quotation + " followed by unrelated source wording"),
            ("b.md", quotation + continuation),
        ])
        needle = quotecheck.norm(quotation)
        self.assertEqual(index.source_names(needle), ["a.md", "b.md"])
        best, shared = index.best_continuation(needle, quotecheck.norm(continuation))
        self.assertEqual(best[0], "b.md")
        self.assertGreaterEqual(shared, quotecheck.SPILL_MIN_MATCH)

    def test_missing_configured_path_is_an_error(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(quotecheck.CheckError):
                list(quotecheck.walk(root, ["missing"], [".md"]))


class IntegrationTests(unittest.TestCase):
    def test_spill_uses_the_matching_occurrence_not_the_first(self):
        quotation = "the same sufficiently long quotation appears in both held sources"
        continuation = " and this exact continuation belongs to the second source only"
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            (root / "docs").mkdir()
            (root / "sources").mkdir()
            (root / "docs" / "entry.md").write_text(
                f'"{quotation}"{continuation}.', encoding="utf-8")
            (root / "sources" / "a.md").write_text(
                quotation + " followed by unrelated source wording", encoding="utf-8")
            (root / "sources" / "b.md").write_text(
                quotation + continuation, encoding="utf-8")
            config = {
                "terms": {},
                "quotecheck": {"documents": ["docs"], "sources": ["sources"]},
            }
            config_path = root / "config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(ROOT / "register" / "quotecheck.py"),
                 "--root", str(root), "--config", str(config_path)],
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("SPILL", result.stdout)
            self.assertIn("sources/b.md", result.stdout)


if __name__ == "__main__":
    unittest.main()
