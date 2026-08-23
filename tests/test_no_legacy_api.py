"""Guard: the legacy private Skyport API must never enter runtime code.

This test fails the build if anyone reintroduces the unofficial API hostname, the
password login endpoint, the private deviceData endpoint, or password-based auth
outside the two modules that legitimately reference the legacy password key
(config-entry migration and reauth cleanup).
"""

from __future__ import annotations

from pathlib import Path
import re

INTEGRATION_DIR = Path(__file__).parent.parent / "custom_components" / "daikinone"

# The official host is integrator-api.daikinskyport.com, so the legacy hostname is only
# forbidden when NOT preceded by "integrator-".
FORBIDDEN_EVERYWHERE = (
    re.compile(r"(?<!integrator-)api\.daikinskyport\.com"),
    re.compile(r"api\.skyportcloud\.com"),
    re.compile(r"/users/auth/login"),
    re.compile(r"/users/auth/token"),
    re.compile(r"/deviceData"),
)

# CONF_PASSWORD may appear only where the legacy field is detected/removed.
PASSWORD_ALLOWED_FILES = {"__init__.py", "config_flow.py"}

# A migrated entry keeps its legacy password until reauthentication succeeds, so
# diagnostics must name the key to redact it. Naming it is allowed there; importing
# CONF_PASSWORD (i.e. actually handling the credential) is not.
PASSWORD_REDACT_ONLY_FILES = {"diagnostics.py"}


def test_no_legacy_api_hostnames() -> None:
    """No runtime module may reference the private Skyport API."""
    assert INTEGRATION_DIR.is_dir()
    offenders: list[str] = []
    for path in sorted(INTEGRATION_DIR.rglob("*")):
        if path.suffix not in {".py", ".json"} or not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        offenders.extend(f"{path.name}: {needle.pattern}" for needle in FORBIDDEN_EVERYWHERE if needle.search(text))
    assert not offenders, f"legacy private-API references found: {offenders}"


def test_password_only_in_migration_modules() -> None:
    """Password handling exists solely to migrate/clean up legacy entries."""
    offenders: list[str] = []
    for path in sorted(INTEGRATION_DIR.rglob("*.py")):
        if path.name in PASSWORD_ALLOWED_FILES:
            continue
        text = path.read_text(encoding="utf-8")
        if path.name in PASSWORD_REDACT_ONLY_FILES:
            if "CONF_PASSWORD" in text:
                offenders.append(path.name)
            continue
        if "CONF_PASSWORD" in text or '"password"' in text:
            offenders.append(path.name)
    assert not offenders, f"password references outside migration modules: {offenders}"
