#!/bin/sh
# Checks a profile-guided build by what rustc said about its profile (LOG, from rustc-pgo.sh).
# rustc only warns when the profile can't be read or doesn't match the code, and builds on
# without it; it says nothing at all about a function it finds no counts for unless built with
# -Cllvm-args=-pgo-warn-missing-function. Fails if the profile couldn't be read, or if the build
# for Python 3.14, the Python it was trained on, found any function changed since or any of
# fastpg's own functions (crate `_native`) without counts. The builds for the other Pythons find
# some of both (code that differs between Python versions, e.g. PyO3's), and use the rest.
# Run from the repo root:
#   sh .github/scripts/pgo-check.sh LOG
set -eu

log=$1

if [ ! -f "$log" ]; then
    echo "::error::$log is missing: the build didn't run rustc through rustc-pgo.sh"
    exit 1
fi
cut -d: -f1 "$log" | sort -u | while read -r python; do
    echo "$python: $(grep -c "^$python: .*hash mismatch" "$log") functions changed since the training," \
        "$(grep -c "^$python: .*no profile data" "$log") without counts"
done
status=0
if grep '\.profdata' "$log" | head -5 | grep .; then
    echo "::error::The profile couldn't be read"
    status=1
fi
if grep -E '^cpython-3\.14-64bit: .*(hash mismatch|no profile data available for function _ZN7_native)' "$log" \
    | head -5 | grep .; then
    echo "::error::The build for Python 3.14 doesn't match the profile it was trained for"
    status=1
fi
[ "$status" -eq 0 ] && echo "The build for Python 3.14 matches its profile"
exit "$status"
