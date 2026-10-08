# Bundled InstSci runtime

This directory contains the InstSci runtime source, package data, build metadata,
and its original MIT license. It is loaded by `scripts/download_institutional.py`
relative to this skill, without requiring a separate source checkout.

Snapshot date: 2026-10-08.
Base Git revision: `bae03c76c1fe6703bcab1c46c874c19ba9b93289`.

The snapshot also includes the working-tree changes in
`instsci/publisher_batch.py` and `instsci/publisher_profiles.py` used in the local
Social Forces download tests. It is not an unmodified release of the base revision.
The runtime files were copied without further edits.

The download skill supplies its own headless-first browser wrapper, visible login
handoff, dedicated profile, session restoration, and password-manager settings.
Use that wrapper for this skill; the upstream CLI is not its default entrypoint.
Institutional routing policy is retained in
`instsci/data/institutional_identity_policy.json`; the wrapper's display behavior
follows the user's requested headless-first workflow.

Excluded: repository metadata, tests, development files, browser binaries, Python
environments, caches, configuration containing user state, login profiles, cookies,
passwords, downloaded papers, and logs. Python dependencies are declared in
`pyproject.toml`. CloakBrowser downloads its platform-specific browser separately.

See `LICENSE` for the upstream copyright and redistribution terms.
