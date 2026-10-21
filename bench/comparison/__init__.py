"""Cross-driver comparison suite: fastpg vs asyncpg, psycopg 3 (async) and psqlpy.

Entry point: ``bench/compare.py``. Methodology: ``docs/benchmarks.md``.

* ``data``: the suite's own schema, seed data and the expected rows (computed in Python from
  the same formulas), plus the checks that every driver returned exactly those rows.
* ``drivers``: one adapter per driver configuration, each using the driver's own pool.
* ``workloads``: what each workload runs, its concurrency and its correctness checks.
* ``runner``: warm-up, closed-loop and open-loop timing, trials with drivers interleaved.
* ``report``: environment capture, aggregation (median, spread, percentiles) and Markdown.
"""
