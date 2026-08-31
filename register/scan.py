#!/usr/bin/env python3
"""register-scan — AI-tell and cadence scanner for repo prose.

Config-driven so one engine serves every project: the rules live in a JSON file
in the consuming repo, the engine lives here.

    python3 tools/shared/register/scan.py --config tools/register.config.json
    python3 tools/shared/register/scan.py --config <cfg> --paths frontier_log/
    python3 tools/shared/register/scan.py --config <cfg> --since origin/master
    python3 tools/shared/register/scan.py --config <cfg> --json

--since implements the standing discipline that additions are measured against the
base branch rather than against the corpus, which is what separates "this pass did
this" from "the house has always done this".

Exit codes: 0 clean · 1 findings above threshold · 2 usage/config error.
Standard library only, deliberately: consuming repos must not gain a dependency.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from itertools import zip_longest

# ---------------------------------------------------------------- config

DEFAULTS = {
    "paths": ["."],
    "extensions": [".md"],
    "exclude": [],
    "terms": {},
    "exempt_quoted": True,
    "exempt_patterns": [],
    "exempt_terms_in_files": {},
    "cadence": {"limits": {}},
    "density_per_1k_max": 0.0,       # 0.0 = report only
    "fail_on_live_hits": False,
}

CADENCE_LEGACY_KEYS = {
    "antithetical_max": "antithetical_frames",
    "paired_short_closes_max": "paired_short_closes",
    "anaphora_max": "anaphora_adjacent",
    "isolated_short_paras_max": "isolated_short_paras",
    "ly_adverbs_max": "ly_adverbs",
}
CADENCE_MEASURES = set(CADENCE_LEGACY_KEYS.values())


class ConfigError(ValueError):
    """Invalid configuration supplied by a consumer."""


class ScanError(RuntimeError):
    """The requested scan could not be completed safely."""


def _string_list(cfg: dict, key: str) -> None:
    value = cfg.get(key)
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise ConfigError(f"{key} must be a list of strings")


def _compile(pattern: str, label: str) -> None:
    try:
        re.compile(pattern)
    except re.error as exc:
        raise ConfigError(f"invalid regex for {label}: {exc}") from exc


def validate_config(cfg: dict) -> None:
    for key in ("paths", "extensions", "exclude", "exempt_patterns"):
        _string_list(cfg, key)
    if not cfg["paths"]:
        raise ConfigError("paths must contain at least one file or directory")
    if not isinstance(cfg.get("terms"), dict) or not cfg["terms"]:
        raise ConfigError("config defines no terms — nothing to scan")
    if not all(isinstance(k, str) and isinstance(v, str)
               for k, v in cfg["terms"].items()):
        raise ConfigError("terms must map names to regex strings")
    exemptions = cfg.get("exempt_terms_in_files")
    if not isinstance(exemptions, dict) or not all(
            isinstance(k, str) and isinstance(v, list)
            and all(isinstance(x, str) for x in v)
            for k, v in exemptions.items()):
        raise ConfigError("exempt_terms_in_files must map path regexes to term-name lists")
    if not isinstance(cfg.get("exempt_quoted"), bool):
        raise ConfigError("exempt_quoted must be true or false")
    if not isinstance(cfg.get("fail_on_live_hits"), bool):
        raise ConfigError("fail_on_live_hits must be true or false")
    density = cfg.get("density_per_1k_max")
    if isinstance(density, bool) or not isinstance(density, (int, float)) or density < 0:
        raise ConfigError("density_per_1k_max must be a non-negative number")

    cadence = cfg.get("cadence")
    if not isinstance(cadence, dict):
        raise ConfigError("cadence must be an object")
    limits = cadence.get("limits", {})
    if not isinstance(limits, dict):
        raise ConfigError("cadence.limits must be an object")
    unknown = set(limits) - CADENCE_MEASURES
    if unknown:
        raise ConfigError(f"unknown cadence limit(s): {', '.join(sorted(unknown))}")
    for name, value in limits.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise ConfigError(f"cadence.limits.{name} must be a non-negative number")
    for legacy in CADENCE_LEGACY_KEYS:
        if legacy not in cadence:
            continue
        value = cadence[legacy]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise ConfigError(f"cadence.{legacy} must be a non-negative number")

    for name, pattern in cfg["terms"].items():
        _compile(pattern, f"terms.{name}")
    for i, pattern in enumerate(cfg["exclude"]):
        _compile(pattern, f"exclude[{i}]")
    for i, pattern in enumerate(cfg["exempt_patterns"]):
        _compile(pattern, f"exempt_patterns[{i}]")
    for pattern in exemptions:
        _compile(pattern, f"exempt_terms_in_files.{pattern}")


def load_config(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except FileNotFoundError as exc:
        raise ConfigError(f"config not found: {path}") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read config {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"config is not valid JSON: {exc}") from exc
    if not isinstance(cfg, dict):
        raise ConfigError("config root must be an object")
    merged = dict(DEFAULTS)
    merged.update(cfg)
    merged["cadence"] = {**DEFAULTS["cadence"], **cfg.get("cadence", {})}
    validate_config(merged)
    return merged


# ---------------------------------------------------------------- text model

WORD = re.compile(r"\b[\w'-]+\b")
# Straight and curly pairs, plus <q>. Straight single quotes need word
# boundaries at both ends: without them, the apostrophes in "it's ... isn't"
# become a fake quotation and silently exempt everything between them.
QUOTED = re.compile(
    r'''"[^"\n]*"|“[^”\n]*”|‘[^’\n]*’|(?<![\w’])'[^'\n]{2,}'(?![\w’])|<q\b[^>]*>.*?</q>''',
    re.I,
)
FRONTMATTER = re.compile(
    r"\A(?:\ufeff)?---[ \t]*\r?\n.*?\r?\n---[ \t]*(?=\r?\n|\Z)", re.S)
TAG = re.compile(r"<[^>]*>")
Q_TAG = re.compile(r"<q\b[^>]*>.*?</q>", re.I)


def mask_tags(s: str) -> str:
    """Blank out HTML tags, preserving offsets — attribute quotes otherwise
    offset every quotation pair after them and make quoted text read as own
    voice."""
    return TAG.sub(lambda m: " " * len(m.group(0)), s)


def mask_frontmatter(s: str) -> str:
    """Blank frontmatter while preserving every offset and line number."""
    return FRONTMATTER.sub(lambda m: re.sub(r"[^\r\n]", " ", m.group(0)), s)


def words(text: str) -> int:
    return len(WORD.findall(text))


def quoted_spans(line: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in QUOTED.finditer(line)]


def in_any(pos: int, spans: list[tuple[int, int]]) -> bool:
    return any(a <= pos < b for a, b in spans)


# ---------------------------------------------------------------- findings

@dataclass
class Hit:
    path: str
    line: int
    term: str
    match: str
    context: str
    exempt: str | None = None


@dataclass
class FileReport:
    path: str
    words: int = 0
    hits: list[Hit] = field(default_factory=list)
    cadence: dict = field(default_factory=dict)

    @property
    def live(self) -> list[Hit]:
        return [h for h in self.hits if not h.exempt]

    @property
    def rate(self) -> float:
        return len(self.live) / self.words * 1000 if self.words else 0.0


# ---------------------------------------------------------------- scanning

def cadence_measures(body: str) -> dict:
    paras = [p for p in re.split(r"\n\s*\n", body)
             if p.strip() and not p.strip().startswith(("#", "|", "---", ">"))]
    anti = len(re.findall(r"\brather than\b|\bnot (?:a|an|the|because)\b[^.]{0,80}?\bbut\b", body, re.I))
    stop = {"only", "early", "likely", "family", "apply", "reply", "supply", "fully"}
    advs = [a for a in re.findall(r"\b\w+ly\b", body) if a.lower() not in stop]
    pairs = anaphora = 0
    for p in paras:
        sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+", p.strip()) if s.strip()]
        if len(sents) >= 2 and words(sents[-1]) <= 10 and words(sents[-2]) <= 10:
            pairs += 1
        starts = [" ".join(WORD.findall(s)[:2]).lower() for s in sents]
        anaphora += sum(1 for i in range(len(starts) - 1)
                        if starts[i] and starts[i] == starts[i + 1])
    return {
        "antithetical_frames": anti,
        "ly_adverbs": len(advs),
        "isolated_short_paras": sum(1 for p in paras if words(p) <= 12),
        "paired_short_closes": pairs,
        "anaphora_adjacent": anaphora,
    }


def scan_text(path: str, raw: str, cfg: dict,
              only_lines: set[int] | None = None) -> FileReport:
    visible = mask_tags(mask_frontmatter(raw))
    visible_lines = visible.splitlines()
    raw_lines = raw.splitlines()
    if only_lines is None:
        scoped = visible
    else:
        # Keep line boundaries so disconnected diff hunks cannot become one
        # synthetic paragraph or sentence during cadence measurement.
        scoped = "\n".join(line if i in only_lines else ""
                           for i, line in enumerate(visible_lines, 1))
    rep = FileReport(path=path, words=words(scoped))
    exempt_pats = [re.compile(p, re.I) for p in cfg["exempt_patterns"]]
    file_exempt = {t for pat, terms in cfg["exempt_terms_in_files"].items()
                   if re.search(pat, path) for t in terms}
    compiled = {name: re.compile(rx, re.I) for name, rx in cfg["terms"].items()}

    for i, (line, searchable) in enumerate(
            zip_longest(raw_lines, visible_lines, fillvalue=""), 1):
        if only_lines is not None and i not in only_lines:
            continue
        spans = (quoted_spans(searchable)
                 + [(m.start(), m.end()) for m in Q_TAG.finditer(line)]
                 if cfg["exempt_quoted"] else [])
        line_exempt = next((p.pattern for p in exempt_pats if p.search(line)), None)
        for name, rx in compiled.items():
            for m in rx.finditer(searchable):
                reason = None
                if name in file_exempt:
                    reason = "file-scoped exemption"
                elif in_any(m.start(), spans):
                    reason = "inside quotation"
                elif line_exempt:
                    reason = "exempt pattern"
                s, e = max(0, m.start() - 90), min(len(line), m.end() + 90)
                rep.hits.append(Hit(path, i, name, m.group(0), line[s:e].strip(), reason))
    rep.cadence = cadence_measures(scoped)
    return rep


# ---------------------------------------------------------------- file walk

def iter_files(root: str, cfg: dict):
    excl = [re.compile(p) for p in cfg["exclude"]]
    seen: set[str] = set()
    for base in cfg["paths"]:
        start = os.path.join(root, base)
        if not os.path.exists(start):
            raise ScanError(f"configured path does not exist: {base}")
        if os.path.isfile(start):
            real = os.path.realpath(start)
            if real not in seen:
                seen.add(real)
                yield start
            continue
        for dirpath, dirnames, filenames in os.walk(start):
            dirnames[:] = sorted(d for d in dirnames if d != ".git")
            for fn in sorted(filenames):
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, root)
                if not any(fn.endswith(x) for x in cfg["extensions"]):
                    continue
                if any(p.search(rel) for p in excl):
                    continue
                real = os.path.realpath(full)
                if real not in seen:
                    seen.add(real)
                    yield full


def added_lines(root: str, ref: str) -> dict[str, set[int]]:
    """Line numbers added since `ref`, per file — the base-branch discipline."""
    out = subprocess.run(
        ["git", "-C", root, "-c", "core.quotePath=false", "diff",
         "--unified=0", "--no-color", "--no-ext-diff", ref, "--"],
        capture_output=True, text=True)
    if out.returncode != 0:
        raise ScanError(f"git diff against {ref} failed: {out.stderr.strip()}")
    result: dict[str, set[int]] = {}
    path = None
    for line in out.stdout.splitlines():
        if line.startswith("diff --git "):
            path = None
        elif line.startswith("+++ b/"):
            path = line[6:]
        elif line == "+++ /dev/null":
            path = None
        elif line.startswith("@@") and path:
            m = re.search(r"\+(\d+)(?:,(\d+))?", line)
            if m:
                start, count = int(m.group(1)), int(m.group(2) or 1)
                result.setdefault(path, set()).update(range(start, start + count))
    return result


# ---------------------------------------------------------------- reporting

def cadence_limits(cfg: dict) -> dict[str, float]:
    """Return enforced limits, accepting the original *_max schema.

    Legacy zero values remain report-only. The additive `limits` mapping makes
    zero an enforceable limit because presence, rather than truthiness, carries
    the policy decision.
    """
    cadence = cfg["cadence"]
    limits = dict(cadence.get("limits", {}))
    for legacy, measure in CADENCE_LEGACY_KEYS.items():
        value = cadence.get(legacy, 0)
        if value and measure not in limits:
            limits[measure] = value
    return limits


def report_failures(rep: FileReport, cfg: dict) -> list[str]:
    failures = []
    if cfg["fail_on_live_hits"] and rep.live:
        failures.append(f"{len(rep.live)} live hit(s)")
    density = cfg["density_per_1k_max"]
    if density and rep.rate > density:
        failures.append(f"density {rep.rate:.2f}/1k > {density:g}")
    for measure, limit in cadence_limits(cfg).items():
        value = rep.cadence.get(measure, 0)
        if value > limit:
            failures.append(f"{measure} {value:g} > {limit:g}")
    return failures


def emit_text(reports: list[FileReport], cfg: dict) -> None:
    total_live = sum(len(rep.live) for rep in reports)
    total_words = sum(rep.words for rep in reports)
    for rep in reports:
        if not rep.hits and not any(rep.cadence.values()):
            continue
        failures = report_failures(rep, cfg)
        flag = f"  <<< {'; '.join(failures)}" if failures else ""
        print(f"\n{rep.path}  ({rep.words} words, {rep.rate:.2f}/1k){flag}")
        for h in rep.live:
            print(f"  L{h.line:<5} [{h.term}]  …{h.context}…")
        for h in (x for x in rep.hits if x.exempt):
            print(f"  L{h.line:<5} [{h.term}]  EXEMPT ({h.exempt})")
        cad = "  ".join(f"{k}={v}" for k, v in rep.cadence.items() if v)
        if cad:
            print(f"  cadence: {cad}")
    rate = total_live / total_words * 1000 if total_words else 0.0
    print(f"\n{total_live} live hit(s) across {total_words} words — {rate:.2f}/1k")


def main() -> int:
    ap = argparse.ArgumentParser(description="AI-tell and cadence scanner")
    ap.add_argument("--config", required=True)
    ap.add_argument("--root", default=".", help="repo root (default: cwd)")
    ap.add_argument("--paths", nargs="*", help="override config paths")
    ap.add_argument("--since", help="scan only lines added since this git ref")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    try:
        cfg = load_config(args.config)
        if args.paths:
            cfg["paths"] = args.paths
            validate_config(cfg)

        only = added_lines(root, args.since) if args.since else None
        reports = []
        for full in iter_files(root, cfg):
            rel = os.path.relpath(full, root)
            lines = only.get(rel) if only is not None else None
            if only is not None and not lines:
                continue
            try:
                with open(full, encoding="utf-8") as fh:
                    raw = fh.read()
            except (OSError, UnicodeDecodeError) as exc:
                raise ScanError(f"cannot scan {rel}: {exc}") from exc
            reports.append(scan_text(rel, raw, cfg, lines))
    except (ConfigError, ScanError) as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2

    live = sum(len(r.live) for r in reports)
    failed = any(report_failures(r, cfg) for r in reports)
    if args.json:
        print(json.dumps({
            "schema_version": 1,
            "scope": {"root": root, "since": args.since},
            "files": [{"path": r.path, "words": r.words, "rate": round(r.rate, 3),
                       "cadence": r.cadence,
                       "hits": [vars(h) for h in r.hits],
                       "failures": report_failures(r, cfg)} for r in reports],
            "live_hits": live,
            "failed": failed,
        }, indent=2))
    else:
        emit_text(reports, cfg)

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
