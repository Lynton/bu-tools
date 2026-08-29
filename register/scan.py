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
import argparse, json, os, re, subprocess, sys
from dataclasses import dataclass, field

# ---------------------------------------------------------------- config

DEFAULTS = {
    "paths": ["."],
    "extensions": [".md"],
    "exclude": [],
    "terms": {},
    "exempt_quoted": True,
    "exempt_patterns": [],
    "exempt_terms_in_files": {},
    "cadence": {
        "antithetical_max": 0,       # 0 = report only, never fail
        "paired_short_closes_max": 0,
        "anaphora_max": 0,
        "isolated_short_paras_max": 0,
    },
    "density_per_1k_max": 0.0,       # 0.0 = report only
}


def load_config(path: str) -> dict:
    if not os.path.exists(path):
        sys.stderr.write(f"config not found: {path}\n")
        sys.exit(2)
    with open(path) as fh:
        try:
            cfg = json.load(fh)
        except json.JSONDecodeError as exc:
            sys.stderr.write(f"config is not valid JSON: {exc}\n")
            sys.exit(2)
    merged = dict(DEFAULTS)
    merged.update(cfg)
    merged["cadence"] = {**DEFAULTS["cadence"], **cfg.get("cadence", {})}
    if not merged["terms"]:
        sys.stderr.write("config defines no terms — nothing to scan\n")
        sys.exit(2)
    return merged


# ---------------------------------------------------------------- text model

WORD = re.compile(r"\b[\w'-]+\b")
# Straight and curly pairs, plus <q>. Non-greedy, single line: a quotation that
# spans lines is not detected, which is the safe direction — it reports rather
# than silently exempts.
QUOTED = re.compile(r"\"[^\"]{2,}\"|“[^”]{2,}”|'[^']{8,}'|<q>.*?</q>")
FRONTMATTER = re.compile(r"\A---\n.*?\n---\n", re.S)


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


def scan_text(path: str, raw: str, cfg: dict, only_lines: set[int] | None = None) -> FileReport:
    body = FRONTMATTER.sub("", raw)
    rep = FileReport(path=path, words=words(body))
    exempt_pats = [re.compile(p, re.I) for p in cfg["exempt_patterns"]]
    file_exempt = {t for pat, terms in cfg["exempt_terms_in_files"].items()
                   if re.search(pat, path) for t in terms}
    compiled = {name: re.compile(rx, re.I) for name, rx in cfg["terms"].items()}

    for i, line in enumerate(raw.splitlines(), 1):
        if only_lines is not None and i not in only_lines:
            continue
        spans = quoted_spans(line) if cfg["exempt_quoted"] else []
        line_exempt = next((p.pattern for p in exempt_pats if p.search(line)), None)
        for name, rx in compiled.items():
            for m in rx.finditer(line):
                reason = None
                if name in file_exempt:
                    reason = "file-scoped exemption"
                elif in_any(m.start(), spans):
                    reason = "inside quotation"
                elif line_exempt:
                    reason = "exempt pattern"
                s, e = max(0, m.start() - 90), min(len(line), m.end() + 90)
                rep.hits.append(Hit(path, i, name, m.group(0), line[s:e].strip(), reason))
    rep.cadence = cadence_measures(body)
    return rep


# ---------------------------------------------------------------- file walk

def iter_files(root: str, cfg: dict):
    excl = [re.compile(p) for p in cfg["exclude"]]
    for base in cfg["paths"]:
        start = os.path.join(root, base)
        if os.path.isfile(start):
            yield start
            continue
        for dirpath, dirnames, filenames in os.walk(start):
            dirnames[:] = [d for d in dirnames if d != ".git"]
            for fn in sorted(filenames):
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, root)
                if not any(fn.endswith(x) for x in cfg["extensions"]):
                    continue
                if any(p.search(rel) for p in excl):
                    continue
                yield full


def added_lines(root: str, ref: str) -> dict[str, set[int]]:
    """Line numbers added since `ref`, per file — the base-branch discipline."""
    out = subprocess.run(["git", "-C", root, "diff", "--unified=0", ref],
                         capture_output=True, text=True)
    if out.returncode != 0:
        sys.stderr.write(f"git diff against {ref} failed: {out.stderr.strip()}\n")
        sys.exit(2)
    result: dict[str, set[int]] = {}
    path = None
    for line in out.stdout.splitlines():
        if line.startswith("+++ b/"):
            path = line[6:]
        elif line.startswith("@@") and path:
            m = re.search(r"\+(\d+)(?:,(\d+))?", line)
            if m:
                start, count = int(m.group(1)), int(m.group(2) or 1)
                result.setdefault(path, set()).update(range(start, start + count))
    return result


# ---------------------------------------------------------------- reporting

def emit_text(reports: list[FileReport], cfg: dict) -> None:
    total_live = total_words = 0
    for rep in reports:
        if not rep.hits and not any(rep.cadence.values()):
            continue
        total_live += len(rep.live)
        total_words += rep.words
        flag = ""
        if cfg["density_per_1k_max"] and rep.rate > cfg["density_per_1k_max"]:
            flag = "  <<< over threshold"
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
    cfg = load_config(args.config)
    if args.paths:
        cfg["paths"] = args.paths

    only = added_lines(root, args.since) if args.since else None
    reports = []
    for full in iter_files(root, cfg):
        rel = os.path.relpath(full, root)
        lines = only.get(rel) if only is not None else None
        if only is not None and not lines:
            continue
        try:
            raw = open(full, encoding="utf-8").read()
        except (OSError, UnicodeDecodeError):
            continue
        reports.append(scan_text(rel, raw, cfg, lines))

    live = sum(len(r.live) for r in reports)
    if args.json:
        print(json.dumps({
            "files": [{"path": r.path, "words": r.words, "rate": round(r.rate, 3),
                       "cadence": r.cadence,
                       "hits": [vars(h) for h in r.hits]} for r in reports],
            "live_hits": live,
        }, indent=2))
    else:
        emit_text(reports, cfg)

    if cfg["density_per_1k_max"]:
        if any(r.rate > cfg["density_per_1k_max"] for r in reports):
            return 1
    elif live:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
