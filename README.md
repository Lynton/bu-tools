# bu-tools

Shared prose-integrity tooling, single-sourced across the projects.

The engine lives here; **the rules live in the consuming repo.** A holiday-let site and
a philosophy corpus need different wordlists and different exemptions — *genuine
understanding* is a tell in one and a term of art in the other — so the split is
engine/config, not one-size-fits-all.

Standard library only, deliberately: no consuming repo gains a dependency, and the tools
run in CI and in mobile sessions without an install step.

## Tools

| Tool | Answers |
|---|---|
| `register/scan.py` | Where is the copy reaching for AI-tells, and how dense is the cadence? |
| `register/quotecheck.py` | Do the quotations match the held sources — and does any passage keep speaking in a source's words *after* the closing mark? |

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

## The SPILL check

`quotecheck` flags a passage that closes its quotation mark and then keeps using the
source's exact words. Those words are still the author's; a register sweep that treats
them as own voice rewrites the source.

Added 2026-08-29, after precisely that happened: a three-word register pass stripped
Sekrst's *genuine understanding* and Birch's *might genuinely be achieved*, both sitting
just outside the marks, both the authors' own terms — Birch's the verbatim statement of
his Challenge Two. The lesson generalises past that pass: **a word outside the quotation
marks is not thereby own voice.**
