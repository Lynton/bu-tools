#!/usr/bin/env python3
"""quotecheck — verify quotations against a held source corpus.

Generalised from 0x00's working/quotecheck.py so one engine serves every project.
Matching is Unicode-aware and OCR-tolerant: both sides casefold, discard combining
marks, whitespace and punctuation, and retain letters and numbers. That absorbs
common accent and space-injection artefacts ("café" -> "cafe"; "m ultiplicity"
-> "multiplicity") without deleting non-Latin scripts.

Three verdicts, and the third is the one that earns the tool:

  FOUND         the quotation is in the corpus, verbatim after normalisation
  PARTIAL       head or tail matches but not the whole — the shape an altered
                quotation makes
  NOT-IN-CORPUS unverifiable here. Not proof of misquotation: the source may
                simply not be held.

And one warning that is not about quotations at all:

  SPILL         the document CONTINUES IN THE SOURCE'S WORDING after the closing
                quotation mark. The words outside the marks are still the
                author's. Editing them — a register sweep, say — silently
                rewrites the source. Added 2026-08-29 after exactly that: a
                three-word register pass stripped Sekrst's "genuine
                understanding" and Birch's "might genuinely be achieved", both
                of which sat just outside the marks and both of which were the
                authors' own terms.

    python3 tools/shared/register/quotecheck.py --config tools/register.config.json
    python3 tools/shared/register/quotecheck.py --config <cfg> --json out.json

Exit codes: 0 no actionable findings · 1 findings · 2 usage/config error.
Standard library only.
"""
from __future__ import annotations

import argparse
import bisect
import json
import os
import re
import sys
import unicodedata
from collections import Counter

MINWORDS = 6
SPILL_CHARS = 120           # how far past the closing mark to compare
SPILL_MIN_MATCH = 40        # normalised chars that must agree to call it a spill
SHINGLE = 5                 # words per shingle for close-paraphrase detection
PARAPHRASE_MIN = 2          # distinct shingles from one sentence found in one source

# Extraction wants a quotation worth checking (>=25 chars). Straight and curly
# quotation marks are paired separately so malformed mixed pairs are not treated
# as verified quotations. There is no upper bound: long block quotations need
# checking too.
QUOTE = re.compile(r'"(?P<straight>[^"]{25,})"|“(?P<curly>[^”]{25,})”')
# SPAN wants every pair because short quotation marks still delimit own voice.
# Straight single quotes require word boundaries so apostrophes in contractions
# cannot manufacture a quotation span.
QUOTE_SPAN = re.compile(
    r'''"[^"\n]*"|“[^”\n]*”|‘[^’\n]*’|(?<![\w’])'[^'\n]{2,}'(?![\w’])''')
Q_TAG = re.compile(r"<q\b[^>]*>.*?</q>", re.I | re.S)
TAG = re.compile(r"<[^>]*>")
WORD_TOKEN = re.compile(r"[^\W_]+(?:['’][^\W_]+)*", re.UNICODE)


class ConfigError(ValueError):
    """Invalid configuration supplied by a consumer."""


class CheckError(RuntimeError):
    """The requested check could not be completed safely."""


def mask_tags(s: str) -> str:
    """Blank out HTML tags, preserving offsets.

    Entries in an HTML-fragment corpus carry attribute quotes (class="reading"),
    which offset every quotation pair after them and make a passage inside
    quotation marks look like own voice. Masking to equal-length spaces keeps
    match positions aligned with the original line.
    """
    return TAG.sub(lambda m: " " * len(m.group(0)), s)


def mask_spans(s: str, spans: list[tuple[int, int]]) -> str:
    """Blank spans without changing offsets or line numbers."""
    chars = list(s)
    for start, end in spans:
        for i in range(start, end):
            if chars[i] not in "\r\n":
                chars[i] = " "
    return "".join(chars)


def quotation_spans(s: str) -> list[tuple[int, int]]:
    masked = mask_tags(s)
    spans = [(m.start(), m.end()) for m in QUOTE_SPAN.finditer(masked)]
    spans.extend((m.start(), m.end()) for m in Q_TAG.finditer(s))
    return sorted(spans)


def norm(s: str) -> str:
    # NFKD makes accented Latin text OCR-tolerant (café == cafe) while
    # isalnum keeps letters from non-Latin scripts instead of deleting them.
    folded = unicodedata.normalize("NFKD", s).casefold()
    return "".join(ch for ch in folded if ch.isalnum())


def shingles(text: str, n: int = SHINGLE) -> list[str]:
    """Normalised n-word windows, for detecting close paraphrase."""
    w = WORD_TOKEN.findall(text)
    return [norm("".join(w[i:i + n])) for i in range(len(w) - n + 1)]


def distinct_shingles(text: str, n: int = SHINGLE) -> list[str]:
    """Shingles in first-seen order, with repetition counted once."""
    return list(dict.fromkeys(s for s in shingles(text, n) if s))


def _string_list(value, label: str, *, allow_empty: bool = False) -> None:
    if (not isinstance(value, list) or not all(isinstance(x, str) for x in value)
            or (not allow_empty and not value)):
        qualifier = "" if allow_empty else " non-empty"
        raise ConfigError(f"{label} must be a{qualifier} list of strings")


def _compile(pattern: str, label: str) -> None:
    try:
        re.compile(pattern)
    except re.error as exc:
        raise ConfigError(f"invalid regex for {label}: {exc}") from exc


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
    qc = cfg.get("quotecheck")
    if not isinstance(qc, dict):
        raise ConfigError("config needs a quotecheck object")
    qc = dict(qc)
    _string_list(qc.get("documents"), "quotecheck.documents")
    _string_list(qc.get("sources"), "quotecheck.sources")
    for key, default in (("document_extensions", [".md"]),
                         ("source_extensions", [".md", ".txt"])):
        qc.setdefault(key, default)
        _string_list(qc[key], f"quotecheck.{key}", allow_empty=True)
    if "skip_paraphrase" in qc and not isinstance(qc["skip_paraphrase"], bool):
        raise ConfigError("quotecheck.skip_paraphrase must be true or false")
    # the register terms live at the top level; the term-in-source pass needs them
    terms = cfg.get("terms", {})
    if not isinstance(terms, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in terms.items()):
        raise ConfigError("terms must map names to regex strings")
    for name, pattern in terms.items():
        _compile(pattern, f"terms.{name}")
    qc["terms"] = terms
    return qc


def walk(root: str, specs: list[str], exts: list[str]):
    seen: set[str] = set()
    for spec in specs:
        start = os.path.join(root, spec)
        if not os.path.exists(start):
            raise CheckError(f"configured path does not exist: {spec}")
        if os.path.isfile(start):
            real = os.path.realpath(start)
            if real not in seen:
                seen.add(real)
                yield start
            continue
        for dirpath, dirnames, filenames in os.walk(start):
            dirnames[:] = sorted(d for d in dirnames if d != ".git")
            for fn in sorted(filenames):
                if exts and not any(fn.endswith(x) for x in exts):
                    continue
                full = os.path.join(dirpath, fn)
                real = os.path.realpath(full)
                if real not in seen:
                    seen.add(real)
                    yield full


def read_text(path: str, root: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError as exc:
        rel = os.path.relpath(path, root)
        raise CheckError(f"cannot read {rel}: {exc}") from exc


class SourceIndex:
    """Normalised source corpus with all-occurrence lookup."""

    def __init__(self, items: list[tuple[str, str]]):
        self.names = [name for name, _ in items]
        self.blobs = [norm(text) for _, text in items]
        self.starts: list[int] = []
        offset = 0
        for blob in self.blobs:
            self.starts.append(offset)
            offset += len(blob) + 1
        self.joined = "\x00".join(self.blobs)

    def locate_all(self, needle: str) -> list[tuple[str, int, str]]:
        if not needle:
            return []
        matches = []
        start = 0
        while True:
            at = self.joined.find(needle, start)
            if at < 0:
                break
            i = bisect.bisect_right(self.starts, at) - 1
            local = at - self.starts[i]
            # Defensive boundary check. A normalised needle cannot contain NUL,
            # but retaining this guard makes the invariant explicit.
            if local >= 0 and local + len(needle) <= len(self.blobs[i]):
                matches.append((self.names[i], local, self.blobs[i]))
            start = at + 1
        return matches

    def source_names(self, needle: str) -> list[str]:
        return list(dict.fromkeys(name for name, _, _ in self.locate_all(needle)))

    def best_continuation(self, needle: str, after_doc: str
                          ) -> tuple[tuple[str, int, str] | None, int]:
        best = None
        best_shared = -1
        for match in self.locate_all(needle):
            _, at, blob = match
            after_src = blob[at + len(needle): at + len(needle) + SPILL_CHARS]
            shared = 0
            for a, b in zip(after_doc, after_src):
                if a != b:
                    break
                shared += 1
            if shared > best_shared:
                best, best_shared = match, shared
        return best, max(best_shared, 0)


def main() -> int:
    ap = argparse.ArgumentParser(description="verify quotations against held sources")
    ap.add_argument("--config", required=True)
    ap.add_argument("--root", default=".")
    ap.add_argument("--json", help="write the full record here")
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    qc = load_config(args.config)
    doc_exts = qc["document_extensions"]
    src_exts = qc["source_extensions"]

    # Index the corpus once. SourceIndex retains every occurrence so repeated
    # quotations can be attributed by their continuation rather than whichever
    # copy happens to appear first in filesystem order.
    source_items = []
    for f in walk(root, qc["sources"], src_exts):
        source_items.append((os.path.relpath(f, root), read_text(f, root)))
    sys.stderr.write(f"indexed {len(source_items)} source files\n")
    if not source_items:
        raise CheckError("no sources indexed — nothing to check against")
    index = SourceIndex(source_items)

    findings = []
    for f in walk(root, qc["documents"], doc_exts):
        rel = os.path.relpath(f, root)
        text = read_text(f, root)
        for m in QUOTE.finditer(mask_tags(text)):
            raw = (m.group("straight") or m.group("curly")).strip()
            if len(WORD_TOKEN.findall(raw)) < MINWORDS:
                continue
            if raw.startswith(("http", "/", "`")) or ".md" in raw:
                continue
            rec = {"file": rel, "line": text[:m.start()].count("\n") + 1, "quote": raw}
            nq = norm(raw)
            sources = index.source_names(nq)
            if sources:
                after_doc = norm(text[m.end():m.end() + SPILL_CHARS * 2])
                best, shared = index.best_continuation(nq, after_doc)
                src = best[0] if best else sources[0]
                rec.update(status="FOUND", source=src, sources=sources)
                if shared >= SPILL_MIN_MATCH:
                    rec["status"] = "SPILL"
                    rec["spill_chars"] = shared
                    rec["spill_text"] = text[m.end():m.end() + SPILL_CHARS].strip()
            else:
                w = WORD_TOKEN.findall(raw)
                head_sources = index.source_names(norm(" ".join(w[:MINWORDS])))
                tail_sources = index.source_names(norm(" ".join(w[-MINWORDS:])))
                partial_sources = list(dict.fromkeys(head_sources + tail_sources))
                if partial_sources:
                    common = [s for s in head_sources if s in set(tail_sources)]
                    source = (common or head_sources or tail_sources)[0]
                    rec["status"] = "PARTIAL"
                    rec["source"] = source
                    rec["sources"] = partial_sources
                    rec["detail"] = (f"head={'y' if head_sources else 'n'} "
                                     f"tail={'y' if tail_sources else 'n'}")
                else:
                    rec.update(status="NOT-IN-CORPUS", source=None, sources=[])
            findings.append(rec)

    # ---- source-fidelity passes -----------------------------------------
    # Both passes below run only against sources the document actually quotes.
    # That is faster (comparing every sentence against every source was quadratic
    # and unusable on a real corpus) and more precise: a shingle shared with an
    # unrelated source is coincidence, not provenance.
    by_name = dict(zip(index.names, index.blobs))
    doc_sources: dict[str, set[str]] = {}
    for r in findings:
        for source in r.get("sources", []):
            doc_sources.setdefault(r["file"], set()).add(source)

    terms = {n: re.compile(rx, re.I) for n, rx in (qc.get("terms") or {}).items()}
    skip_par = qc.get("skip_paraphrase")
    paraphrase, term_hits = [], []

    for rel, srcs in doc_sources.items():
        text = read_text(os.path.join(root, rel), root)
        src_blobs = [(s, by_name[s]) for s in sorted(srcs) if s in by_name]
        for ln, line in enumerate(text.splitlines(), 1):
            masked = mask_tags(line)
            spans = quotation_spans(line)

            # word scale — a flagged term outside the marks that the source also uses.
            # The bare word is worthless as a signal: "actually" appears in any long
            # source, which is why a first cut returned 211 hits. What carries
            # provenance is the COLLOCATION — the term plus the word it sits with.
            # "genuine understanding" is Sekrst's; "genuine" alone is nobody's.
            for name, rx in terms.items():
                for m in rx.finditer(masked):
                    if any(a <= m.start() < b for a, b in spans):
                        continue
                    nxt = WORD_TOKEN.search(masked, m.end())
                    previous = list(WORD_TOKEN.finditer(masked, 0, m.start()))
                    prv = previous[-1] if previous else None
                    grams = []
                    if nxt:
                        grams.append((norm(m.group(0) + nxt.group(0)),
                                      f"{m.group(0)} {nxt.group(0)}"))
                    if prv:
                        grams.append((norm(prv.group(0) + m.group(0)),
                                      f"{prv.group(0)} {m.group(0)}"))
                    hit = None
                    for gram, shown in grams:
                        for s, blob in src_blobs:
                            if gram and gram in blob:
                                hit = (s, shown)
                                break
                        if hit:
                            break
                    if hit:
                        term_hits.append({
                            "file": rel, "line": ln, "term": name,
                            "word": hit[1], "source": hit[0],
                            "context": line[max(0, m.start() - 80):m.end() + 80].strip()})

        # Sentence scale — run over the whole document so a sentence wrapped
        # across Markdown source lines remains one sentence. Quoted spans are
        # blanked with offsets preserved before comparison.
        if not skip_par:
            stripped = mask_spans(mask_tags(text), quotation_spans(text))
            for sentence in re.finditer(r"[^.!?]+(?:[.!?]+|$)", stripped, re.S):
                sent = sentence.group(0).strip()
                if len(WORD_TOKEN.findall(sent)) < 12:
                    continue
                sh = distinct_shingles(sent)
                if len(sh) < PARAPHRASE_MIN:
                    continue
                ln = stripped[:sentence.start()].count("\n") + 1
                for s, blob in src_blobs:
                    found = [x for x in sh if x in blob]
                    if len(found) >= PARAPHRASE_MIN:
                        paraphrase.append({"file": rel, "line": ln, "source": s,
                                           "matched": len(found), "of": len(sh),
                                           "text": sent[:200], "example": found[0]})
                        break

    if term_hits:
        print(f"\n=== TERM-IN-SOURCE ({len(term_hits)}) — a flagged term that the quoted source also uses")
        for r in term_hits:
            print(f"  {r['file']}:{r['line']}  [{r['term']}] \u201c{r['word']}\u201d -> also in {r['source']}")
            print(f"    …{r['context']}…")
            print("    -> may be the author's term. Check the source before removing it.")

    if paraphrase:
        print(f"\n=== PARAPHRASE ({len(paraphrase)}) — unquoted prose tracking a source's wording")
        for r in paraphrase:
            print(f"  {r['file']}:{r['line']}  [{r['source']}]  {r['matched']}/{r['of']} shingles")
            print(f"    {r['text']}…")
            print("    -> the wording is the source's. Do not edit as own voice.")

    counts = Counter(r["status"] for r in findings)
    for status, label in (("SPILL", "the document continues in the source's wording"),
                          ("PARTIAL", "head or tail matched but not the whole")):
        rows = [r for r in findings if r["status"] == status]
        if rows:
            print(f"\n=== {status} ({len(rows)}) — {label}")
            for r in rows:
                print(f"  {r['file']}:{r['line']}  [{r.get('source') or '?'}]")
                print(f"    quoted: “{r['quote'][:110]}…”")
                if status == "SPILL":
                    print(f"    then, still the source's words ({r['spill_chars']} chars): "
                          f"{r['spill_text'][:110]}…")
                    print("    -> treat as quotation. Do not edit as own voice.")
                else:
                    print(f"    {r.get('detail','')}")
    print(f"\n{dict(counts)} across {len(findings)} quotations")
    if args.json:
        record = {"schema_version": 1, "quotations": findings,
                  "paraphrase": paraphrase, "term_in_source": term_hits}
        try:
            with open(args.json, "w", encoding="utf-8") as fh:
                json.dump(record, fh, indent=1, ensure_ascii=False)
                fh.write("\n")
        except OSError as exc:
            raise CheckError(f"cannot write JSON record {args.json}: {exc}") from exc
        print(f"record written to {args.json}")
    return 1 if (counts["PARTIAL"] or counts["SPILL"] or paraphrase or term_hits) else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ConfigError, CheckError) as exc:
        sys.stderr.write(f"error: {exc}\n")
        sys.exit(2)
