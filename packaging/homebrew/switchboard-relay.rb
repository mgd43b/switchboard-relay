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
  # PyPI sdist (PEP 625 normalizes the name to underscores). Bump on each release.
  url "https://files.pythonhosted.org/packages/source/s/switchboard-relay/switchboard_relay-0.1.0.tar.gz"
  sha256 "REPLACE_WITH_SDIST_SHA256"
  license "MIT"

  livecheck do
    url :stable
    strategy :pypi
  end

  depends_on "python@3.13"
  # pydantic-core builds from its sdist via maturin, so Rust is needed at build time.
  depends_on "rust" => :build

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
