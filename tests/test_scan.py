import copy
import contextlib
import importlib.util
import io
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


scan = load_module("bu_tools_scan", ROOT / "register" / "scan.py")


def config():
    cfg = copy.deepcopy(scan.DEFAULTS)
    cfg["terms"] = {"actual(ly)": r"\bactual(?:ly)?\b"}
    return cfg


class QuotationTests(unittest.TestCase):
    def test_contraction_apostrophes_do_not_create_quote(self):
        rep = scan.scan_text(
            "x.md", "It's actually useful when it isn't overdone.", config())
        self.assertEqual([(h.match, h.exempt) for h in rep.hits],
                         [("actually", None)])

    def test_straight_curly_and_q_quotes_are_exempt(self):
        for text in ("She called it 'actually useful'.",
                     "She called it ‘actually useful’.",
                     "She called it <q>actually useful</q>."):
            with self.subTest(text=text):
                rep = scan.scan_text("x.md", text, config())
                self.assertEqual(len(rep.hits), 1)
                self.assertEqual(rep.hits[0].exempt, "inside quotation")

    def test_html_attributes_are_not_prose(self):
        rep = scan.scan_text(
            "x.md", '<div data-description="actually useful">Plain body.</div>', config())
        self.assertEqual(rep.hits, [])


class ScopeTests(unittest.TestCase):
    def test_frontmatter_is_excluded_from_hits_and_word_count(self):
        raw = "---\ntitle: Actually useful\n---\nPlain body.\n"
        rep = scan.scan_text("x.md", raw, config())
        self.assertEqual(rep.words, 2)
        self.assertEqual(rep.hits, [])

    def test_since_scope_uses_only_selected_lines_for_density(self):
        raw = "actually\n" + ("ordinary prose " * 500)
        rep = scan.scan_text("x.md", raw, config(), {1})
        self.assertEqual(rep.words, 1)
        self.assertEqual(len(rep.live), 1)
        self.assertEqual(rep.rate, 1000.0)

    def test_missing_configured_path_is_an_error(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = config()
            cfg["paths"] = ["missing"]
            with self.assertRaises(scan.ScanError):
                list(scan.iter_files(root, cfg))


class PolicyTests(unittest.TestCase):
    def test_zero_density_is_report_only(self):
        cfg = config()
        rep = scan.scan_text("x.md", "actually", cfg)
        self.assertEqual(scan.report_failures(rep, cfg), [])

    def test_live_hits_can_be_an_explicit_gate(self):
        cfg = config()
        cfg["fail_on_live_hits"] = True
        rep = scan.scan_text("x.md", "actually", cfg)
        self.assertIn("1 live hit(s)", scan.report_failures(rep, cfg))

    def test_zero_cadence_limit_can_be_enforced(self):
        cfg = config()
        cfg["cadence"]["limits"] = {"ly_adverbs": 0}
        rep = scan.scan_text("x.md", "It moved quietly.", cfg)
        self.assertIn("ly_adverbs 1 > 0", scan.report_failures(rep, cfg))

    def test_invalid_regex_gets_a_config_error(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "config.json"
            data = config()
            data["terms"] = {"broken": "["}
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(scan.ConfigError, "invalid regex"):
                scan.load_config(str(path))

    def test_clean_files_are_included_in_aggregate_word_count(self):
        dirty = scan.scan_text("dirty.md", "actually", config())
        clean = scan.scan_text("clean.md", "ordinary prose " * 500, config())
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            scan.emit_text([dirty, clean], config())
        self.assertIn("across 1001 words", output.getvalue())


class CliTests(unittest.TestCase):
    def run_scan(self, fail_on_live_hits):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            (root / "content").mkdir()
            (root / "content" / "entry.md").write_text(
                "This is actually prose.", encoding="utf-8")
            data = config()
            data["paths"] = ["content"]
            data["fail_on_live_hits"] = fail_on_live_hits
            config_path = root / "config.json"
            config_path.write_text(json.dumps(data), encoding="utf-8")
            return subprocess.run(
                [sys.executable, str(ROOT / "register" / "scan.py"),
                 "--root", str(root), "--config", str(config_path), "--json"],
                capture_output=True, text=True)

    def test_report_only_scan_returns_zero_and_records_the_hit(self):
        result = self.run_scan(False)
        self.assertEqual(result.returncode, 0, result.stderr)
        record = json.loads(result.stdout)
        self.assertEqual(record["live_hits"], 1)
        self.assertFalse(record["failed"])

    def test_gated_scan_returns_one_with_a_reason(self):
        result = self.run_scan(True)
        self.assertEqual(result.returncode, 1, result.stderr)
        record = json.loads(result.stdout)
        self.assertTrue(record["failed"])
        self.assertEqual(record["files"][0]["failures"], ["1 live hit(s)"])


if __name__ == "__main__":
    unittest.main()
