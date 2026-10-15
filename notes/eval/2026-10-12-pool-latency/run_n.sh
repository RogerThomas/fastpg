#!/bin/sh
# usage: run_n.sh N script.py [args...]  (run from the worktree root)
n=$1; shift
unset VIRTUAL_ENV
i=1
while [ "$i" -le "$n" ]; do
  echo "--- run $i"
  uv run --group bench yeet "$@" 2>&1 | grep -v "^warning"
  i=$((i + 1))
done
