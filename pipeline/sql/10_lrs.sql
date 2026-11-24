-- Linear referencing support objects. Re-runnable: `make lrs` executes this file first and
-- fills in {CRS_METRIC} from config.py.
SET search_path = szelveny, public;

-- Every national road number an OSM way carries, normalised ('M 1' -> 'M1'), from its own ref
-- tag or from a route relation it belongs to.
CREATE TABLE IF NOT EXISTS osm_way_ref (
    osm_id    bigint NOT NULL,
    ref       text NOT NULL,
    category  text NOT NULL,
    via_tag   boolean NOT NULL,   -- the way's own ref tag lists it
    via_route boolean NOT NULL,   -- a route relation lists it
    PRIMARY KEY (osm_id, ref)
);

-- Counties each road-numbered way touches (OSM vármegye boundaries, ISO code).
CREATE TABLE IF NOT EXISTS osm_way_county (
    osm_id bigint NOT NULL,
    county text NOT NULL,
    PRIMARY KEY (osm_id, county)
);

-- One row per road built from OSM: how its geometry and km calibration were built.
CREATE TABLE IF NOT EXISTS lrs_road (
    road_id       integer PRIMARY KEY REFERENCES road ON DELETE CASCADE,
    status        text NOT NULL,       -- ok | partial | no_geometry | osm_only
    official_km   numeric(9,3),
    path_km       numeric(9,3),        -- length of the built centreline (EOV)
    n_junction    integer NOT NULL,    -- junction control points used
    n_milestone   integer NOT NULL,    -- milestone control points used
    orientation   text NOT NULL,       -- how the km direction was decided
    km_quality    text NOT NULL        -- worst segment quality on the road
);

-- km of the control points used (county borders, milestones, junctions), for "distance to the
-- nearest reference point" next to the km quality.
ALTER TABLE lrs_road ADD COLUMN IF NOT EXISTS controls_km numeric[];
ALTER TABLE lrs_road DROP CONSTRAINT IF EXISTS lrs_road_status_check;
ALTER TABLE lrs_road ADD CONSTRAINT lrs_road_status_check
    CHECK (status IN ('ok', 'partial', 'no_geometry', 'osm_only'));

-- 'M1', 12.3 -> point on the road. National road numbers are unique nationwide; p_county is
-- kept for local roads. Interpolates within the segment in EOV.
CREATE OR REPLACE FUNCTION locate(p_ref text, p_km numeric, p_county text DEFAULT NULL)
RETURNS TABLE (road_id integer, county text, km_quality text, geom geometry(Point, 4326))
LANGUAGE sql STABLE AS $$
    SELECT s.road_id, r.county, s.km_quality,
           ST_Transform(ST_LineInterpolatePoint(
               ST_Transform(s.geom, {CRS_METRIC}),
               CASE WHEN s.km_to = s.km_from THEN 0
                    ELSE ((p_km - s.km_from) / (s.km_to - s.km_from))::float8 END), 4326)
    FROM szelveny.road_segment s
    JOIN szelveny.road r USING (road_id)
    WHERE r.ref = upper(replace(p_ref, ' ', ''))
      AND (r.county IS NULL OR r.county = p_county)
      AND p_km BETWEEN s.km_from AND s.km_to
    ORDER BY r.county IS NULL DESC, s.sequence
    LIMIT 1
$$;

-- The reverse of locate(): the km of the point on road p_ref nearest to p_point, and how far
-- the point is from the road.
CREATE OR REPLACE FUNCTION measure(p_ref text, p_point geometry, p_county text DEFAULT NULL)
RETURNS TABLE (road_id integer, km numeric, offset_m double precision, km_quality text)
LANGUAGE sql STABLE AS $$
    SELECT s.road_id,
           round((s.km_from + (s.km_to - s.km_from) * ST_LineLocatePoint(
               ST_Transform(s.geom, {CRS_METRIC}), ST_Transform(p_point, {CRS_METRIC})))::numeric, 3),
           ST_Distance(ST_Transform(s.geom, {CRS_METRIC}), ST_Transform(p_point, {CRS_METRIC})),
           s.km_quality
    FROM szelveny.road_segment s
    JOIN szelveny.road r USING (road_id)
    WHERE r.ref = upper(replace(p_ref, ' ', ''))
      AND (r.county IS NULL OR r.county = p_county)
    ORDER BY ST_Transform(s.geom, {CRS_METRIC}) <-> ST_Transform(p_point, {CRS_METRIC})
    LIMIT 1
$$;
