#!/bin/sh
# Runs commands at once, each through `sh -c` with its output in a log of its own, and says as
# each one ends whether it passed. Then it shows the logs: those that passed as collapsed groups
# (in GitHub Actions), then those that failed in full, each with an error naming it. Fails if
# any command failed.
#   sh .github/scripts/parallel.sh LABEL COMMAND [LABEL COMMAND]...
# e.g. sh .github/scripts/parallel.sh "Python 3.13" "UV_PYTHON=3.13 uv run pytest" \
#        "Python 3.14" "UV_PYTHON=3.14 uv run pytest"
set -eu

if [ "$#" -eq 0 ] || [ $(($# % 2)) -ne 0 ]; then
    echo "usage: $0 LABEL COMMAND [LABEL COMMAND]..." >&2
    exit 2
fi

logs=$(mktemp -d)
trap 'rm -rf "$logs"' EXIT

n=0
while [ "$#" -gt 0 ]; do
    n=$((n + 1))
    printf '%s\n' "$1" > "$logs/$n.label"
    (
        start=$(date +%s)
        status=0
        sh -c "$2" > "$logs/$n.log" 2>&1 < /dev/null || status=$?
        took=$(($(date +%s) - start))
        printf '%s %s\n' "$status" "$took" > "$logs/$n.status"
        if [ "$status" -eq 0 ]; then
            printf '%s: passed in %dm%02ds\n' "$1" $((took / 60)) $((took % 60))
        else
            printf '%s: FAILED (exit %s) in %dm%02ds\n' "$1" "$status" $((took / 60)) $((took % 60))
        fi
    ) &
    shift 2
done
wait

failed=""
i=0
while [ "$i" -lt "$n" ]; do
    i=$((i + 1))
    if [ "$(cut -d' ' -f1 "$logs/$i.status")" -eq 0 ]; then
        echo "::group::$(cat "$logs/$i.label") (passed)"
        cat "$logs/$i.log"
        echo "::endgroup::"
    else
        failed="$failed $i"
    fi
done
for i in $failed; do
    label=$(cat "$logs/$i.label")
    echo "======== $label (FAILED) ========"
    cat "$logs/$i.log"
    echo "::error::$label failed"
done
[ -z "$failed" ]
