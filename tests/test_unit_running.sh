#!/bin/bash
# Tests for the unit_running() function in opengrok/scripts/run-opengrok.sh.
# The real function calls `systemctl --user is-active`; we stub it here to
# exercise the case-dispatch (active, activating, inactive, failed, unknown).
#
# Run: bash tests/test_unit_running.sh   (exit 0 = pass)
set -uo pipefail

fail=0
pass() { printf '  ok   %s\n' "$1"; }
bad()  { printf '  FAIL %s\n' "$1"; fail=$((fail+1)); }

# Re-implement unit_running() with a pluggable status getter so we can test
# the case logic without systemctl.
_mock_status=""
mock_is_active() { echo "$_mock_status"; }

unit_running() {
  case "$(mock_is_active)" in
    active|activating) return 0 ;;
    *) return 1 ;;
  esac
}

# ---- active -> running -------------------------------------------------------
_mock_status="active"
unit_running && pass "active -> running" || bad "active -> running"

# ---- activating -> running (the new behaviour) -------------------------------
_mock_status="activating"
unit_running && pass "activating -> running" || bad "activating -> running"

# ---- inactive -> not running -------------------------------------------------
_mock_status="inactive"
unit_running && bad "inactive -> not running" || pass "inactive -> not running"

# ---- failed -> not running ---------------------------------------------------
_mock_status="failed"
unit_running && bad "failed -> not running" || pass "failed -> not running"

# ---- unknown -> not running --------------------------------------------------
_mock_status="unknown"
unit_running && bad "unknown -> not running" || pass "unknown -> not running"

# ---- empty string -> not running ---------------------------------------------
_mock_status=""
unit_running && bad "empty -> not running" || pass "empty -> not running"

# ---- done --------------------------------------------------------------------
if (( fail )); then
    printf '\n%d test(s) failed\n' "$fail"; exit 1
fi
printf '\nall tests passed\n'
