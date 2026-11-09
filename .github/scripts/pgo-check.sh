#!/bin/sh
# Checks the profile-guided build of the wheel for Python 3.14, the Python the profile was
# trained on, by what rustc said about the profile (LOG, from rustc-pgo.sh). rustc only warns
# when the profile can't be read or doesn't match the code, and builds on without it; it says
# nothing at all about a function it finds no counts for unless built with
# -Cllvm-args=-pgo-warn-missing-function. Fails if the profile couldn't be read, or the build
# found any function changed since the training, or any of fastpg's own functions (crate
# `_native`) without counts. (The other Pythons' builds find some of both, in code that differs
# between Python versions, e.g. PyO3's, and use the rest.) Run from the repo root:
#   sh .github/scripts/pgo-check.sh LOG
set -eu

log=$1

if [ ! -f "$log" ]; then
    echo "::error::$log is missing: the build didn't run rustc through rustc-pgo.sh"
    exit 1
fi
unreadable=$(grep -c '\.profdata' "$log" || true)
changed=$(grep -c 'hash mismatch' "$log" || true)
missing=$(grep -c 'no profile data' "$log" || true)
own=$(grep -c 'no profile data available for function _ZN7_native' "$log" || true)
echo "Python 3.14's build: $changed functions changed since the training; $missing without counts," \
    "$own of them fastpg's own"
if [ "$unreadable" -ne 0 ] || [ "$changed" -ne 0 ] || [ "$own" -ne 0 ]; then
    grep -E '\.profdata|hash mismatch|_ZN7_native' "$log" | head -5 | cut -c1-400
    echo "::error::The wheel for Python 3.14 doesn't match the profile it was trained for"
    exit 1
fi
