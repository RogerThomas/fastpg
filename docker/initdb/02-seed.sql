-- Deterministic seed data (no random()): identical on every run.
INSERT INTO people (id, name, age, email, score, balance, active, external_id, created_at, birthday)
SELECT
    i,
    'person-' || i,
    18 + (i % 60),
    CASE WHEN i % 7 = 0 THEN NULL ELSE 'person' || i || '@example.com' END,
    (i % 1000) / 10.0,
    ((i * 37) % 100000) / 100.0,
    i % 2 = 0,
    md5(i::text)::uuid,
    timestamptz '2024-01-01 00:00:00+00' + make_interval(secs => i * 61),
    CASE WHEN i % 11 = 0 THEN NULL ELSE date '1950-01-01' + (i % 20000) END
FROM generate_series(1, 100000) AS s(i);

ANALYZE people;

-- Load the table and its index into shared_buffers up front (the whole DB is ~20 MB), so
-- benchmarks never measure cold reads. Creating the extension needs a superuser: seeding
-- another server as a plain user (`task test-against SEED=1`) skips this step.
DO $$
BEGIN
    CREATE EXTENSION IF NOT EXISTS pg_prewarm;
    PERFORM pg_prewarm('people'), pg_prewarm('people_pkey');
EXCEPTION WHEN insufficient_privilege OR undefined_file OR feature_not_supported THEN
    RAISE NOTICE 'skipping pg_prewarm: %', SQLERRM;
END
$$;
