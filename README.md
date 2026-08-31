# bu-tools

Shared prose-integrity tooling, single-sourced across the projects.

The engine lives here; **the rules live in the consuming repo.** A holiday-let site and
a philosophy corpus need different wordlists and different exemptions — *genuine
understanding* is a tell in one and a term of art in the other — so the split is
engine/config, not one-size-fits-all.

Standard library only, deliberately: no consuming repo gains a dependency, and the tools
run in CI and in mobile sessions without an install step.

Released under the [MIT License](LICENSE).

## Tools

| Tool | Answers |
|---|---|
| `register/scan.py` | Where is the copy reaching for AI-tells, and how dense is the cadence? |
| `register/quotecheck.py` | Do the quotations match the held sources — and does any passage keep speaking in a source's words *after* the closing mark? |

## Agent use and development

Repository-wide working instructions are single-sourced in [`AGENTS.md`](AGENTS.md).
[`CLAUDE.md`](CLAUDE.md) imports that file, so Claude Code and Codex receive the same
engine boundary, compatibility rules and verification expectations without a second
copy to maintain.

When this repository is mounted as a submodule, the consuming repository remains the
authority for its own rules and release gates. Changes land and are verified here
first; each consumer then bumps the pinned commit deliberately.

Baseline checks for engine changes:

```bash
python3 -m py_compile register/scan.py register/quotecheck.py
python3 register/scan.py --help
python3 register/quotecheck.py --help
```

Behavioural changes also need a focused fixture or reproduction and the relevant
consumer-specific regression or equivalence gate.

## Install into a repo

```bash
git submodule add git@github.com:Lynton/bu-tools.git tools/shared
cp tools/shared/configs/example.register.config.json tools/register.config.json
# edit the config: paths, terms, exemptions
```

Clone a consumer with `git clone --recurse-submodules`, or run
`git submodule update --init` in an existing clone. In GitHub Actions add
`submodules: true` to `actions/checkout`.

**Pinning is a feature.** A submodule is pinned to a commit, so a change here can never
silently alter what a published corpus's gate says. You bump the pointer when you mean to:

```bash
git -C tools/shared pull origin main
git add tools/shared && git commit -m "tools: bump bu-tools"
```

## Running

```bash
python3 tools/shared/register/scan.py --config tools/register.config.json
python3 tools/shared/register/scan.py --config tools/register.config.json --paths frontier_log/
python3 tools/shared/register/scan.py --config tools/register.config.json --since origin/main
python3 tools/shared/register/quotecheck.py --config tools/register.config.json --json /tmp/q.json
```

`--since` scans only lines added against a base ref. That is the standing discipline: it
separates *this pass did this* from *the house has always done this*, and it keeps a
register repair from quietly becoming an unmandated rewrite.

## Config

```jsonc
{
  "paths":      ["canonical", "frontier_log"],   // what to scan
  "extensions": [".md"],
  "exclude":    ["^site/library/"],              // regex on the repo-relative path
  "terms":      { "genuine(ly)": "\\bgenuine(ly)?\\b" },
  "exempt_quoted": true,                         // matches inside " " “ ” <q> are exempt
  "exempt_patterns": ["\\*\\*Actually doing:\\*\\*"],   // named apparatus, per line
  "exempt_terms_in_files": { "ARG-03": ["genuine(ly)"] },// path regex -> terms
  "density_per_1k_max": 0,                       // 0 = report only, never fail
  "cadence": { "paired_short_closes_max": 0 },
  "quotecheck": {
    "documents": ["canonical", "frontier_log/entries"],
    "sources":   ["project_knowledge/sources"],
    "source_extensions": [".md", ".txt"]
  }
}
```

Exemptions are reported, never hidden — an exempt hit prints with its reason, so a wrong
exemption is visible rather than silent.

## The three source-fidelity checks

`quotecheck` runs three passes past plain quotation matching, in widening scope. All three
exist because a register sweep on 2026-08-29 edited three sentences that were reporting a
source, two of them stripping the author's own term — Sekrst's *genuine understanding* and
Birch's *might genuinely be achieved*, the latter the verbatim statement of his Challenge Two.
The general lesson: **a word outside the quotation marks is not thereby own voice.**

| Check | Scope | Catches |
|---|---|---|
| **SPILL** | words immediately after a closing mark | the document carries on in the source's exact words |
| **PARAPHRASE** | whole sentences | unquoted prose tracking the source's wording, by shared 5-word shingles |
| **TERM-IN-SOURCE** | single words | a term your config would flag that the quoted source also uses — the narrowest and, on the case that prompted it, the one that fires |

**TERM-IN-SOURCE matches the collocation, not the bare word.** A first cut compared single
words and returned 211 hits: *actually* appears in any long source, so the bare word carries no
provenance at all. What carries it is the pairing — *genuine understanding* is Sekrst's,
*genuine* alone is nobody's. On the same corpus the collocation version returns one hit before
the fix and none after it.

**Quotation spans are detected separately from quotation extraction**, and HTML tags are masked
first. Both matter more than they sound: attribute quotes (`class="reading"`) and any quotation
under the 25-character extraction floor still consume their partner mark, and leaving them out
desynchronises every pair after them — which makes properly quoted text read as own voice, the
exact error the tool exists to prevent.

TERM-IN-SOURCE is the guard to run before any register sweep. It reports, for every flagged
term sitting outside quotation marks, whether a source the document quotes uses that same
word. On the Sekrst entry it fires on both lines the sweep touched, including the one the
sweep got wrong.

**Its reach is only as wide as the converted corpus.** Sources held as PDFs with no text
conversion are not indexed, so their terms cannot be checked. Birch's manifesto was in that
state and the tool is silent on it — converting primaries to text under the sources directory
is what makes them checkable.

## The SPILL check

`quotecheck` flags a passage that closes its quotation mark and then keeps using the
source's exact words. Those words are still the author's; a register sweep that treats
them as own voice rewrites the source.

Added 2026-08-29, after precisely that happened: a three-word register pass stripped
Sekrst's *genuine understanding* and Birch's *might genuinely be achieved*, both sitting
just outside the marks, both the authors' own terms — Birch's the verbatim statement of
his Challenge Two. The lesson generalises past that pass: **a word outside the quotation
marks is not thereby own voice.**


## Performance

The source index is joined into one blob with NUL separators — the needles are pure `[a-z0-9]`
after normalisation, so a match can never span a file boundary. Even so, a whole-corpus
`quotecheck` scales with the number of quotations that are *not* found, each of which costs a
full scan: roughly two minutes over 1,300 quotations against 216 sources. Scoped to one
directory it is around ten seconds, which is the shape that matters — the pre-edit check runs on
what you are editing, and the whole-corpus run is a periodic audit.
