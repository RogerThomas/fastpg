#!/bin/sh
# Merges the raw counts pgo-train.sh wrote to RAW_DIR into PROFILE, the file a profile-guided
# build takes (-Cprofile-use), with the toolchain's own llvm-profdata (rustup's llvm-tools, the
# same LLVM as the compiler). Fails if there are no counts, or the profile is empty or unreadable:
# rustc only warns about a bad profile and builds without it. Also writes PROFILE.functions, the
# names of the functions it has counts for, for pgo-check.sh. Run from the repo root:
#   sh .github/scripts/pgo-merge.sh RAW_DIR PROFILE
set -eu

raw=$1
profile=$2

rustup component add llvm-tools
host=$(rustc -vV | sed -n 's/^host: //p')
profdata="$(rustc --print sysroot)/lib/rustlib/$host/bin/llvm-profdata"

set -- "$raw"/*.profraw
if [ ! -f "$1" ]; then
    echo "::error::No profile counts in $raw"
    exit 1
fi
"$profdata" merge --failure-mode=any -o "$profile" "$@"
summary=$("$profdata" show "$profile")
echo "$summary"
total=$(echo "$summary" | sed -n 's/^Total count: //p')
if [ "${total:-0}" -eq 0 ]; then
    echo "::error::$profile has no counts"
    exit 1
fi
# Names as the profile keeps them; a file-local function's is prefixed with its file and a `;`.
"$profdata" show --all-functions "$profile" | sed -n 's/^  \(.*\):$/\1/p' | sed 's/.*;//' \
    | sort -u > "$profile.functions"
echo "$profile: $(wc -c < "$profile") bytes, from $# raw files, $(wc -l < "$profile.functions") functions"
