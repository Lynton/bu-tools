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

QUOTE = re.compile(r'[“"]([^“”"]{25,600})[”"]')


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def load_config(path: str) -> dict:
    if not os.path.exists(path):
        sys.stderr.write(f"config not found: {path}\n")
        sys.exit(2)
    cfg = json.load(open(path))
    qc = cfg.get("quotecheck")
    if not qc or not qc.get("documents") or not qc.get("sources"):
        sys.stderr.write("config needs quotecheck.documents and quotecheck.sources\n")
        sys.exit(2)
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

    def locate(needle: str):
        for nm, b in zip(names, blobs):
            if needle and needle in b:
                return nm, b.index(needle), b
        return None, -1, None

    findings = []
    for f in walk(root, qc["documents"], doc_exts):
        rel = os.path.relpath(f, root)
        text = open(f, encoding="utf-8", errors="replace").read()
        for m in QUOTE.finditer(text):
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
        json.dump(findings, open(args.json, "w"), indent=1, ensure_ascii=False)
        print(f"record written to {args.json}")
    return 1 if (counts["PARTIAL"] or counts["SPILL"]) else 0


if __name__ == "__main__":
    sys.exit(main())
