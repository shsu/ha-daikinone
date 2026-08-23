"""Repository metadata conformance: hacs.json, manifest.json, LICENSE, README.md.

These are the checks HACS and hassfest make about the repository itself, kept local so
`make check` catches a broken manifest without cloning home-assistant/core.
"""

from __future__ import annotations

import json
from pathlib import Path
import re

from awesomeversion import AwesomeVersion
import pytest

REPO_ROOT = Path(__file__).parent.parent
INTEGRATION_DIR = REPO_ROOT / "custom_components" / "daikinone"


@pytest.fixture(name="hacs_json", scope="module")
def hacs_json_fixture() -> dict:
    """Parsed hacs.json."""
    return json.loads((REPO_ROOT / "hacs.json").read_text(encoding="utf-8"))


@pytest.fixture(name="manifest", scope="module")
def manifest_fixture() -> dict:
    """Parsed manifest.json."""
    return json.loads((INTEGRATION_DIR / "manifest.json").read_text(encoding="utf-8"))


def test_hacs_json_keys(hacs_json: dict) -> None:
    """hacs.json declares the keys HACS needs to render this repository."""
    assert hacs_json["name"] == "Daikin One"
    assert isinstance(hacs_json["homeassistant"], str)
    assert AwesomeVersion(hacs_json["homeassistant"]) >= AwesomeVersion("2025.12.0")
    assert hacs_json["render_readme"] is True


def test_manifest_identity(manifest: dict) -> None:
    """The manifest identifies the integration exactly as the plan specifies."""
    assert manifest["domain"] == "daikinone"
    assert manifest["name"] == "Daikin One"
    assert manifest["integration_type"] == "hub"
    assert manifest["iot_class"] == "cloud_polling"
    assert manifest["config_flow"] is True


def test_manifest_codeowners(manifest: dict) -> None:
    """integration-owner: at least one GitHub handle owns this integration."""
    codeowners = manifest["codeowners"]
    assert codeowners, "manifest codeowners must not be empty"
    assert all(owner.startswith("@") for owner in codeowners)


@pytest.mark.parametrize("key", ["documentation", "issue_tracker"])
def test_manifest_urls(manifest: dict, key: str) -> None:
    """Documentation and issue tracker are https URLs."""
    assert manifest[key].startswith("https://")


def test_manifest_no_requirements(manifest: dict) -> None:
    """dependency-transparency: no runtime dependencies beyond HA's own."""
    assert manifest["requirements"] == []


def test_manifest_version_parses(manifest: dict) -> None:
    """HACS requires a parseable version on a custom integration manifest."""
    version = AwesomeVersion(manifest["version"])
    assert version.valid
    assert version >= AwesomeVersion("1.0.0")


def test_manifest_quality_scale_declared(manifest: dict) -> None:
    """The manifest declares the tier the quality_scale.yaml ledger backs up."""
    assert manifest["quality_scale"] in {"bronze", "silver", "gold", "platinum"}


def test_license_is_mit() -> None:
    """LICENSE is the MIT text the README attributes."""
    text = (REPO_ROOT / "LICENSE").read_text(encoding="utf-8")
    assert text.startswith("MIT License")
    assert "Copyright (c) 2026 Steven Hsu" in text


def test_readme_is_substantial() -> None:
    """README.md exists and is a real document, not a stub."""
    readme = REPO_ROOT / "README.md"
    assert readme.is_file()
    assert readme.stat().st_size > 2000


@pytest.mark.parametrize(
    "literal",
    [
        "integrator-api.daikinskyport.com",
        "Integrator Token",
        "API key",
        "180",
        "15 second",
        "more than 3",
    ],
)
def test_readme_documents_api_contract(literal: str) -> None:
    """The README states the API host, credential names and Daikin's usage limits verbatim."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert literal in readme


@pytest.mark.parametrize(
    "doc",
    ["FEATURE_MATRIX.md", "MIGRATION.md", "ARCHITECTURE.md", "TROUBLESHOOTING.md"],
)
def test_docs_present(doc: str) -> None:
    """The documentation set the README links to actually ships."""
    path = REPO_ROOT / "docs" / doc
    assert path.is_file()
    assert path.stat().st_size > 500


def test_readme_links_resolve() -> None:
    """Every relative markdown link in the README points at a file that exists."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    targets = re.findall(r"\]\((?!https?://|#)([^)#]+)", readme)
    missing = [t for t in targets if not (REPO_ROOT / t).exists()]
    assert not missing, f"README links to missing files: {missing}"


# --- Credential hygiene -------------------------------------------------------------
# A real Daikin Integrator Token is a ~1,780-char RSA-OAEP JWE. gitleaks cannot match one
# (its jwt rule requires the second segment to start with "ey"; a JWE's second segment is
# the encrypted content key), and scripts/secret_scan.sh's .env-value comparison is a no-op
# wherever .env is absent -- which is every CI runner. This scan is therefore the enforced
# gate behind "no credential ever lands in a fixture, doc or source file", and unlike the
# shell script it runs inside pytest, so it runs in CI and in the pre-push hook.
CREDENTIAL_SHAPES = (
    ("opaque blob", re.compile(r"[A-Za-z0-9_-]{100,}")),
    ("hex blob", re.compile(r"\b[0-9a-fA-F]{40,}\b")),
    ("bearer token", re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{16,}")),
)
SCANNED_SUFFIXES = {".ambr", ".cfg", ".json", ".md", ".py", ".sh", ".toml", ".txt", ".yaml", ".yml"}
SCANNED_TREES = (".github", "custom_components", "docs", "scripts", "tests")
SCANNED_FILES = (".pre-commit-config.yaml", "Makefile", "README.md", "hacs.json", "pyproject.toml")


def _scanned_paths() -> list[Path]:
    """Every tracked text file a credential could be pasted into."""
    paths = [REPO_ROOT / name for name in SCANNED_FILES]
    for tree in SCANNED_TREES:
        paths.extend(
            path
            for path in sorted((REPO_ROOT / tree).rglob("*"))
            if path.is_file() and path.suffix in SCANNED_SUFFIXES and "__pycache__" not in path.parts
        )
    return paths


def test_credential_scan_covers_the_tree() -> None:
    """Canary: the scan below is worthless if its walk comes back empty."""
    paths = _scanned_paths()
    assert len(paths) > 20
    for expected in ("tests/fixtures/token.json", "tests/conftest.py", "README.md"):
        assert REPO_ROOT / expected in paths


def test_credential_shapes_match_a_real_token_shape() -> None:
    """Canary: the patterns must still bite on the shape of the real credential."""
    jwe = ".".join(["eyJhbGciOiJSU0EtT0FFUCJ9", "A" * 342, "B" * 16, "C" * 1334, "D" * 22])
    assert len(jwe) > 1700
    assert CREDENTIAL_SHAPES[0][1].search(jwe)
    assert CREDENTIAL_SHAPES[1][1].search("deadbeef" * 6)
    # Built by concatenation so this file does not trip its own scan.
    assert CREDENTIAL_SHAPES[2][1].search("Authorization: " + "Bearer " + "a" * 20)


def test_no_credential_shaped_material_in_repo() -> None:
    """No opaque blob, long hex string or literal bearer token anywhere in the tree."""
    offenders = [
        f"{path.relative_to(REPO_ROOT)}: {label} ({len(hit)} chars)"
        for path in _scanned_paths()
        for label, pattern in CREDENTIAL_SHAPES
        for hit in pattern.findall(path.read_text(encoding="utf-8"))
    ]
    assert not offenders, f"credential-shaped material found: {offenders}"


def test_no_password_in_json_fixtures() -> None:
    """Password auth is forbidden, so no fixture may carry a password field."""
    offenders = [
        path.name
        for path in sorted((REPO_ROOT / "tests" / "fixtures").glob("*.json"))
        if "password" in path.read_text(encoding="utf-8").lower()
    ]
    assert not offenders, f"password field in fixtures: {offenders}"
