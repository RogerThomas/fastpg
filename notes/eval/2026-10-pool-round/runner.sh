#!/bin/bash
cd <repository root>
S=<this directory>
O=<this directory>/raw
waitload() { while :; do l=$(uptime | sed 's/.*averages*: *//' | awk '{print $1}' | tr -d ,); echo "$(date +%T) load $l" >> $O/load.log; awk "BEGIN{exit !($l<=8)}" && break; sleep 60; done; echo "$l"; }
run() { tag=$1; shift; l=$(waitload); for r in 1 2 3; do
  echo "LOAD $l" > $O/$tag.r$r.txt
  docker compose -f docker-compose.yml -f $S/cpu1.yml --profile bench run --rm --no-deps bench ./bench/run.py -s concurrent -o /tmp/out "$@" >> $O/$tag.r$r.txt 2>&1
done; }
for d in fastpg-struct asyncpg-struct psycopg-async-struct psqlpy-struct fastpg-pipelined; do run A-$d -d $d -c 16; done
for c in 16 100; do for p in 1 2 4 16; do run B-c$c-p$p -d fastpg-struct -c $c --pool-size $p; done; done
for d in fastpg-struct asyncpg-struct psycopg-async-struct psqlpy-struct; do run C-$d -d $d -c 100 --pool-size 16; done
echo DONE > $O/DONE
