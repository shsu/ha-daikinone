#!/usr/bin/env bash
# Fails if legacy private-API references or any value from .env appear in the tree.
# Never prints secret values.
#
# Sections 2 and 3 are best-effort: they need a local .env / a local gitleaks and are
# therefore skipped on CI runners (they say so rather than implying they passed). The
# always-on gate for credential-shaped material is tests/test_repo_metadata.py, which
# runs inside pytest -- gitleaks cannot match the RSA-OAEP JWE Daikin issues anyway,
# because its jwt rule needs the second segment to start with "ey".
set -euo pipefail
cd "$(dirname "$0")/.."
fail=0

# 1) Legacy private-API hostnames/paths (the official integrator- host is allowed).
# -I skips binaries: a .pyc holding only the allowed host still prints an unfilterable
# "Binary file ... matches" line.
if grep -rnI --exclude-dir=__pycache__ -E "api\.daikinskyport\.com|api\.skyportcloud\.com|/users/auth/login|/deviceData" \
    custom_components docs README.md 2>/dev/null | grep -v "integrator-api.daikinskyport.com"; then
  echo "SECRET-SCAN FAIL: legacy private-API reference above"
  fail=1
fi

# 2) No value from .env may appear anywhere else in the tree.
if [ -f .env ]; then
  while IFS= read -r line; do
    case "$line" in ''|\#*) continue ;; esac
    v="${line#*=}"
    v="${v%\"}"; v="${v#\"}"; v="${v%\'}"; v="${v#\'}"
    [ "${#v}" -lt 8 ] && continue
    if grep -rqF -- "$v" custom_components tests docs scripts README.md Makefile pyproject.toml hacs.json 2>/dev/null; then
      echo "SECRET-SCAN FAIL: value of .env key '${line%%=*}' found in the tree"
      fail=1
    fi
  done < .env
else
  echo "secret-scan: SKIPPED .env-value comparison (no .env here)"
fi

# 3) gitleaks, when available (also runs as a pre-commit hook).
if command -v gitleaks >/dev/null 2>&1; then
  # `gitleaks dir` takes ONE path; extra args are ignored and it scans the cwd instead,
  # sweeping in the vendored .hassfest HA checkout. One call per directory.
  for d in custom_components tests docs scripts; do
    gitleaks dir --no-banner --redact "$d" >/dev/null || {
      echo "SECRET-SCAN FAIL: gitleaks findings in $d"; fail=1; }
  done
else
  echo "secret-scan: SKIPPED gitleaks (not installed)"
fi

[ "$fail" -eq 0 ] && echo "secret-scan: clean (checks marked SKIPPED above did not run)"
exit "$fail"
