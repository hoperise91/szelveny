-- Brings a database created from an older 00_schema.sql up to date for the NAP reference
-- network. Re-runnable: `make reference` executes this file first. Fresh databases get the
-- same columns and checks from 00_schema.sql.
SET search_path = szelveny, public;

ALTER TABLE road ADD COLUMN IF NOT EXISTS road_class text;
ALTER TABLE road ADD COLUMN IF NOT EXISTS administrator text;
ALTER TABLE road DROP CONSTRAINT IF EXISTS road_category_check;
ALTER TABLE road ADD CONSTRAINT road_category_check
    CHECK (category IN ('M', 'F', 'OK', 'BK', 'ramp', 'cycle', 'rest', 'local'));

ALTER TABLE road_segment ADD COLUMN IF NOT EXISTS carriageway smallint NOT NULL DEFAULT 0;
ALTER TABLE road_segment ADD COLUMN IF NOT EXISTS km_from_text text;
ALTER TABLE road_segment ADD COLUMN IF NOT EXISTS km_to_text text;
ALTER TABLE road_segment ADD COLUMN IF NOT EXISTS shared_with text;
ALTER TABLE road_segment ADD COLUMN IF NOT EXISTS source_ref text;
ALTER TABLE road_segment DROP CONSTRAINT IF EXISTS road_segment_carriageway_check;
ALTER TABLE road_segment ADD CONSTRAINT road_segment_carriageway_check CHECK (carriageway IN (0, 1, 2));

ALTER TABLE attribute_event ADD COLUMN IF NOT EXISTS carriageway smallint;
ALTER TABLE attribute_event DROP CONSTRAINT IF EXISTS attribute_event_carriageway_check;
ALTER TABLE attribute_event ADD CONSTRAINT attribute_event_carriageway_check CHECK (carriageway IN (0, 1, 2));
ALTER TABLE attribute_event DROP CONSTRAINT IF EXISTS attribute_event_type_check;
ALTER TABLE attribute_event ADD CONSTRAINT attribute_event_type_check CHECK (type IN
    ('surface', 'lanes', 'maxspeed', 'aadt', 'width', 'bridge', 'tunnel', 'works', 'ten_t', 'eroad',
     'curve', 'built_up', 'oneway', 'truck_restriction', 'administrator'));
