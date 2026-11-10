#!/bin/bash
# entitled-extras.sh -- install %prep tools that no UBI repo carries, from the
# real RHEL repos, then drop the entitlement again. Runs inside one RUN step so
# no registration state reaches a committed layer.
#
# Why: el9 kernels from 5.14.0-427.140 on, and el10 6.12, run
# `make olddefconfig` in %prep, which builds kconfig's lexer and parser and so
# needs flex and bison. coreutils, pam, xz, quota and grub2 run autopoint
# (gettext-devel). All three are RHEL AppStream packages, absent from UBI.
#
# The credentials file is the same ~/.config/casket/rhsm.env the collect image
# uses (RHSM_ORG + RHSM_ACTIVATION_KEY, or RHSM_USERNAME + RHSM_PASSWORD),
# passed as a build secret so it never lands in a layer or in the image
# history. Without it, the build still succeeds: these packages are skipped,
# and the affected SRPMs stay _incomplete as before.
set -euo pipefail

SECRET=/run/secrets/rhsm
PKGS=(${ENTITLED_EXTRAS:-flex bison gettext-devel})

if [[ ! -s "$SECRET" ]]; then
    echo "[entitled-extras] no rhsm secret -- skipping ${PKGS[*]}" >&2
    exit 0
fi

dnf install -y --setopt=install_weak_deps=False subscription-manager >/dev/null

cleanup() {
    subscription-manager unregister >/dev/null 2>&1 || true
    subscription-manager clean >/dev/null 2>&1 || true
    rm -f /etc/yum.repos.d/redhat.repo
    dnf remove -y subscription-manager >/dev/null 2>&1 || true
}
trap cleanup EXIT

set -a; . "$SECRET"; set +a
if [[ -n "${RHSM_ORG:-}" && -n "${RHSM_ACTIVATION_KEY:-}" ]]; then
    subscription-manager register --force --org "$RHSM_ORG" \
        --activationkey "$RHSM_ACTIVATION_KEY" >/dev/null
elif [[ -n "${RHSM_USERNAME:-}" && -n "${RHSM_PASSWORD:-}" ]]; then
    subscription-manager register --force --username "$RHSM_USERNAME" \
        --password "$RHSM_PASSWORD" >/dev/null
else
    echo "[entitled-extras] rhsm secret has no usable credentials" >&2
    exit 1
fi

# Only the base AppStream/BaseOS repos are needed; the UBI repos stay enabled
# for the dependency closure.
dnf install -y --setopt=install_weak_deps=False "${PKGS[@]}"
echo "[entitled-extras] installed: $(rpm -q "${PKGS[@]}" | tr '\n' ' ')" >&2
