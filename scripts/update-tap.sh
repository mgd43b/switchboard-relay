#!/usr/bin/env bash
set -euo pipefail

# Update the Homebrew tap formula for a new switchboard-relay release.
#
# switchboard-relay is a Python package, so unlike a source-tarball (Rust/Go) formula
# this does more than bump url+sha256: it regenerates the pinned dependency
# `resource` stanzas with `brew update-python-resources` (Homebrew builds in a
# no-network sandbox, so every dep must be a checksummed resource). Requires
# macOS + Homebrew, and the package must already be published to PyPI.

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# Configuration
REPO_OWNER="mgd43b"
PYPI_NAME="switchboard-relay"   # PyPI distribution name
FORMULA_NAME="switchboard-relay"    # formula file / installed name
TAP_REPO="homebrew-taps"
TAP_PATH="$(brew --repository 2>/dev/null)/Library/Taps/${REPO_OWNER}/${TAP_REPO}"
FORMULA_PATH="${TAP_PATH}/Formula/${FORMULA_NAME}.rb"

# Locate this repo (to seed the formula from the template on first run).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
TEMPLATE="${REPO_ROOT}/packaging/homebrew/${FORMULA_NAME}.rb"

DRY_RUN=false
SKIP_TEST=false

info()    { echo -e "${BLUE}ℹ${NC} $*"; }
success() { echo -e "${GREEN}✓${NC} $*"; }
warn()    { echo -e "${YELLOW}⚠${NC} $*"; }
error()   { echo -e "${RED}✗${NC} $*" >&2; }

# Single EXIT handler: always remove the temp download dir, and if we still hold
# a formula backup (i.e. we exited before finishing the edit) restore it, so a
# failure part-way through never leaves the tap formula half-modified.
TEMP_DIR=""
BACKUP=""
cleanup() {
    local ec=$?
    [[ -n "$TEMP_DIR" ]] && rm -rf "$TEMP_DIR"
    if [[ -n "$BACKUP" && -f "$BACKUP" ]]; then
        mv -f "$BACKUP" "$FORMULA_PATH"
        warn "Restored the original formula after an incomplete run."
    fi
    return $ec
}
trap cleanup EXIT

usage() {
    cat <<EOF
Usage: $0 <version> [options]

Update the Homebrew tap formula for a new switchboard-relay release.

Arguments:
  version       Version number (e.g., 0.1.0) without the 'v' prefix

Options:
  --dry-run     Show the formula changes without committing or pushing
  --skip-test   Skip the local 'brew install --build-from-source' test
  -h, --help    Show this help message

Requires macOS + Homebrew, and switchboard-relay <version> published to PyPI.

Examples:
  $0 0.1.0
  $0 0.2.0 --dry-run
EOF
    exit 0
}

# -- parse args -------------------------------------------------------------
[[ $# -eq 0 ]] && { error "No version specified"; usage; }

VERSION=""
while [[ $# -gt 0 ]]; do
    case $1 in
        --dry-run)  DRY_RUN=true; shift ;;
        --skip-test) SKIP_TEST=true; shift ;;
        -h|--help)  usage ;;
        *)
            if [[ -z "$VERSION" ]]; then VERSION="$1"; else error "Unknown argument: $1"; usage; fi
            shift ;;
    esac
done

if ! [[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    error "Invalid version format: '${VERSION}' (expected X.Y.Z, e.g. 0.1.0)"
    exit 1
fi

command -v brew >/dev/null 2>&1 || { error "Homebrew is required (macOS)."; exit 1; }

info "Updating Homebrew tap for ${FORMULA_NAME} v${VERSION}"
[[ "$DRY_RUN" == true ]] && warn "DRY RUN — no commit or push"

# -- step 1: resolve the sdist URL + sha256 from PyPI -----------------------
# FormulaAudit/PyPiUrls requires the canonical hash-path "Source" URL (the
# predictable /packages/source/ shorthand is rejected by `brew style`), and the
# hash path can't be constructed from the version -- so ask PyPI's JSON API.
PYPI_JSON_URL="https://pypi.org/pypi/${PYPI_NAME}/${VERSION}/json"
info "Resolving sdist via PyPI: ${PYPI_JSON_URL}"
SDIST_META="$(curl -sfL "$PYPI_JSON_URL" | python3 -c '
import json, sys
data = json.load(sys.stdin)
for u in data["urls"]:
    if u["packagetype"] == "sdist":
        print(u["url"])
        print(u["digests"]["sha256"])
        break
' 2>/dev/null || true)"
SDIST_URL="$(sed -n 1p <<<"$SDIST_META")"
SHA256="$(sed -n 2p <<<"$SDIST_META")"
if [[ -z "$SDIST_URL" || -z "$SHA256" ]]; then
    error "sdist not found on PyPI. Publish ${PYPI_NAME} ${VERSION} first (tag v${VERSION})."
    exit 1
fi
success "sdist: ${SDIST_URL}"

# -- step 2: download and verify the advertised sha256 -----------------------
TEMP_DIR="$(mktemp -d)"  # cleaned up (and backup restored) by the EXIT trap
TARBALL="${TEMP_DIR}/$(basename "$SDIST_URL")"
curl -sLf "$SDIST_URL" -o "$TARBALL" || { error "Download failed"; exit 1; }
DOWNLOADED_SHA="$(shasum -a 256 "$TARBALL" | awk '{print $1}')"
if [[ "$DOWNLOADED_SHA" != "$SHA256" ]]; then
    error "sha256 mismatch: PyPI advertises ${SHA256} but the download is ${DOWNLOADED_SHA}"
    exit 1
fi
success "SHA256 verified: ${SHA256}"

# -- step 3: ensure the tap and formula exist -------------------------------
if [[ ! -d "$TAP_PATH" ]]; then
    info "Tapping ${REPO_OWNER}/${TAP_REPO}..."
    brew tap "${REPO_OWNER}/${TAP_REPO#homebrew-}"
fi
git -C "$TAP_PATH" pull --ff-only >/dev/null 2>&1 || true

if [[ ! -f "$FORMULA_PATH" ]]; then
    warn "Formula not in tap yet — seeding from ${TEMPLATE}"
    [[ -f "$TEMPLATE" ]] || { error "Template missing: ${TEMPLATE}"; exit 1; }
    cp "$TEMPLATE" "$FORMULA_PATH"
fi

BACKUP="${FORMULA_PATH}.bak"
cp "$FORMULA_PATH" "$BACKUP"

# -- step 4: bump the main package url + sha256 -----------------------------
# Replace ONLY the first url/sha256 (the package's own -- two-space indented at
# the formula top); resource stanzas are regenerated wholesale in step 5. The
# whole url line is replaced because the canonical hash path shares no stable
# prefix between releases.
awk -v new="$SDIST_URL" '
    !done && /^  url "/ { sub(/url "[^"]*"/, "url \"" new "\""); done=1 }
    { print }
' "$FORMULA_PATH" > "${FORMULA_PATH}.tmp" && mv "${FORMULA_PATH}.tmp" "$FORMULA_PATH"
awk -v new="$SHA256" '
    !done && /sha256 "/ { sub(/sha256 "[^"]*"/, "sha256 \"" new "\""); done=1 }
    { print }
' "$FORMULA_PATH" > "${FORMULA_PATH}.tmp" && mv "${FORMULA_PATH}.tmp" "$FORMULA_PATH"
success "Bumped url + sha256"

# -- step 5: regenerate dependency resource stanzas -------------------------
# --ignore-main-package-cooldown lets us resolve a version published <24h ago
# (Homebrew otherwise ignores just-uploaded packages). Deps still respect the
# cooldown; it is a no-op once the release has aged. Only applies to custom taps.
info "Regenerating Python resources (brew update-python-resources)..."
brew update-python-resources --ignore-main-package-cooldown \
    "${REPO_OWNER}/${TAP_REPO#homebrew-}/${FORMULA_NAME}"
success "Resources regenerated"

info "Formula changes:"
git -C "$TAP_PATH" --no-pager diff -- "$FORMULA_PATH" || true

# -- step 6: local install test ---------------------------------------------
if [[ "$SKIP_TEST" == false && "$DRY_RUN" == false ]]; then
    info "Testing formula (brew install --build-from-source; may take a few minutes)..."
    brew list "$FORMULA_NAME" &>/dev/null && brew uninstall "$FORMULA_NAME" || true
    if brew install --build-from-source "${REPO_OWNER}/${TAP_REPO#homebrew-}/${FORMULA_NAME}"; then
        INSTALLED="$("$FORMULA_NAME" --version 2>/dev/null | awk '{print $2}')"
        if [[ "$INSTALLED" == "$VERSION" ]]; then
            success "Install test passed (${FORMULA_NAME} ${INSTALLED})"
        else
            error "Version mismatch: expected ${VERSION}, got '${INSTALLED}'"
            exit 1  # EXIT trap restores the formula
        fi
    else
        error "Formula install failed"
        exit 1  # EXIT trap restores the formula
    fi
else
    warn "Skipping install test"
fi

# -- step 7: commit + push (or restore on dry-run) --------------------------
if [[ "$DRY_RUN" == true ]]; then
    warn "DRY RUN — not committing (the EXIT trap restores the original formula)"
    exit 0
fi

rm -f "$BACKUP"  # committing for real: drop the backup so the trap won't restore
git -C "$TAP_PATH" add "$FORMULA_PATH"
git -C "$TAP_PATH" commit -m "${FORMULA_NAME} ${VERSION}"
git -C "$TAP_PATH" push
success "Tap updated — users can now: brew update && brew upgrade ${FORMULA_NAME}"
