-- This is a test to ensure that we don't run
-- CREATE INDEX or DROP INDEX in the schema file, and instead use
-- background updates.

CREATE UNIQUE index foobar ON foobar(ads);
