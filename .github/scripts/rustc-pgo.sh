#!/bin/sh
# The RUSTC_WRAPPER of the profile-guided builds (.github/workflows/release.yml): runs rustc as
# given, and adds the warnings it gives about the profile to RUSTC_PGO_LOG, each after the Python
# it was building for (maturin's PYO3_ENVIRONMENT_SIGNATURE, e.g. cpython-3.14-64bit). rustc only
# warns when the profile can't be read or doesn't match the code, and builds on without it;
# pgo-check.sh then fails on them. Not autocfg's: the probes some build scripts compile, which
# aren't part of the wheel and were never trained.
set -eu

err=$(mktemp)
trap 'rm -f "$err"' EXIT
: >> "$RUSTC_PGO_LOG"
status=0
"$@" 2> "$err" || status=$?
cat "$err" >&2
grep -E 'hash mismatch|profile data|\.profdata' "$err" | grep -v autocfg_ \
    | sed "s|^|${PYO3_ENVIRONMENT_SIGNATURE:-none}: |" >> "$RUSTC_PGO_LOG" || true
exit "$status"
