# Releasing switchboard-relay

Versioning and releases are automated with
[release-please](https://github.com/googleapis/release-please). It reads the
[Conventional Commits](https://www.conventionalcommits.org/) on `main` and keeps a
**release PR** open that bumps the version in `pyproject.toml` and updates
`CHANGELOG.md`. Merging that PR creates the `vX.Y.Z` tag + GitHub Release, and the
publish job in [`.github/workflows/release.yml`](.github/workflows/release.yml) builds and
pushes to PyPI via **Trusted Publishing** (OIDC — no API tokens).

## One-time setup

### PyPI Trusted Publishing

1. In the GitHub repo: **Settings → Environments → New environment**, named exactly `pypi`.
   (Optional: add required reviewers so each publish pauses for approval.)
2. On PyPI, **before the first release**, register a *pending* publisher at
   <https://pypi.org/manage/account/publishing/> → GitHub, matching the workflow exactly:
   - PyPI Project Name: `switchboard-relay`
   - Owner: `mgd43b` · Repository: `switchboard-relay`
   - Workflow name: `release.yml` · Environment name: `pypi`

   The first successful run consumes the pending publisher, creates the project, and
   converts it to a normal trusted publisher. No token is ever stored.

### Homebrew tap (optional)

The tap lives in a **separate** repo, `github.com/mgd43b/homebrew-taps` (the `homebrew-`
prefix is required — `brew tap mgd43b/taps` re-adds it). Everything below needs **macOS with
Homebrew**.

1. Create the tap once: `brew tap-new mgd43b/homebrew-taps` and push it to GitHub.
2. After the first PyPI release, run the update script (see below) — it seeds
   `Formula/switchboard-relay.rb` from [`packaging/homebrew/switchboard-relay.rb`](packaging/homebrew/switchboard-relay.rb),
   generates the pinned dependency `resource` stanzas, and pushes.

Users then install with:

```bash
brew install mgd43b/taps/switchboard-relay   # or: brew tap mgd43b/taps && brew install switchboard-relay
```

## Cutting a release

1. Land your changes on `main` as Conventional Commits (`feat:`, `fix:`, `chore:`, …).
2. release-please opens/updates a PR titled **"chore(main): release X.Y.Z"**. Review it —
   the version bump and CHANGELOG are computed from the commit types
   (`feat` → minor, `fix` → patch; pre-1.0 stays in `0.x`).
3. **Merge the release PR.** That creates the tag + GitHub Release and triggers the PyPI
   publish automatically.

> **First release:** the manifest baselines at `0.1.0`, so the first release PR proposes the
> next bump from your commits. To force a specific first version, add a `Release-As: 0.1.0`
> line to a commit body on `main`.

### Homebrew (per release, on macOS)

Once the release is on PyPI, update the tap formula — this bumps `url`/`sha256` **and**
regenerates the pinned `resource` stanzas (which no CI action can do in Homebrew's no-network
sandbox):

```bash
./scripts/update-tap.sh 0.2.0            # or --dry-run to preview, --skip-test to skip the build test
```

The script verifies the sdist is on PyPI, computes its SHA256, runs
`brew update-python-resources`, optionally test-installs, then commits + pushes to the tap.
