-- Extensions the design depends on. Both are created here rather than in a migration so
-- that a developer's first `make stack-up` produces a database the application can use
-- without a privileged step.
--
--   postgis  - space-and-time clustering of geotagged media into trip groupings
--   pg_trgm  - fuzzy filename matching, to associate sidecar files with their media
--
-- Neither is used yet: clustering runs in Python and sidecar association is not built
-- (openspec/config.yaml marks both DEFERRED).

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
