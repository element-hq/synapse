CREATE TABLE test1 (
    id INT
);

CREATE INDEX test1_id_idx ON test1 (id);

CREATE TABLE IF NOT EXISTS test2 (
    id INT
);

CREATE INDEX test2_id_idx ON test2 (id);
CREATE INDEX IF NOT EXISTS test2_id_idx ON test2 (id);

CREATE INDEX CONCURRENTLY IF NOT EXISTS test3_id_idx ON test3 (id);

CREATE TEMPORARY TABLE IF NOT EXSTS test4 (
    id INT
);

CREATE INDEX IF NOT EXISTS test4_id_idx ON test4 (id);
