-- Szelvény core schema (NVDB-like). See docs/ROADMAP.md.
-- Geometry is stored in EPSG:4326. Lengths and chainage are computed in
-- EPSG:23700 (HD72 / EOV, config.CRS_METRIC): ST_Length(ST_Transform(geom, 23700)).
-- (This file runs from docker-entrypoint-initdb.d, so the SRID is written out here.)

CREATE EXTENSION IF NOT EXISTS postgis;

CREATE SCHEMA IF NOT EXISTS raw;    -- untouched source imports (osm2pgsql, ogr2ogr, …)
CREATE SCHEMA IF NOT EXISTS szelveny;  -- the reference network and its attributes

SET search_path = szelveny, public;

-- Every value in Szelvény traces back to a row here.
CREATE TABLE source (
    source_id     text PRIMARY KEY,               -- 'osm', 'nap', 'gisco', …
    name          text NOT NULL,
    url           text,
    licence       text NOT NULL,                  -- 'ODbL-1.0', 'NAP ÁSZF 2024-03-01 §32', …
    attribution   text,
    retrieved_at  timestamptz NOT NULL,
    notes         text
);

-- Roads. National roads come from NAP's national network (Országos közúthálózat, Phase 3),
-- or from OSM when NAP lacks them; local roads have no national number and are identified by
-- name + settlement.
CREATE TABLE road (
    road_id           serial PRIMARY KEY,
    ref               text NOT NULL,              -- 'M1', '8', '4512', '81101'; local roads: the name
    category          text NOT NULL CHECK (category IN ('M', 'F', 'OK', 'BK', 'ramp', 'cycle', 'rest', 'local')),
                                                  -- M gyorsforgalmi, F főút, OK összekötő út, BK bekötő
                                                  -- út, ramp csomóponti ág, cycle kerékpárút, rest
                                                  -- pihenőút (NAP kutka; number-based for OSM roads)
    road_class        text,                       -- NAP's category name: 'autópálya', 'II. rendű főút', …
    administrator     text,                       -- kezelő (NAP): 'Magyar Közút Nonprofit Zrt.', …
    county            text,                       -- ISO 3166-2:HU code ('GS'); NULL for national roads
    settlement        text,                       -- local roads only (with ref = name)
    route_desc        text,
    km_from_official  numeric(9,3),
    km_to_official    numeric(9,3),
    length_official   numeric(9,3),               -- km
    source_id         text NOT NULL REFERENCES source,
    UNIQUE NULLS NOT DISTINCT (ref, settlement)
);

CREATE TABLE road_segment (
    seg_id      serial PRIMARY KEY,
    road_id     integer NOT NULL REFERENCES road ON DELETE CASCADE,
    sequence    integer NOT NULL,
    km_from     numeric(9,3) NOT NULL,
    km_to       numeric(9,3) NOT NULL,
    geom        geometry(LineString, 4326) NOT NULL,
    length_m    double precision GENERATED ALWAYS AS (ST_Length(ST_Transform(geom, 23700))) STORED,
    km_quality  text NOT NULL CHECK (km_quality IN ('official', 'calibrated', 'interpolated', 'estimated', 'none')),
                                    -- official: NAP szelvény; none: local road without chainage
    carriageway smallint NOT NULL DEFAULT 0 CHECK (carriageway IN (0, 1, 2)),
                                    -- NAP pkod: 0 undivided, 1 right (chainage direction), 2 left
    km_from_text text,              -- NAP's own szelvény ('20 + 1007'): the metre part can pass 999
    km_to_text   text,              -- in km sections longer than 1 km, so keep it for display
    shared_with text,               -- közös szakasz: the lower-ranked road on this section
    source_ref  text,               -- the source's record id (NAP: id)
    source_id   text NOT NULL REFERENCES source,
    UNIQUE (road_id, sequence)
);
CREATE INDEX road_segment_geom_idx ON road_segment USING gist (geom);

-- Linear attributes along a road: source, date and licence are mandatory.
CREATE TABLE attribute_event (
    event_id     serial PRIMARY KEY,
    road_id      integer NOT NULL REFERENCES road ON DELETE CASCADE,
    km_from      numeric(9,3) NOT NULL,
    km_to        numeric(9,3) NOT NULL,
    type         text NOT NULL CHECK (type IN
                   ('surface', 'lanes', 'maxspeed', 'aadt', 'width', 'bridge', 'tunnel', 'works', 'ten_t', 'eroad',
                    'curve', 'built_up', 'oneway', 'truck_restriction', 'administrator')),
    value        text NOT NULL,
    carriageway  smallint CHECK (carriageway IN (0, 1, 2)),   -- NAP pkod; NULL: whole road
    source_id    text NOT NULL REFERENCES source,
    source_date  date NOT NULL,
    licence      text NOT NULL,
    CHECK (km_to >= km_from)
);
CREATE INDEX attribute_event_road_idx ON attribute_event (road_id, type, km_from);

-- Point objects at a szelvény position (km posts, bridges, rest areas, bus stops, …).
CREATE TABLE point_object (
    obj_id       serial PRIMARY KEY,
    road_id      integer REFERENCES road ON DELETE CASCADE,
    km           numeric(9,3),
    type         text NOT NULL,
    props        jsonb NOT NULL DEFAULT '{}',
    geom         geometry(Point, 4326),
    source_id    text NOT NULL REFERENCES source,
    source_date  date NOT NULL,
    licence      text NOT NULL
);
CREATE INDEX point_object_geom_idx ON point_object USING gist (geom);
