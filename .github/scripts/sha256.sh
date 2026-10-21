#!/bin/sh
# Records or checks the SHA-256 of built distributions (.github/workflows/release.yml), with
# sha256sum, or shasum where there's none (macOS). Run from the repo root:
#   sh .github/scripts/sha256.sh record dist/*.whl     # writes dist/<file>.sha256 for each file
#   sh .github/scripts/sha256.sh check dist/*.sha256   # fails unless each still matches
set -eu

if command -v sha256sum >/dev/null 2>&1; then
    sum="sha256sum"
else
    sum="shasum -a 256"
fi

mode=$1
shift
for file; do
    case $mode in
        record) (cd "$(dirname "$file")" && $sum "$(basename "$file")") > "$file.sha256" ;;
        check) (cd "$(dirname "$file")" && $sum -c "$(basename "$file")") ;;
        *) echo "usage: sh $0 record|check FILE..." >&2; exit 2 ;;
    esac
done
