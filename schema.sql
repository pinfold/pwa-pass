-- schema.sql
--
-- Database schema for pwa-passv3 (OpenCommons PWA Capture).
--
-- This creates the PostGIS extension and the incident reports table that
-- app.py reads from and writes to via /upload, /check_nearby, and
-- /still_there.
--
-- Usage:
--   docker exec -i pgvector-db psql -U passuser -d passdb < schema.sql
--
-- Or, to have a fresh Postgres container auto-run this on first boot,
-- mount it into the official postgres/postgis image's init directory:
--   volumes:
--     - ./schema.sql:/docker-entrypoint-initdb.d/schema.sql
--
-- NOTE: If you set a custom DB_TABLE environment variable for the app,
-- replace "incident_reports_test" below (in both the CREATE TABLE and the
-- CREATE INDEX statements) with that same table name. The default here
-- matches the fallback value used in app.py when DB_TABLE is unset:
--   DB_TABLE = os.getenv('DB_TABLE', 'incident_reports_test')

-- 1. Enable PostGIS (required for the GEOGRAPHY column + ST_MakePoint /
--    ST_DWithin queries used by app.py).
CREATE EXTENSION IF NOT EXISTS postgis;

-- 2. Incident reports table.
CREATE TABLE IF NOT EXISTS incident_reports_test (
    id             SERIAL PRIMARY KEY,

    -- Report location as a geographic point (SRID 4326 / WGS84), written by
    -- app.py via ST_SetSRID(ST_MakePoint(longitude, latitude), 4326).
    location       GEOGRAPHY(Point, 4326) NOT NULL,

    -- Object key of the processed JPEG stored in the S3/MinIO bucket
    -- (S3_BUCKET_NAME), e.g. "capture_1699999999.jpg".
    image_path     TEXT,

    -- When the incident was first reported, and when it was most recently
    -- confirmed present (bumped by POST /still_there/<id>).
    first_reported TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_report    TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Free-form report details captured at submission time: reporter email,
    -- incident type(s), description, camera heading, obstruction/hazard
    -- flags. Stored as JSONB so new fields can be added without a migration.
    metadata       JSONB,

    -- Simple lifecycle flag for the report (currently always 'active' on
    -- insert; reserved for future use, e.g. 'resolved'/'archived').
    status         TEXT NOT NULL DEFAULT 'active'
);

-- 3. Spatial index: required for GET /check_nearby's ST_DWithin() radius
--    search to perform well at scale instead of a full table scan.
CREATE INDEX IF NOT EXISTS incident_reports_test_location_gix
    ON incident_reports_test USING GIST (location);

-- 4. Supporting index: GET /check_nearby also filters on
--    "last_report >= NOW() - INTERVAL '3 days'" and orders by last_report,
--    so a btree index here keeps that filter/sort efficient as the table
--    grows.
CREATE INDEX IF NOT EXISTS incident_reports_test_last_report_idx
    ON incident_reports_test (last_report DESC);
