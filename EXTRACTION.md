# Staged extraction: not yet authoritative

This private repository is a **staged candidate** extracted from
[elgansayer/elgl](https://github.com/elgansayer/elgl) for issue
[elgl#9822](https://github.com/elgansayer/elgl/issues/9822).

**Elgl `main` remains the single authoritative source.** The deployed updater,
services, pins and package installation all still use Elgl. Do not deploy from
this repository, and do not point any factory instance or updater at it, until
the cutover receipts listed in #9822 exist. Those receipts are build/test, config
load, preserved state identities, dry-run deploy, one bounded task, recovery and
exercised rollback.

- Source: `elgansayer/elgl` `main`, first at `3a1e240261e710d8b8e105d65fe05be9eca32d77`, synced to `7f082afea1afe6b1e302553919c215725c468f88` (2026-11-04; deterministic re-filter, earlier history unchanged)
- Method: `git filter-repo` 2.47.0 with `--paths-from-file extraction/paths.txt`.
  It preserves history for those paths only.
- Commit map: `extraction/elgl-commit-map.txt`, in the format
  `old-elgl-sha new-sha`. Use it to port fixes merged into Elgl after the
  extraction.
- Audit: no credential patterns were found in the filtered history. The
  `.env.example` and `factory.env.example` files contain placeholders only.
- Actions: disabled on this repository until the entitlement and charging check
  is done.
- Packaging: the distribution is still `repo-factory`, the import namespace is
  still `openhands_factory`, and both are unchanged.

Project-specific code stays in its project repositories. For Neptune that is
`tools/factory`, `tools/saturn_lab` and `docs/restart`. The HelloTalk
application, the Workout application and private media are not included.
