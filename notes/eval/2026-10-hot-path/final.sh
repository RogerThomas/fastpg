#!/bin/zsh
# The runs in final.log: build `base` (1e864ae, before this work) vs `coalesce` (7114f07,
# every commit of this branch), interleaved. Run from the repository root with the two
# variants in /tmp/fastpg-variants (see README.md). (The original run used scratch copies
# of these scripts; the arguments are the same.)
Y=.venv/bin/yeet
H=notes/eval/2026-10-hot-path
date; uptime
echo "== hot.py sequential struct"
$Y $H/ab.py base coalesce --pairs 10 -n 4000 --rounds 3 --mode struct
echo "== hot.py sequential tuple"
$Y $H/ab.py base coalesce --pairs 6 -n 4000 --rounds 3 --mode tuple
echo "== hot.py pipe16"
$Y $H/ab.py base coalesce --pairs 6 -n 20000 --rounds 3 --mode pipe16
echo "== cpu_per_request sequential fastpg-struct"
$Y $H/cprab.py base coalesce --pairs 6 --requests 5000
echo "== cpu_per_request -t 16 fastpg-pipelined"
$Y $H/cprab.py base coalesce --pairs 4 --requests 20000 --tasks 16 -d fastpg-pipelined
echo "== cpu_per_request -t 16 fastpg-struct (pool of 1)"
$Y $H/cprab.py base coalesce --pairs 4 --requests 20000 --tasks 16
date; uptime
