#!/usr/bin/env python3
"""Compare the live Daikin One Open API docs against tests/spec/daikin_open_api.json.

Daikin publishes no OpenAPI document, so the integration's ground truth is a hand-transcribed
snapshot. This script re-extracts the machine-checkable parts of the three doc pages -- the
"Last updated" dates, the endpoint paths, the Data Definitions enum tables and the API USAGE
LIMITS numbers -- and diffs them against that snapshot.

Not run by pytest (it needs the network): ``make spec-drift``, plus a weekly CI job.

Exit codes: 0 = snapshot matches the live docs, 1 = drift (see the printed diff), 2 = fetch failed.
"""

from __future__ import annotations

import difflib
import html
import json
from pathlib import Path
import re
import sys
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

SNAPSHOT_PATH = Path(__file__).resolve().parent.parent / "tests" / "spec" / "daikin_open_api.json"
TIMEOUT = 30
USER_AGENT = "ha-daikinone-spec-drift/1.0"

# Enum tables tracked in the snapshot (the docs define more rows; only these are enums we model).
TRACKED_ENUMS = ("mode", "modeLimit", "equipmentStatus", "fan", "fanCirculate", "fanCirculateSpeed")


def fetch(url: str) -> str:
    """Return the page body, or raise URLError/OSError on failure."""
    # Fixed https URLs from the snapshot; no user input reaches urlopen.
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=TIMEOUT) as response:
        return str(response.read().decode("utf-8", errors="replace"))


def strip_comments(page: str) -> str:
    """Drop HTML comments: Daikin hides withdrawn enum values (equipmentStatus 6-9) in them."""
    return re.sub(r"<!--.*?-->", " ", page, flags=re.DOTALL)


def visible_text(page: str) -> str:
    """Tags to newlines, entities decoded -- what a reader actually sees."""
    without_code = re.sub(r"<(script|style)\b.*?</\1>", " ", strip_comments(page), flags=re.DOTALL | re.IGNORECASE)
    return html.unescape(re.sub(r"<[^>]+>", "\n", without_code))


def last_updated(text: str) -> str:
    """The page's own 'Last updated: <date>' stamp."""
    match = re.search(r"Last updated:\s*([A-Za-z]+ \d{1,2},\s*\d{4})", text)
    return match.group(1).strip() if match else "MISSING"


def endpoint_paths(text: str) -> set[str]:
    """Every /v1/... path on the page, with the id placeholder normalised to {id}."""
    paths: set[str] = set()
    for raw in re.findall(r"/v1/[A-Za-z/{}<>$ ]*", text):
        path = re.sub(r"(<[^>]*>|\$\{[^}]*\}|\{[^}]*\})", "{id}", raw).split(" ")[0]
        path = path.rstrip("/.,")
        if path.startswith("/v1/") and len(path) > len("/v1/"):
            paths.add(path)
    return paths


def enum_tables(page: str) -> dict[str, dict[str, str]]:
    """Extract '<n>: <label>' rows from the Data Definitions table, keyed by field name."""
    tables: dict[str, dict[str, str]] = {}
    for row in re.findall(r"<tr\b.*?</tr>", strip_comments(page), flags=re.DOTALL):
        name = re.search(r"<th[^>]*>\s*([A-Za-z]+)\s*</th>", row)
        if name is None:
            continue
        body = html.unescape(re.sub(r"<[^>]+>", "\n", row))
        values = dict(re.findall(r"^\s*(\d+)\s*:\s*(.+?)\s*$", body, flags=re.MULTILINE))
        if values:
            tables[name.group(1)] = values
    return tables


def usage_limits(text: str) -> dict[str, int | None]:
    """The three numbers in the API USAGE LIMITS callout."""

    def number(pattern: str, scale: int = 1) -> int | None:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        return int(match.group(1)) * scale if match else None

    return {
        "write_settle_seconds": number(r"minimum of (\d+) seconds"),
        "poll_min_seconds": number(r"quicker than once every (\d+) minutes", 60),
        "max_open_requests": number(r"more than (\d+) open HTTP requests"),
    }


def matches_label(snapshot_name: str, live_label: str) -> bool:
    """True when the live label still says the same thing as the snapshot's name.

    The snapshot stores identifier-shaped names ('overcool_dehum', 'always_on'); the docs write
    prose ('overcool for dehum', 'always on', 'on a schedule'). A snapshot name matches when its
    words appear, in order, in the live label -- so a reworded label still passes but a renamed
    one ('cool' -> 'cooling') is reported.
    """
    wanted = [word for word in re.split(r"[^a-z0-9]+", snapshot_name.lower()) if word]
    live = [word for word in re.split(r"[^a-z0-9]+", live_label.lower()) if word]
    iterator = iter(live)
    return all(word in iterator for word in wanted)


def canonical_enum(snapshot: dict[str, str], live: dict[str, str]) -> dict[str, str]:
    """Rewrite live labels that still mean the snapshot's name, so the diff shows real changes."""
    return {
        key: snapshot[key] if key in snapshot and matches_label(snapshot[key], label) else label
        for key, label in live.items()
    }


def report(title: str, expected: dict[str, Any] | list[str], actual: dict[str, Any] | list[str]) -> list[str]:
    """Unified diff of one section; empty list when the two sides agree."""
    left = json.dumps(expected, indent=2, sort_keys=True).splitlines()
    right = json.dumps(actual, indent=2, sort_keys=True).splitlines()
    return list(difflib.unified_diff(left, right, fromfile=f"snapshot/{title}", tofile=f"live/{title}", lineterm=""))


def main() -> int:
    """Fetch, extract, diff. Returns the process exit code."""
    snapshot = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    source = snapshot["source"]
    urls = {page: source[page] for page in ("overview", "documentation", "examples")}

    pages: dict[str, str] = {}
    for page, url in urls.items():
        try:
            pages[page] = fetch(url)
        except (URLError, OSError, ValueError) as err:  # network down, DNS, TLS, timeout
            print(f"FETCH FAILED: {page} <{url}>: {type(err).__name__}: {err}")
            print("Cannot check for spec drift without network access.")
            return 2

    texts = {page: visible_text(body) for page, body in pages.items()}
    diffs: list[str] = []

    diffs += report(
        "last_updated",
        {page: source[f"{page}_last_updated"] for page in urls},
        {page: last_updated(text) for page, text in texts.items()},
    )

    documented = {name: spec["path"] for name, spec in snapshot["endpoints"].items()}
    diffs += report(
        "endpoints",
        sorted(set(documented.values())),
        sorted(endpoint_paths(texts["documentation"]) | endpoint_paths(texts["examples"])),
    )

    live_enums = enum_tables(pages["documentation"])
    diffs += report(
        "enums",
        {name: snapshot["enums"][name] for name in TRACKED_ENUMS},
        {name: canonical_enum(snapshot["enums"][name], live_enums.get(name, {})) for name in TRACKED_ENUMS},
    )

    diffs += report("limits", snapshot["limits"], usage_limits(texts["documentation"]))

    print(f"snapshot: {SNAPSHOT_PATH}")
    for page, url in urls.items():
        print(f"fetched:  {url} ({len(pages[page])} bytes, last updated {last_updated(texts[page])})")

    if diffs:
        print("\nDRIFT DETECTED - the live docs no longer match the snapshot:\n")
        print("\n".join(diffs))
        print("\nUpdate tests/spec/daikin_open_api.json (and the code it drives), then re-run.")
        return 1

    print("\nNo drift: dates, endpoints, enum tables and usage limits all match the snapshot.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
