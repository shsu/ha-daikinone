"""Validate custom_components/daikinone/quality_scale.yaml.

hassfest skips quality_scale.yaml for custom integrations, so this test is the gate:
every rule from hassfest 2026.8.3 `script/hassfest/quality_scale.py` must be listed with
status `done` or `exempt` + comment, and the manifest tier must be backed by the ledger.
"""

from __future__ import annotations

import json
from pathlib import Path
import re

from homeassistant.components.sensor import SensorDeviceClass
import pytest
import yaml

from custom_components.daikinone.binary_sensor import BINARY_SENSORS
from custom_components.daikinone.coordinator import WRITE_ERROR_KEYS
from custom_components.daikinone.select import SELECTS
from custom_components.daikinone.sensor import SENSORS
from custom_components.daikinone.switch import SCHEDULE

REPO_ROOT = Path(__file__).parent.parent
INTEGRATION_DIR = REPO_ROOT / "custom_components" / "daikinone"
QUALITY_SCALE_FILE = INTEGRATION_DIR / "quality_scale.yaml"

# Verbatim from hassfest 2026.8.3 script/hassfest/quality_scale.py ALL_RULES.
TIER_RULES: dict[str, tuple[str, ...]] = {
    "bronze": (
        "action-setup",
        "appropriate-polling",
        "brands",
        "common-modules",
        "config-flow",
        "config-flow-test-coverage",
        "dependency-transparency",
        "docs-actions",
        "docs-conditions",
        "docs-high-level-description",
        "docs-installation-instructions",
        "docs-removal-instructions",
        "docs-triggers",
        "entity-event-setup",
        "entity-unique-id",
        "has-entity-name",
        "runtime-data",
        "test-before-configure",
        "test-before-setup",
        "unique-config-entry",
    ),
    "silver": (
        "action-exceptions",
        "config-entry-unloading",
        "docs-configuration-parameters",
        "docs-installation-parameters",
        "entity-unavailable",
        "integration-owner",
        "log-when-unavailable",
        "parallel-updates",
        "reauthentication-flow",
        "test-coverage",
    ),
    "gold": (
        "devices",
        "diagnostics",
        "discovery",
        "discovery-update-info",
        "docs-data-update",
        "docs-examples",
        "docs-known-limitations",
        "docs-supported-devices",
        "docs-supported-functions",
        "docs-troubleshooting",
        "docs-use-cases",
        "dynamic-devices",
        "entity-category",
        "entity-device-class",
        "entity-disabled-by-default",
        "entity-translations",
        "exception-translations",
        "icon-translations",
        "reconfiguration-flow",
        "repair-issues",
        "stale-devices",
    ),
    "platinum": (
        "async-dependency",
        "inject-websession",
        "strict-typing",
    ),
}
TIER_ORDER = ("bronze", "silver", "gold", "platinum")
ALL_RULES = tuple(rule for tier in TIER_ORDER for rule in TIER_RULES[tier])

# Rules that may legitimately be exempt for a cloud-account integration with no custom
# actions, triggers or conditions. Anything else claiming `exempt` is a regression.
ALLOWED_EXEMPT = {
    "action-setup",
    "docs-actions",
    "docs-triggers",
    "docs-conditions",
    "discovery",
    "discovery-update-info",
}

# Every entity of the integration except climate, which HA icons from the domain itself.
ICONABLE_ENTITIES = (
    ("binary_sensor", BINARY_SENSORS),
    ("sensor", SENSORS),
    ("select", SELECTS),
    ("switch", (SCHEDULE,)),
)

# parallel-updates: the value each platform must declare. 0 leaves the read-only platforms
# unthrottled; 1 serialises the write platforms so two commands never race on one device.
PARALLEL_UPDATES_BY_MODULE: dict[str, int] = {
    "climate.py": 1,
    "sensor.py": 0,
    "binary_sensor.py": 0,
    "switch.py": 1,
    "select.py": 1,
}

# docs-* rule -> the exact README heading (lowercased) that implements it. Matched against
# the heading list, not the whole document, so renaming or deleting a section fails here.
README_HEADINGS: dict[str, str] = {
    "docs-installation-instructions": "installation",
    "docs-removal-instructions": "removing the integration",
    "docs-configuration-parameters": "configuration parameters",
    "docs-installation-parameters": "prerequisites",
    "docs-data-update": "data updates",
    "docs-examples": "examples",
    "docs-known-limitations": "known limitations",
    "docs-supported-devices": "supported devices",
    "docs-supported-functions": "supported functionality",
    "docs-troubleshooting": "troubleshooting",
    "docs-use-cases": "use cases",
    "docs-actions": "actions",
}

# docs-* rule -> a phrase that must appear in the README body; these two rules are prose,
# not a section. docs-actions is exempt on the grounds that the README says so explicitly.
README_PHRASES: dict[str, str] = {
    "docs-high-level-description": "daikin one open api",
    "docs-actions": "does not provide custom actions",
}


@pytest.fixture(name="ledger", scope="module")
def ledger_fixture() -> dict:
    """The `rules:` mapping from quality_scale.yaml."""
    data = yaml.safe_load(QUALITY_SCALE_FILE.read_text(encoding="utf-8"))
    assert isinstance(data, dict), "quality_scale.yaml must be a mapping"
    rules = data["rules"]
    assert isinstance(rules, dict), "quality_scale.yaml must have a `rules:` mapping"
    return rules


@pytest.fixture(name="readme", scope="module")
def readme_fixture() -> str:
    """README.md lowercased for keyword greps."""
    return (REPO_ROOT / "README.md").read_text(encoding="utf-8").lower()


@pytest.fixture(name="readme_headings", scope="module")
def readme_headings_fixture(readme: str) -> set[str]:
    """Every markdown heading in the README, lowercased and stripped of its hashes."""
    return set(re.findall(r"^#{1,6}\s+(?:\d+\.\s+)?(.+?)\s*$", readme, re.MULTILINE))


def _status(value: object) -> str:
    return value["status"] if isinstance(value, dict) else str(value)


def test_no_unknown_rules(ledger: dict) -> None:
    """The ledger lists no rule hassfest does not know about."""
    assert not set(ledger) - set(ALL_RULES)


@pytest.mark.parametrize("rule", ALL_RULES)
def test_rule_present_and_valid(ledger: dict, rule: str) -> None:
    """Every hassfest rule is listed with a valid, non-todo status."""
    assert rule in ledger, f"quality_scale.yaml is missing rule {rule}"
    status = _status(ledger[rule])
    assert status in {"done", "exempt"}, f"{rule} has status {status!r}; expected done or exempt"


@pytest.mark.parametrize("rule", ALL_RULES)
def test_exempt_rules_are_justified(ledger: dict, rule: str) -> None:
    """Exempt rules carry a comment and are on the reviewed exemption list."""
    value = ledger.get(rule)
    if _status(value) != "exempt":
        return
    assert rule in ALLOWED_EXEMPT, f"{rule} claims exempt but is not a reviewed exemption"
    assert isinstance(value, dict)
    assert value.get("comment", "").strip(), f"{rule} is exempt without a comment"


def test_manifest_tier_is_backed_by_the_ledger() -> None:
    """Every rule at or below the declared manifest tier is done or exempt."""
    manifest = json.loads((INTEGRATION_DIR / "manifest.json").read_text(encoding="utf-8"))
    tier = manifest["quality_scale"]
    assert tier in TIER_ORDER
    ledger = yaml.safe_load(QUALITY_SCALE_FILE.read_text(encoding="utf-8"))["rules"]
    required = [r for t in TIER_ORDER[: TIER_ORDER.index(tier) + 1] for r in TIER_RULES[t]]
    unmet = [r for r in required if _status(ledger.get(r)) not in {"done", "exempt"}]
    assert not unmet, f"manifest declares {tier} but these rules are not satisfied: {unmet}"


@pytest.mark.parametrize(("module", "expected"), sorted(PARALLEL_UPDATES_BY_MODULE.items()))
def test_parallel_updates_declared_in_every_platform(module: str, expected: int) -> None:
    """parallel-updates: each platform declares PARALLEL_UPDATES, at the value it needs.

    Parsed rather than grepped, so a commented-out, renamed or wrongly-valued declaration
    fails instead of passing on the identifier alone.
    """
    path = INTEGRATION_DIR / module
    assert path.is_file(), f"platform module not implemented yet: {module}"
    declared = re.findall(r"^PARALLEL_UPDATES\s*=\s*(\d+)$", path.read_text(encoding="utf-8"), re.MULTILINE)
    assert declared == [str(expected)], f"{module} must declare PARALLEL_UPDATES = {expected}, found {declared}"


@pytest.mark.parametrize(("rule", "heading"), sorted(README_HEADINGS.items()))
def test_readme_covers_docs_rule(readme_headings: set[str], rule: str, heading: str) -> None:
    """Each docs-* rule marked done has its own README section, under the expected heading."""
    assert heading in readme_headings, f"README has no `{heading}` section for {rule}"


@pytest.mark.parametrize(("rule", "phrase"), sorted(README_PHRASES.items()))
def test_readme_states_docs_rule_in_prose(readme: str, rule: str, phrase: str) -> None:
    """The two docs-* rules that are prose rather than a section say what they claim to."""
    assert phrase in readme, f"README does not state {phrase!r} for {rule}"


def test_translation_keys_used_in_code_exist_in_strings() -> None:
    """exception-translations: every key the code raises or files has a message, and no key is dead.

    Nothing else in the suite opens strings.json - the other tests only pin the key
    *string* raised in code - so a rename or deletion on the translation side would
    otherwise ship silently.
    """
    strings = json.loads((INTEGRATION_DIR / "strings.json").read_text(encoding="utf-8"))
    known = set(strings["exceptions"]) | set(strings["issues"])
    for entities in strings["entity"].values():
        known |= set(entities)

    # Literal keys on any line mentioning translation_key (covers `translation_key="x"`
    # and the `WRITE_ERROR_KEYS.get(code, "write_failed")` fallback), plus the mapping's
    # own values, which no other test pins.
    used = set(WRITE_ERROR_KEYS.values())
    for path in INTEGRATION_DIR.rglob("*.py"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if "translation_key" in line:
                used |= set(re.findall(r'"([a-z_]+)"', line))

    assert not used - known, f"used in code, missing from strings.json: {sorted(used - known)}"
    assert not known - used, f"defined in strings.json, never used by the code: {sorted(known - used)}"

    en = json.loads((INTEGRATION_DIR / "translations" / "en.json").read_text(encoding="utf-8"))
    assert en == strings, "translations/en.json has drifted from strings.json"


def test_icon_translations_are_used_and_resolve() -> None:
    """icon-translations: icons come from icons.json only, and every key names a live entity."""
    hardcoded = [
        f"{path.name}:{number}"
        for path in INTEGRATION_DIR.rglob("*.py")
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if "_attr_icon" in line or re.search(r"\bicon\s*=", line)
    ]
    assert not hardcoded, f"icons must live in icons.json, not in code: {hardcoded}"

    icons = json.loads((INTEGRATION_DIR / "icons.json").read_text(encoding="utf-8"))["entity"]
    strings = json.loads((INTEGRATION_DIR / "strings.json").read_text(encoding="utf-8"))["entity"]
    dead = [f"{domain}.{key}" for domain, keys in icons.items() for key in keys if key not in strings.get(domain, {})]
    assert not dead, f"icons.json keys that match no entity: {dead}"


def test_entities_that_get_no_icon_from_a_device_class_bring_their_own() -> None:
    """icon-translations: every entity HA cannot icon from its device class has an icon key."""
    icons = json.loads((INTEGRATION_DIR / "icons.json").read_text(encoding="utf-8"))["entity"]
    missing = {
        f"{domain}.{description.translation_key}"
        for domain, descriptions in ICONABLE_ENTITIES
        for description in descriptions
        # SensorDeviceClass.ENUM carries no icon of its own, so it counts as no device class.
        if description.device_class in (None, SensorDeviceClass.ENUM)
        and description.translation_key not in icons.get(domain, {})
    }
    assert not missing, f"no icon from a device class and none of its own: {sorted(missing)}"
