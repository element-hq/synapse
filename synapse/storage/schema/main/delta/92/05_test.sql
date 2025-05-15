

-- This is a test

CREATE TABLE test_table(
    foo TEXT NOT NULL
);

CREATE index asdasd on test_table(foo);

CREATE INDEX test ON some_other_table(asdasd);
