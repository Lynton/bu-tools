#!/usr/bin/env python3
"""quotecheck — verify quotations against a held source corpus.

Generalised from 0x00's working/quotecheck.py so one engine serves every project.
Matching is OCR-tolerant: both sides reduce to lowercase alphanumerics with all
whitespace and punctuation stripped, which absorbs the space-injection artefacts
typical of scanned text ("m ultiplicity" -> "multiplicity").

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

Exit codes: 0 no PARTIAL or SPILL · 1 findings · 2 usage/config error.
Standard library only.
"""
from __future__ import annotations
import argparse, json, os, re, sys
from collections import Counter

MINWORDS = 6
SPILL_CHARS = 120           # how far past the closing mark to compare
SPILL_MIN_MATCH = 40        # normalised chars that must agree to call it a spill
SHINGLE = 5                 # words per shingle for close-paraphrase detection
PARAPHRASE_MIN = 2          # distinct shingles from one sentence found in one source

QUOTE = re.compile(r'[“"]([^“”"]{25,600})[”"]')
# Extraction wants a quotation worth checking (>=25 chars). SPAN wants every pair,
# because a short quote's marks still consume their partners — leaving them out
# desynchronises every pair after it and makes quoted text read as own voice.
QUOTE_SPAN = re.compile(r'"[^"]*"|“[^”]*”')
TAG = re.compile(r"<[^>]*>")


def mask_tags(s: str) -> str:
    """Blank out HTML tags, preserving offsets.

    Entries in an HTML-fragment corpus carry attribute quotes (class="reading"),
    which offset every quotation pair after them and make a passage inside
    quotation marks look like own voice. Masking to equal-length spaces keeps
    match positions aligned with the original line.
    """
    return TAG.sub(lambda m: " " * len(m.group(0)), s)


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def shingles(text: str, n: int = SHINGLE) -> list[str]:
    """Normalised n-word windows, for detecting close paraphrase."""
    w = re.findall(r"[A-Za-z0-9']+", text)
    return [norm("".join(w[i:i + n])) for i in range(len(w) - n + 1)]


def load_config(path: str) -> dict:
    if not os.path.exists(path):
        sys.stderr.write(f"config not found: {path}\n")
        sys.exit(2)
    cfg = json.load(open(path))
    qc = cfg.get("quotecheck")
    if not qc or not qc.get("documents") or not qc.get("sources"):
        sys.stderr.write("config needs quotecheck.documents and quotecheck.sources\n")
        sys.exit(2)
    # the register terms live at the top level; the term-in-source pass needs them
    qc.setdefault("terms", cfg.get("terms", {}))
    return qc


def walk(root: str, specs: list[str], exts: list[str]):
    for spec in specs:
        start = os.path.join(root, spec)
        if os.path.isfile(start):
            yield start
            continue
        for dirpath, dirnames, filenames in os.walk(start):
            dirnames[:] = [d for d in dirnames if d != ".git"]
            for fn in sorted(filenames):
                if exts and not any(fn.endswith(x) for x in exts):
                    continue
                yield os.path.join(dirpath, fn)


def main() -> int:
    ap = argparse.ArgumentParser(description="verify quotations against held sources")
    ap.add_argument("--config", required=True)
    ap.add_argument("--root", default=".")
    ap.add_argument("--json", help="write the full record here")
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    qc = load_config(args.config)
    doc_exts = qc.get("document_extensions", [".md"])
    src_exts = qc.get("source_extensions", [".md", ".txt"])

    # index the corpus once, keeping the normalised blob for substring search
    blobs, names = [], []
    for f in walk(root, qc["sources"], src_exts):
        try:
            blobs.append(norm(open(f, encoding="utf-8", errors="replace").read()))
            names.append(os.path.relpath(f, root))
        except OSError:
            continue
    sys.stderr.write(f"indexed {len(blobs)} source files\n")
    if not blobs:
        sys.stderr.write("no sources indexed — nothing to check against\n")
        return 2

    # One joined index rather than 216 separate scans. The needles are pure
    # [a-z0-9] after normalisation, so a NUL separator can never be spanned by a
    # match — file boundaries cannot produce a false hit. This is the difference
    # between a tool that runs in seconds and one nobody runs.
    import bisect
    joined_parts, starts, offset = [], [], 0
    for b in blobs:
        starts.append(offset)
        joined_parts.append(b)
        offset += len(b) + 1
    joined = "\x00".join(blobs)

    def locate(needle: str):
        if not needle:
            return None, -1, None
        at = joined.find(needle)
        if at < 0:
            return None, -1, None
        i = bisect.bisect_right(starts, at) - 1
        return names[i], at - starts[i], blobs[i]

    findings = []
    for f in walk(root, qc["documents"], doc_exts):
        rel = os.path.relpath(f, root)
        text = open(f, encoding="utf-8", errors="replace").read()
        for m in QUOTE.finditer(mask_tags(text)):
            raw = m.group(1).strip()
            if len(raw.split()) < MINWORDS:
                continue
            if raw.startswith(("http", "/", "`")) or ".md" in raw:
                continue
            rec = {"file": rel, "line": text[:m.start()].count("\n") + 1, "quote": raw}
            nq = norm(raw)
            src, at, blob = locate(nq)
            if src:
                rec["status"], rec["source"] = "FOUND", src
                # does the document keep speaking in the source's words?
                after_doc = norm(text[m.end():m.end() + SPILL_CHARS * 2])
                after_src = blob[at + len(nq): at + len(nq) + SPILL_CHARS]
                shared = 0
                for a, b in zip(after_doc, after_src):
                    if a != b:
                        break
                    shared += 1
                if shared >= SPILL_MIN_MATCH:
                    rec["status"] = "SPILL"
                    rec["spill_chars"] = shared
                    rec["spill_text"] = text[m.end():m.end() + SPILL_CHARS].strip()
            else:
                w = raw.split()
                head, _, _ = locate(norm(" ".join(w[:MINWORDS])))
                tail, _, _ = locate(norm(" ".join(w[-MINWORDS:])))
                if head or tail:
                    rec["status"] = "PARTIAL"
                    rec["source"] = head or tail
                    rec["detail"] = f"head={'y' if head else 'n'} tail={'y' if tail else 'n'}"
                else:
                    rec["status"], rec["source"] = "NOT-IN-CORPUS", None
            findings.append(rec)

    # ---- source-fidelity passes -----------------------------------------
    # Both passes below run only against sources the document actually quotes.
    # That is faster (comparing every sentence against every source was quadratic
    # and unusable on a real corpus) and more precise: a shingle shared with an
    # unrelated source is coincidence, not provenance.
    by_name = dict(zip(names, blobs))
    doc_sources: dict[str, set[str]] = {}
    for r in findings:
        if r.get("source"):
            doc_sources.setdefault(r["file"], set()).add(r["source"])

    terms = {n: re.compile(rx, re.I) for n, rx in (qc.get("terms") or {}).items()}
    skip_par = qc.get("skip_paraphrase")
    paraphrase, term_hits = [], []

    for rel, srcs in doc_sources.items():
        text = open(os.path.join(root, rel), encoding="utf-8", errors="replace").read()
        src_blobs = [(s, by_name[s]) for s in sorted(srcs) if s in by_name]
        for ln, line in enumerate(text.splitlines(), 1):
            masked = mask_tags(line)
            spans = [(m.start(), m.end()) for m in QUOTE_SPAN.finditer(masked)]

            # word scale — a flagged term outside the marks that the source also uses.
            # The bare word is worthless as a signal: "actually" appears in any long
            # source, which is why a first cut returned 211 hits. What carries
            # provenance is the COLLOCATION — the term plus the word it sits with.
            # "genuine understanding" is Sekrst's; "genuine" alone is nobody's.
            for name, rx in terms.items():
                for m in rx.finditer(line):
                    if any(a <= m.start() < b for a, b in spans):
                        continue
                    nxt = re.match(r"\W*([A-Za-z0-9']+)", line[m.end():])
                    prv = re.search(r"([A-Za-z0-9']+)\W*$", line[:m.start()])
                    grams = []
                    if nxt:
                        grams.append((norm(m.group(0) + nxt.group(1)),
                                      f"{m.group(0)} {nxt.group(1)}"))
                    if prv:
                        grams.append((norm(prv.group(1) + m.group(0)),
                                      f"{prv.group(1)} {m.group(0)}"))
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

            # sentence scale — unquoted prose tracking the source's wording
            if skip_par:
                continue
            stripped = QUOTE_SPAN.sub(" ", masked)
            if len(stripped.split()) < 12:
                continue
            for sent in re.split(r"(?<=[.!?])\s+", stripped):
                sh = [s for s in shingles(sent) if s]
                if len(sh) < PARAPHRASE_MIN:
                    continue
                for s, blob in src_blobs:
                    found = [x for x in sh if x in blob]
                    if len(found) >= PARAPHRASE_MIN:
                        paraphrase.append({"file": rel, "line": ln, "source": s,
                                           "matched": len(found), "of": len(sh),
                                           "text": sent.strip()[:200], "example": found[0]})
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
        json.dump({"quotations": findings, "paraphrase": paraphrase, "term_in_source": term_hits},
                  open(args.json, "w"), indent=1, ensure_ascii=False)
        print(f"record written to {args.json}")
    return 1 if (counts["PARTIAL"] or counts["SPILL"] or paraphrase or term_hits) else 0


if __name__ == "__main__":
    sys.exit(main())
