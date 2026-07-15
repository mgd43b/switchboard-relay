# Releasing switchboard

Releases are automated by [`.github/workflows/release.yml`](.github/workflows/release.yml),
which fires on a `v*.*.*` tag and: builds the sdist + wheel, verifies the tag matches
`[project].version`, publishes to PyPI via **Trusted Publishing** (OIDC — no API tokens),
creates a GitHub Release, and bumps the Homebrew tap.

## One-time setup

### PyPI Trusted Publishing

1. In the GitHub repo: **Settings → Environments → New environment**, named exactly `pypi`.
   (Optional: add required reviewers so each release pauses for approval.)
2. On PyPI, **before the first release**, register a *pending* publisher at
   <https://pypi.org/manage/account/publishing/> → GitHub, matching the workflow exactly:
   - PyPI Project Name: `switchboard-mcp`
   - Owner: `mgd43b` · Repository: `switchboard`
   - Workflow name: `release.yml` · Environment name: `pypi`

   The first successful run consumes the pending publisher, creates the project, and
   converts it to a normal trusted publisher. No token is ever stored.

### Homebrew tap (optional)

The tap lives in a **separate** repo, `github.com/mgd43b/homebrew-taps` (the `homebrew-`
prefix is required — `brew tap mgd43b/taps` re-adds it). All `brew` commands below need
**macOS with Homebrew** installed.

1. Create the tap: `brew tap-new mgd43b/homebrew-taps` and push it to GitHub.
2. Scaffold the formula from the published sdist (do this after the first PyPI release):
   ```bash
   brew create --python --set-name switchboard --tap mgd43b/taps \
     https://files.pythonhosted.org/packages/source/s/switchboard-mcp/switchboard_mcp-<ver>.tar.gz
   ```
   Or start from [`packaging/homebrew/switchboard.rb`](packaging/homebrew/switchboard.rb)
   in this repo and copy it to `Formula/switchboard.rb` in the tap.
3. Generate the pinned dependency `resource` stanzas (this is the part no CI action can do
   for you — Homebrew builds in a no-network sandbox, so every dep must be checksummed):
   ```bash
   brew update-python-resources switchboard
   ```
4. Validate and commit:
   ```bash
   brew install --build-from-source switchboard
   brew test switchboard
   brew audit --strict --online --new switchboard
   ```
5. For the automated per-release bump, create a PAT with write access to
   `mgd43b/homebrew-taps` and add it to **this** repo's secrets as `HOMEBREW_TAP_TOKEN`.
   (The default `GITHUB_TOKEN` cannot push to another repo.) Without the secret, the
   `homebrew-bump` job simply skips.

Users then install with:

```bash
brew install mgd43b/taps/switchboard      # or: brew tap mgd43b/taps && brew install switchboard
```

## Cutting a release

1. Bump `[project].version` in `pyproject.toml` (and update any changelog). Commit + merge.
2. Tag and push:
   ```bash
   git tag v0.2.0
   git push origin v0.2.0
   ```
3. The workflow builds, publishes to PyPI, creates the GitHub Release, and (if the token is
   configured) opens a Homebrew bump.

### When dependencies change

The automated Homebrew bump only updates the formula's top-level `url`/`sha256`. If you
**added, removed, or upgraded a dependency** since the last release, regenerate the pinned
resources on macOS and open a PR to the tap:

```bash
brew update-python-resources mgd43b/taps/switchboard
brew audit --strict --online switchboard
```
