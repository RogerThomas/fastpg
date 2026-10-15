-- Benchmark/test schema. Kept deliberately small but covering the common scalar types.
CREATE TABLE people (
    id          integer          PRIMARY KEY,
    name        text             NOT NULL,
    age         integer          NOT NULL,
    email       text,                         -- NULL for every 7th row
    score       double precision NOT NULL,
    balance     numeric(12, 2)   NOT NULL,
    active      boolean          NOT NULL,
    external_id uuid             NOT NULL,
    created_at  timestamptz      NOT NULL,
    birthday    date                          -- NULL for every 11th row
);
