#!/usr/bin/env python3
"""Read-only live verification of the Daikin One Open API against a real account.

Credentials are read by this script itself, so no secret ever passes through a shell command
line, a log, or another tool's output. Sources, in order: DAIKINONE_* environment variables,
an ``.env.test`` file in the repository root when one exists, or a 1Password item fetched with
the 1Password CLI when ``--op-item NAME`` is given (field labels must match the variable
names). Shapes are validated BEFORE any network call, because the two long credentials are
easy to swap.

Nothing secret is ever printed: not the email, API key, integrator token or access token, and
not the device ids, device names or location names. Devices and locations appear as stable
aliases (``dev-1``, ``loc-1``) assigned in the order the API returns them.

    uv run python scripts/live_check.py                    # read-only, <= 120 s
    uv run python scripts/live_check.py --op-item homeassistant   # credentials from 1Password
    uv run python scripts/live_check.py --write-test       # toggles scheduleEnabled, then restores it
    uv run python scripts/live_check.py --bad-token --bad-key
    uv run python scripts/live_check.py --self-test        # offline assertions, no network, no .env

Daikin's usage limits are obeyed: one request in flight at a time (urllib is synchronous),
at least 1 s between calls, and 16 s of settle time before reading back a write.

Exit codes: 0 = all checks ran, 1 = a live check failed, 2 = credentials missing or misshapen.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any
import urllib.error
import urllib.request

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = REPO_ROOT / ".env.test"
SPEC_FILE = REPO_ROOT / "tests" / "spec" / "daikin_open_api.json"
# Documented message enum (mirrors the spec snapshot); safe to echo verbatim.
DOCUMENTED_MESSAGES = {
    "Success",
    "NotAuthorizedException",
    "Invalid request body",
    "DeviceOfflineException",
    "Write sent",
}

BASE_URL = "https://integrator-api.daikinskyport.com"
TOKEN_PATH = "/v1/token"
DEVICES_PATH = "/v1/devices"

REQUIRED_KEYS = ("DAIKINONE_USER_EMAIL", "DAIKINONE_API_KEY", "DAIKINONE_INTEGRATOR_TOKEN")

MIN_SECONDS_BETWEEN_CALLS = 1.0
REQUEST_TIMEOUT = 30
GLOBAL_TIMEOUT = 120.0
SETTLE_SECONDS = 16.0

JWE_SEGMENTS = 5
MIN_TOKEN_LENGTH = 500
MAX_API_KEY_LENGTH = 200

STATUS_MEANING = {
    200: "OK",
    400: "Bad request - the integration maps this to InvalidCredentialsError on /v1/token",
    401: "Unauthorized - accessToken/credentials not valid -> InvalidCredentialsError",
    403: "Forbidden - integratorApiKey not valid -> InvalidApiKeyError",
    404: "Not found (undocumented) -> InvalidRequestError",
    415: "Unsupported Media Type -> InvalidRequestError",
    429: "Too Many Requests -> RateLimitedError",
    500: "Internal error -> ServerError",
    503: "Service unavailable (undocumented) -> ServerError",
}


# --------------------------------------------------------------------------------------
# Pure helpers (covered by --self-test)
# --------------------------------------------------------------------------------------


def parse_env(text: str) -> dict[str, str]:
    """Parse KEY=VALUE lines, stripping surrounding quotes and ignoring comments/blanks."""
    env: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        env[key.strip()] = value
    return env


def looks_like_jwe(value: str) -> bool:
    """True when the value has the five dot-separated segments of a compact JWE."""
    return len(value.split(".")) == JWE_SEGMENTS


def shape_problems(env: dict[str, str]) -> list[str]:
    """Plain-language complaints about missing or swapped credentials. Empty list == fine."""
    problems = [f"{key} is missing." for key in REQUIRED_KEYS if not env.get(key)]
    if problems:
        return problems

    token = env["DAIKINONE_INTEGRATOR_TOKEN"]
    api_key = env["DAIKINONE_API_KEY"]

    if not looks_like_jwe(token) or len(token) <= MIN_TOKEN_LENGTH:
        problems.append(
            f"DAIKINONE_INTEGRATOR_TOKEN does not look like an Integrator Token: it has "
            f"{len(token.split('.'))} dot-separated segments and {len(token)} characters, but the "
            f"token issued by SkyportHome -> SkyportCare -> home integration -> get integration token "
            f"is a JWE with {JWE_SEGMENTS} segments and well over {MIN_TOKEN_LENGTH} characters."
        )
    if looks_like_jwe(api_key):
        problems.append(
            "DAIKINONE_API_KEY holds a JWE - that is the Integrator Token, not the API key. "
            "The API key is a short opaque string from the app developer menu "
            "(enable the developer menu, then SkyportCare -> home integration -> developer). "
            "The two values are almost certainly swapped."
        )
    elif len(api_key) >= MAX_API_KEY_LENGTH:
        problems.append(
            f"DAIKINONE_API_KEY is {len(api_key)} characters long; a developer API key is well under "
            f"{MAX_API_KEY_LENGTH}. Check you did not paste a token into this field."
        )
    return problems


def corrupt(value: str) -> str:
    """Return the value with its last four characters deliberately changed."""
    if len(value) < 4:
        return "xxxx"
    return value[:-4] + "".join("A" if char != "A" else "B" for char in value[-4:])


def fingerprint(value: str) -> str:
    """Short, non-reversible marker so two runs can be compared without revealing anything."""
    return hashlib.sha256(value.encode()).hexdigest()[:8]


def json_type(value: object) -> str:
    """JSON type name of a parsed value; bool is checked before int on purpose."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        return "list"
    return "object"


def status_meaning(status: int) -> str:
    """Documented meaning of an HTTP status, and the exception the integration maps it to."""
    return STATUS_MEANING.get(status, f"undocumented status {status}")


# --------------------------------------------------------------------------------------
# HTTP (one request at a time, >= 1 s apart)
# --------------------------------------------------------------------------------------


class LiveApi:
    """Minimal synchronous client. Never logs URLs with ids, bodies, or headers."""

    def __init__(self, email: str, api_key: str, integrator_token: str, deadline: float | None) -> None:
        self._email = email
        self._api_key = api_key
        self._integrator_token = integrator_token
        self._deadline = deadline
        self._last_call = 0.0
        self.access_token: str | None = None
        self.call_count = 0

    def _throttle(self) -> None:
        wait = MIN_SECONDS_BETWEEN_CALLS - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        if self._deadline is not None and time.monotonic() > self._deadline:
            raise TimeoutError("global timeout reached")

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        api_key: str | None = None,
        authorize: bool = True,
    ) -> tuple[int, Any]:
        """Perform one request; return (status, parsed body or None)."""
        self._throttle()
        headers = {"x-api-key": api_key if api_key is not None else self._api_key}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if authorize and self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"

        req = urllib.request.Request(f"{BASE_URL}{path}", data=data, headers=headers, method=method)
        self.call_count += 1
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                status, raw = resp.status, resp.read()
        except urllib.error.HTTPError as err:
            status, raw = err.code, err.read()
        except urllib.error.URLError as err:
            self._last_call = time.monotonic()
            raise ConnectionError(f"transport failure: {type(err.reason).__name__}") from err
        self._last_call = time.monotonic()

        try:
            parsed = json.loads(raw.decode() or "null")
        except ValueError, UnicodeDecodeError:
            parsed = None
        return status, parsed

    def fetch_token(self, *, integrator_token: str | None = None, api_key: str | None = None) -> tuple[int, Any]:
        """POST /v1/token. Overrides exist only for the --bad-token / --bad-key probes."""
        token = integrator_token if integrator_token is not None else self._integrator_token
        return self.request(
            "POST",
            TOKEN_PATH,
            {"email": self._email, "integratorToken": token},
            api_key=api_key,
            authorize=False,
        )


# --------------------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------------------


class Report:
    """Collects the redacted lines that make up the final summary block."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.failures: list[str] = []

    def add(self, line: str) -> None:
        self.lines.append(line)
        print(f"  {line}")

    def fail(self, line: str) -> None:
        self.failures.append(line)
        print(f"  FAIL: {line}")


def heading(title: str) -> None:
    print(f"\n== {title} " + "=" * max(0, 76 - len(title)))


def safe_message(payload: Any, documented: set[str]) -> str:
    """Echo a response message only when the docs list it; otherwise describe it."""
    if not isinstance(payload, dict):
        return "<no message>"
    value = payload.get("messages", payload.get("message"))
    if not isinstance(value, str):
        return "<no message>"
    if value in documented:
        return value
    return f"<undocumented message, {len(value)} chars>"


# --------------------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------------------


def check_token(api: LiveApi, report: Report) -> bool:
    """POST /v1/token and report the shape of the answer, never the token itself."""
    heading("POST /v1/token")
    status, payload = api.fetch_token()
    report.add(f"status: {status} ({status_meaning(status)})")
    if status != 200 or not isinstance(payload, dict):
        message = safe_message(payload, DOCUMENTED_MESSAGES)
        report.fail(f"token request failed: {status} ({message})")
        if message == "NotAuthorizedException":
            report.add(
                "hint: the API key was accepted (a bad key gives 403); Daikin rejected the "
                "email + integratorToken pair. Check that DAIKINONE_USER_EMAIL matches the "
                "Home App account email EXACTLY (it is case-sensitive), and that the "
                "Integrator Token is the most recent one (requesting a new token in the app "
                "invalidates older ones - re-request via SkyportCare > home integration)."
            )
        return False
    token = payload.get("accessToken")
    if not isinstance(token, str) or not token:
        report.fail("response has no usable accessToken")
        return False
    api.access_token = token
    report.add(f"accessToken: {len(token)} chars (value never printed)")
    report.add(f"accessTokenExpiresIn: {payload.get('accessTokenExpiresIn')!r}")
    report.add(f"tokenType: {payload.get('tokenType')!r}")
    unexpected = sorted(set(payload) - {"accessToken", "accessTokenExpiresIn", "tokenType"})
    report.add(f"unexpected response keys: {unexpected or 'none'}")
    return True


def check_devices(api: LiveApi, report: Report) -> list[tuple[str, str]]:
    """GET /v1/devices. Returns [(alias, device_id)] and reports only aliased data."""
    heading("GET /v1/devices")
    status, payload = api.request("GET", DEVICES_PATH)
    report.add(f"status: {status} ({status_meaning(status)})")
    if status != 200 or not isinstance(payload, list):
        report.fail("device listing did not return 200 with a JSON array")
        return []

    devices: list[tuple[str, str]] = []
    models: list[str] = []
    firmwares: list[str] = []
    for loc_index, location in enumerate(payload, start=1):
        if not isinstance(location, dict):
            report.fail(f"loc-{loc_index} is not an object")
            continue
        entries = location.get("devices") or []
        report.add(f"loc-{loc_index}: {len(entries)} device(s)")
        unexpected = sorted(set(location) - {"locationName", "devices"})
        if unexpected:
            report.add(f"loc-{loc_index}: unexpected location keys {unexpected}")
        for device in entries:
            if not isinstance(device, dict) or not device.get("id"):
                report.fail(f"loc-{loc_index} contains a device without an id")
                continue
            alias = f"dev-{len(devices) + 1}"
            devices.append((alias, str(device["id"])))
            models.append(str(device.get("model")))
            firmwares.append(str(device.get("firmwareVersion")))
            report.add(
                f"{alias} (loc-{loc_index}): model={device.get('model')!r} "
                f"firmware={device.get('firmwareVersion')!r} "
                f"has_name={'name' in device} id_len={len(str(device['id']))}"
            )
            extra = sorted(set(device) - {"id", "name", "model", "firmwareVersion"})
            if extra:
                report.add(f"{alias}: UNDOCUMENTED device keys {extra}")

    report.add(f"locations: {len(payload)}  devices: {len(devices)}")
    report.add(f"models: {sorted(set(models))}")
    report.add(f"firmware versions: {sorted(set(firmwares))}")
    return devices


def check_device_detail(api: LiveApi, report: Report, spec: dict[str, Any], devices: list[tuple[str, str]]) -> None:
    """GET /v1/devices/{id} for every device, sequentially, and diff against the spec."""
    documented = set(spec["endpoints"]["device"]["response_keys"])
    enums: dict[str, dict[str, str]] = spec["enums"]

    for alias, device_id in devices:
        heading(f"GET /v1/devices/{{id}} ({alias})")
        status, payload = api.request("GET", f"{DEVICES_PATH}/{device_id}")
        report.add(f"{alias}: status {status} ({status_meaning(status)})")
        if status != 200 or not isinstance(payload, dict):
            report.fail(f"{alias}: detail read did not return 200 with a JSON object")
            continue

        observed = {key: json_type(value) for key, value in sorted(payload.items())}
        report.add(f"{alias}: fields {observed}")
        unexpected = sorted(set(payload) - documented)
        missing = sorted(documented - set(payload))
        report.add(f"{alias}: UNDOCUMENTED fields: {unexpected or 'none'}")
        report.add(f"{alias}: documented fields MISSING: {missing or 'none'}")

        for field, table in enums.items():
            if field not in payload:
                continue
            value = payload[field]
            name = table.get(str(value), "UNDOCUMENTED VALUE")
            report.add(f"{alias}: {field}={value!r} -> {name}")

        em_heat = payload.get("modeEmHeatAvailable")
        report.add(f"{alias}: modeEmHeatAvailable arrived as {json_type(em_heat)} ({em_heat!r})")

        report.add(
            f"{alias}: setpointMinimum={payload.get('setpointMinimum')!r} "
            f"setpointMaximum={payload.get('setpointMaximum')!r} "
            f"setpointDelta={payload.get('setpointDelta')!r}"
        )


def check_bad_credential(api: LiveApi, report: Report, *, which: str, kwargs: dict[str, str]) -> None:
    """Repeat the token POST with one credential corrupted; report only the status."""
    heading(f"POST /v1/token with a corrupted {which}")
    status, _ = api.fetch_token(**kwargs)
    report.add(f"corrupted {which}: HTTP {status} -> {status_meaning(status)}")


def check_write(api: LiveApi, report: Report, spec: dict[str, Any], devices: list[tuple[str, str]]) -> None:
    """Toggle scheduleEnabled on the FIRST device only, then restore it."""
    heading("write test: scheduleEnabled toggle + restore (first device only)")
    if not devices:
        report.fail("write test skipped: no devices")
        return
    alias, device_id = devices[0]
    detail_path = f"{DEVICES_PATH}/{device_id}"
    schedule_path = f"{detail_path}/schedule"
    documented = set(spec["documented_messages"]) | {spec["write_success_message"]}

    status, payload = api.request("GET", detail_path)
    if status != 200 or not isinstance(payload, dict) or not isinstance(payload.get("scheduleEnabled"), bool):
        report.fail(f"{alias}: cannot read scheduleEnabled; write test aborted before any write")
        return
    original = payload["scheduleEnabled"]
    report.add(f"{alias}: original scheduleEnabled={original}")

    restored = True
    try:
        started = time.monotonic()
        status, payload = api.request("PUT", schedule_path, {"scheduleEnabled": not original})
        report.add(
            f"{alias}: PUT scheduleEnabled={not original} -> {status} "
            f"({safe_message(payload, documented)}) in {time.monotonic() - started:.2f}s"
        )
        if status != 200:
            report.fail(f"{alias}: toggle write rejected with {status}; nothing was changed")
            return
        restored = False

        time.sleep(SETTLE_SECONDS)
        status, payload = api.request("GET", detail_path)
        seen = payload.get("scheduleEnabled") if isinstance(payload, dict) else None
        report.add(f"{alias}: after {SETTLE_SECONDS:.0f}s settle, scheduleEnabled={seen!r} (expected {not original})")
        if seen is not (not original):
            report.fail(f"{alias}: toggle did not take effect within {SETTLE_SECONDS:.0f}s")

        started = time.monotonic()
        status, payload = api.request("PUT", schedule_path, {"scheduleEnabled": original})
        report.add(
            f"{alias}: PUT scheduleEnabled={original} (restore) -> {status} "
            f"({safe_message(payload, documented)}) in {time.monotonic() - started:.2f}s"
        )
        if status == 200:
            restored = True

        time.sleep(SETTLE_SECONDS)
        status, payload = api.request("GET", detail_path)
        seen = payload.get("scheduleEnabled") if isinstance(payload, dict) else None
        report.add(f"{alias}: after restore + {SETTLE_SECONDS:.0f}s settle, scheduleEnabled={seen!r}")
        if seen is not original:
            restored = False
            report.fail(f"{alias}: scheduleEnabled was NOT restored to {original}")
    finally:
        if not restored:
            print(f"  !! {alias}: attempting a final restore of scheduleEnabled={original}")
            try:
                status, _ = api.request("PUT", schedule_path, {"scheduleEnabled": original})
                report.add(f"{alias}: final restore attempt -> {status}")
            except (OSError, TimeoutError, ConnectionError) as err:
                report.fail(f"{alias}: final restore attempt failed ({type(err).__name__}); RESTORE MANUALLY")


# --------------------------------------------------------------------------------------
# Offline self-test, entry point
# --------------------------------------------------------------------------------------


def self_test() -> int:
    """Assertions over the pure helpers. No network, no .env, nothing printed but PASS."""
    env = parse_env("# comment\n\nA=1\nB = \"two\"\nC='three'\nBAD LINE\nD=has=equals\n")
    assert env == {"A": "1", "B": "two", "C": "three", "D": "has=equals"}, env

    assert looks_like_jwe("a.b.c.d.e")
    assert not looks_like_jwe("a.b.c")
    assert not looks_like_jwe("short-opaque-key")

    jwe = "a.b.c.d." + "x" * 600
    good = {
        "DAIKINONE_USER_EMAIL": "owner@example.com",
        "DAIKINONE_API_KEY": "short-opaque-key",
        "DAIKINONE_INTEGRATOR_TOKEN": jwe,
    }
    assert shape_problems(good) == []
    assert "is missing" in shape_problems({**good, "DAIKINONE_API_KEY": ""})[0]
    swapped = {**good, "DAIKINONE_API_KEY": jwe, "DAIKINONE_INTEGRATOR_TOKEN": "short-opaque-key"}
    problems = " ".join(shape_problems(swapped))
    assert "does not look like an Integrator Token" in problems
    assert "holds a JWE" in problems
    assert "swapped" in problems
    assert len(shape_problems({**good, "DAIKINONE_API_KEY": "k" * 250})) == 1

    assert corrupt("abcdefgh") == "abcdAAAA"
    assert corrupt("abcdAAAA") == "abcdBBBB"
    assert corrupt("abcdefgh") != "abcdefgh"
    assert len(corrupt("abcdefgh")) == len("abcdefgh")

    assert json_type(True) == "bool"
    assert json_type(1) == "int"
    assert json_type(1.5) == "float"
    assert json_type(None) == "null"
    assert json_type("x") == "str"

    assert safe_message({"message": "Write sent"}, {"Write sent"}) == "Write sent"
    assert "undocumented message" in safe_message({"messages": "secret-ish detail"}, {"Write sent"})
    assert safe_message(None, set()) == "<no message>"

    assert len(fingerprint("anything")) == 8

    print("self-test: PASS")
    return 0


def load_credentials(op_item: str | None) -> tuple[dict[str, str], str] | None:
    """Return (credentials, source label), or None when nothing provides them."""
    from_env = {key: os.environ[key] for key in REQUIRED_KEYS if os.environ.get(key)}
    if len(from_env) == len(REQUIRED_KEYS):
        return from_env, "environment variables"
    if ENV_FILE.exists():
        return parse_env(ENV_FILE.read_text(encoding="utf-8")), ENV_FILE.name
    if op_item:
        try:
            proc = subprocess.run(
                ["op", "item", "get", op_item, "--format", "json"],
                capture_output=True,
                text=True,
                timeout=90,
                check=True,
            )
        except FileNotFoundError:
            print("ABORT: the 1Password CLI (op) is not installed.")
            return None
        except subprocess.TimeoutExpired:
            print("ABORT: op stalled; approve the 1Password authorization prompt and re-run.")
            return None
        except subprocess.CalledProcessError as err:
            print(f"ABORT: op item get {op_item!r} failed: {err.stderr.strip()[:120]}")
            return None
        item = json.loads(proc.stdout)
        fields = {f.get("label"): f.get("value") for f in item.get("fields", []) if f.get("label")}
        return {key: fields[key] for key in REQUIRED_KEYS if fields.get(key)}, f"1Password item {op_item!r}"
    return None


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, validate credential shapes, then run the requested checks."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--write-test", action="store_true", help="toggle scheduleEnabled on the first device and restore it"
    )
    parser.add_argument("--bad-token", action="store_true", help="probe the status for a corrupted integrator token")
    parser.add_argument("--bad-key", action="store_true", help="probe the status for a corrupted API key")
    parser.add_argument(
        "--self-test", action="store_true", help="run offline assertions and exit (no network, no .env)"
    )
    parser.add_argument(
        "--op-item",
        default=os.environ.get("DAIKINONE_OP_ITEM"),
        help="1Password item to read DAIKINONE_* fields from when no env vars or .env.test exist",
    )
    args = parser.parse_args(argv)

    if args.self_test:
        return self_test()

    loaded = load_credentials(args.op_item)
    if loaded is None:
        print(
            f"ABORT: no credentials. Provide {', '.join(REQUIRED_KEYS)} as environment variables, "
            f"an {ENV_FILE.name} file, or --op-item <1Password item>."
        )
        return 2
    env, source = loaded
    problems = shape_problems(env)
    if problems:
        print(f"ABORT: credentials from {source} are not usable yet.\n")
        for problem in problems:
            print(f"  - {problem}")
        print("\nNo network request was made. Fix the source and re-run; no value was printed.")
        return 2

    spec = json.loads(SPEC_FILE.read_text(encoding="utf-8"))
    deadline = None if args.write_test else time.monotonic() + GLOBAL_TIMEOUT
    api = LiveApi(
        env["DAIKINONE_USER_EMAIL"],
        env["DAIKINONE_API_KEY"],
        env["DAIKINONE_INTEGRATOR_TOKEN"],
        deadline,
    )
    report = Report()

    print(f"Daikin One live check - {BASE_URL}")
    print(
        f"credentials: {source} (api-key fp {fingerprint(env['DAIKINONE_API_KEY'])}, "
        f"token fp {fingerprint(env['DAIKINONE_INTEGRATOR_TOKEN'])}); values are never printed"
    )

    started = time.monotonic()
    try:
        if check_token(api, report):
            devices = check_devices(api, report)
            check_device_detail(api, report, spec, devices)
            if args.bad_token:
                bad = corrupt(env["DAIKINONE_INTEGRATOR_TOKEN"])
                check_bad_credential(api, report, which="integrator token", kwargs={"integrator_token": bad})
            if args.bad_key:
                bad = corrupt(env["DAIKINONE_API_KEY"])
                check_bad_credential(api, report, which="API key", kwargs={"api_key": bad})
            if args.write_test:
                check_write(api, report, spec, devices)
    except (TimeoutError, ConnectionError) as err:
        report.fail(f"aborted: {err}")

    heading("REDACTED SUMMARY (safe to paste into a report)")
    print(f"host: {BASE_URL}")
    print(
        f"mode: {'write-test' if args.write_test else 'read-only'}"
        f"{' +bad-token' if args.bad_token else ''}{' +bad-key' if args.bad_key else ''}"
    )
    print(f"requests: {api.call_count}  elapsed: {time.monotonic() - started:.1f}s")
    for line in report.lines:
        print(f"  {line}")
    if report.failures:
        print(f"FAILURES ({len(report.failures)}):")
        for line in report.failures:
            print(f"  - {line}")
        return 1
    print("result: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
