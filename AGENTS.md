# Agent instructions — `bu-tools`

**Owner:** Lynton Davidson  
**Scope:** this standalone repository and its code when consumed as a submodule

This file is the canonical shared instruction source for coding agents working on
`bu-tools`. Claude Code imports it through `CLAUDE.md`; do not duplicate these
rules there.

## Start here

1. Read `README.md`.
2. Inspect the implementation and example config relevant to the task.
3. If the task comes from a consuming repository, read that repository's instructions
   and actual config before judging compatibility. Consumer policy overrides examples
   here.

## Repository boundary

- This repository owns reusable prose-integrity **engines**.
- Consuming repositories own their wordlists, paths, thresholds, exemptions and
  publication policy.
- Do not hardcode a consumer's terminology, file layout, client facts or private
  operational details into the shared engines.
- `configs/example.register.config.json` demonstrates the schema; it is not a
  canonical ruleset for any consumer.
- Keep runtime code on the Python standard library unless Lynton explicitly approves
  a dependency.
- Public repository state must contain no credentials, private source material,
  consumer datasets or identifying operational detail.

## Change discipline

- Preserve existing CLI flags and config behaviour unless the task explicitly
  authorises a breaking change.
- Prefer additive, optional config fields with safe defaults.
- Treat exemptions as visible evidence: report their reason rather than hiding them.
- Treat source fidelity as prior to register correction. Run `quotecheck` before
  changing prose that reports or paraphrases a held source.
- Update `README.md` when commands, config shape, output meaning, compatibility or
  performance characteristics change.
- Add the smallest focused fixture or regression check that proves a behavioural fix.
  A real-corpus observation may motivate a change, but private corpus material does
  not belong here.

## Verification

For every Python change, run at minimum:

```bash
python3 -m py_compile register/scan.py register/quotecheck.py
python3 -m unittest discover -v
python3 register/scan.py --help
python3 register/quotecheck.py --help
```

Then run the narrow fixture or reproduction for the behaviour changed. Before a
consumer adopts the commit, run that consumer's configured scan, source-fidelity
check and any equivalence/regression gate it defines.

Report exactly what ran and what did not. GitHub Actions runs the repository suite on
the oldest and newest supported Python versions; that suite does not replace a
consumer's own policy and corpus checks.

## Adoption by consumers

Pinning is deliberate:

1. Land and verify the engine change here.
2. Record the resulting `bu-tools` commit.
3. Bump each consumer's submodule pointer separately and intentionally.
4. Run the consumer-specific gates before accepting the bump.
5. Record the upstream commit in the consumer commit or PR.

Never make a consumer silently track `main`, and do not update consumer pointers
unless the authorised task includes that repository.

## Git and collaboration

- One agent leads a workstream; a reviewer verifies the highest-risk assumptions
  rather than creating a competing implementation.
- Use explicit paths and inspect the diff before committing.
- Do not force-push, reset destructively or rewrite shared history without explicit
  authorisation.
- Direct changes to `main` require explicit authorisation; otherwise use a branch
  and review.
- Lynton retains authority over release, licensing and changes to the engine/config
  boundary.
