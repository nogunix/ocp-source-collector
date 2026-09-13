#!/bin/bash
# Static checks for the playbooks under ansible/ (casket-host.yml and its
# teardown). They install systemd units, relabel files for SELinux and open
# firewalld ports on a real host, so an actual run is not something CI can do
# -- same reason the pipeline scripts are only syntax-checked here. This
# guards what can be checked without a host: that the playbooks parse, that
# every module they call resolves, and that they pass ansible-lint.
#
# Rule exceptions live in .ansible-lint at the repo root, with the reason.
#
# Run: bash tests/test_ansible.sh   (exit 0 = pass)
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO" || exit 1

fail=0
pass() { printf '  ok   %s\n' "$1"; }
bad()  { printf '  FAIL %s\n' "$1"; fail=$((fail+1)); }

for cmd in ansible-playbook ansible-lint; do
  command -v "$cmd" >/dev/null 2>&1 || {
    echo "$cmd not found -- install 'ansible' (dnf) and 'ansible-lint' (pip)" >&2
    exit 1
  }
done

# --- the playbooks are the ones we expect -------------------------------------
# A playbook added to ansible/ without a line here would otherwise be checked
# by the lint pass below but never syntax-checked, so assert the set matches.
shopt -s nullglob
found=(ansible/*.yml)
expected=(ansible/casket-host-teardown.yml ansible/casket-host.yml)
if [ "${found[*]}" = "${expected[*]}" ]; then
  pass "ansible/ holds exactly the 2 known playbooks"
else
  bad "ansible/*.yml changed: got '${found[*]}', expected '${expected[*]}'"
fi

# --- syntax-check: parses, and every module/collection resolves ---------------
for pb in "${expected[@]}"; do
  if out=$(ansible-playbook -i ansible/inventory.ini --syntax-check "$pb" 2>&1); then
    pass "syntax-check $(basename "$pb")"
  else
    bad "syntax-check $(basename "$pb")"
    printf '%s\n' "$out" | sed 's/^/       /'
  fi
done

# --- an undefined module must actually be caught ------------------------------
# Guards the check above against silently degrading into a YAML-parse-only
# pass (e.g. if the collections stop being installed).
probe=$(mktemp -d)
trap 'rm -rf "$probe"' EXIT
cat > "$probe/bad.yml" <<'YML'
---
- name: Probe playbook that must fail syntax-check
  hosts: localhost
  gather_facts: false
  tasks:
    - name: Call a module that does not exist
      community.general.no_such_module_at_all:
        target: /tmp/nope
YML
if ansible-playbook -i ansible/inventory.ini --syntax-check "$probe/bad.yml" >/dev/null 2>&1; then
  bad "syntax-check does not catch an unresolvable module (collections missing?)"
else
  pass "syntax-check catches an unresolvable module"
fi

# --- ansible-lint -------------------------------------------------------------
# --offline: never reach out to galaxy; the collections come from the 'ansible'
# package and a network fetch would make this check flaky.
if out=$(ansible-lint --offline ansible/ 2>&1); then
  pass "ansible-lint ansible/"
else
  bad "ansible-lint ansible/"
  printf '%s\n' "$out" | grep -v '^WARNING' | sed 's/^/       /'
fi

# --- the skip list stays justified --------------------------------------------
# .ansible-lint turns off exactly one rule. If that grows, the reason for each
# addition belongs next to it, so make adding one a deliberate act.
skips=$(grep -cE '^\s+- [a-z]' .ansible-lint 2>/dev/null || echo 0)
if [ "$skips" -eq 1 ]; then
  pass ".ansible-lint skips exactly 1 rule"
else
  bad ".ansible-lint skip_list has $skips entries, expected 1 (document any addition)"
fi

echo
if [ "$fail" -eq 0 ]; then echo "all tests passed"; else echo "$fail test(s) failed"; fi
exit $((fail > 0))
