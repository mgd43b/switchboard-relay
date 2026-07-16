# Homebrew formula for switchboard-relay.
#
# This is the SOURCE TEMPLATE. The live formula lives in the tap repo at
# github.com/mgd43b/homebrew-taps → Formula/switchboard-relay.rb. Copy this there,
# then fill the `url`/`sha256` and (auto-)generate the `resource` stanzas with:
#
#     brew update-python-resources switchboard-relay      # macOS + Homebrew required
#
# See RELEASING.md for the full first-time and per-release procedure. Users then
# install with:  brew install mgd43b/taps/switchboard-relay
class SwitchboardRelay < Formula
  include Language::Python::Virtualenv

  desc "Shared, durable messaging channel for independent Claude Code sessions"
  homepage "https://github.com/mgd43b/switchboard-relay"
  # PyPI sdist "Source" URL -- the canonical hash-path form that
  # FormulaAudit/PyPiUrls requires (the /packages/source/ shorthand is
  # rejected by `brew style`). scripts/update-tap.sh resolves the real URL and
  # sha256 from PyPI's JSON API on each release and replaces both lines below.
  url "https://files.pythonhosted.org/packages/REPLACED/BY/UPDATE-TAP/switchboard_relay-0.0.0.tar.gz"
  sha256 "REPLACE_WITH_SDIST_SHA256"
  license "MIT"

  livecheck do
    url :stable
    strategy :pypi
  end

  # Build deps come first (FormulaAudit/DependencyOrder). pydantic-core builds
  # from its sdist via maturin, so Rust is needed at build time.
  depends_on "rust" => :build
  depends_on "python@3.13"

  # ---- BEGIN auto-generated resources ------------------------------------
  # Do NOT hand-edit. Regenerate with `brew update-python-resources switchboard-relay`.
  # The real file lists mcp, pydantic, pydantic-core, anyio, starlette, httpx,
  # sse-starlette, uvicorn, and all their transitive deps (~20-25 stanzas).
  #
  #   resource "pydantic-core" do
  #     url "https://files.pythonhosted.org/packages/.../pydantic_core-x.y.z.tar.gz"
  #     sha256 "..."
  #   end
  # ---- END auto-generated resources --------------------------------------

  def install
    virtualenv_install_with_resources
  end

  test do
    assert_match "switchboard-relay", shell_output("#{bin}/switchboard-relay --version")
  end
end
