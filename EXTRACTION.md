# Extraction record

This public repository is the **authoritative Factory source**. It was extracted
from [elgansayer/elgl](https://github.com/elgansayer/elgl) (elgl#9822) and the
host updater has deployed from here since 2026-11-05. Target repositories hold no
Factory code; the Factory works on them from outside.

## Add a repository

On the host, as root: `scripts/add-repo.sh OWNER/REPO [INSTANCE]`. It clones the
repository, writes a generic instance profile (no ready label or author gate,
ci-gated merges, generic system prompt), limits issue creation to collaborators
and starts `repo-factory@INSTANCE` with its watchdog. The repository's required
GitHub CI is the merge gate. Every active instance follows each deployed runtime
through `repo-factory-follow.path`.

## History

- Source: `elgansayer/elgl` `main`, first at `3a1e240261e710d8b8e105d65fe05be9eca32d77`, synced to `9216c98103cc1e6e776abc3bef78b378fed9520f` (2026-11-05; fresh filter merged onto staged main, repository-only files re-applied)
- Method: `git filter-repo` 2.47.0 with `--paths-from-file extraction/paths.txt`.
  It preserves history for those paths only.
- Commit map: `extraction/elgl-commit-map.txt`, in the format
  `old-elgl-sha new-sha`. Use it to port fixes merged into Elgl after the
  extraction.
- Audit: no credential patterns were found in the filtered history. The
  `.env.example` and `factory.env.example` files contain placeholders only.
- Actions: enabled 2026-11-05 after the repository was made public (free
  minutes). `ci.yml` is the canonical gate the host updater checks.
- Packaging: the distribution is still `repo-factory`, the import namespace is
  still `openhands_factory`, and both are unchanged.

Project-specific code stays in its project repositories. For Neptune that is
`tools/factory`, `tools/saturn_lab` and `docs/restart`. The HelloTalk
application, the Workout application and private media are not included.
