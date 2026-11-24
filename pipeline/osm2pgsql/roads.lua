-- Bornă: osm2pgsql flex style. Roads, road points and road route relations
-- from OSM into the `raw` schema. Values are kept as tagged (text); normalisation
-- (ref splitting, maxspeed parsing, …) happens in Phase 2, not here.

local SCHEMA = 'raw'
local SRID = 4326

-- highway=* values that make up the public road network (incl. DC roads that are
-- often mapped as track/unclassified). service ways are kept only when they carry a ref.
local ROAD_TYPES = {
    motorway = true, motorway_link = true,
    trunk = true, trunk_link = true,
    primary = true, primary_link = true,
    secondary = true, secondary_link = true,
    tertiary = true, tertiary_link = true,
    unclassified = true, residential = true, living_street = true,
    track = true, construction = true,
}

local roads = osm2pgsql.define_table({
    name = 'osm_roads',
    schema = SCHEMA,
    ids = { type = 'way', id_column = 'osm_id' },
    columns = {
        { column = 'highway', type = 'text', not_null = true },
        { column = 'ref', type = 'text' },
        { column = 'int_ref', type = 'text' },
        { column = 'name', type = 'text' },
        { column = 'surface', type = 'text' },
        { column = 'lanes', type = 'text' },
        { column = 'maxspeed', type = 'text' },
        { column = 'oneway', type = 'text' },
        { column = 'bridge', type = 'text' },
        { column = 'tunnel', type = 'text' },
        { column = 'layer', type = 'text' },
        { column = 'tags', type = 'jsonb' },
        { column = 'geom', type = 'linestring', projection = SRID, not_null = true },
    },
})

local points = osm2pgsql.define_table({
    name = 'osm_road_points',
    schema = SCHEMA,
    ids = { type = 'node', id_column = 'osm_id' },
    columns = {
        { column = 'kind', type = 'text', not_null = true },
        { column = 'ref', type = 'text' },
        { column = 'distance', type = 'text' },  -- milestone km as tagged (distance / pk)
        { column = 'tags', type = 'jsonb' },
        { column = 'geom', type = 'point', projection = SRID, not_null = true },
    },
})

local routes = osm2pgsql.define_table({
    name = 'osm_road_routes',
    schema = SCHEMA,
    ids = { type = 'relation', id_column = 'osm_id' },
    columns = {
        { column = 'ref', type = 'text' },
        { column = 'network', type = 'text' },
        { column = 'name', type = 'text' },
        { column = 'way_ids', type = 'jsonb' },  -- member ways, in relation order
        { column = 'tags', type = 'jsonb' },
        { column = 'geom', type = 'multilinestring', projection = SRID },
    },
})

-- County (județ) boundaries, admin_level 4, incl. Municipiul București. Precise enough to place
-- the HG 540 county-border km positions (GISCO 1:1M is not).
local counties = osm2pgsql.define_table({
    name = 'osm_counties',
    schema = SCHEMA,
    ids = { type = 'relation', id_column = 'osm_id' },
    columns = {
        { column = 'name', type = 'text' },
        { column = 'tags', type = 'jsonb' },
        { column = 'geom', type = 'multipolygon', projection = SRID, not_null = true },
    },
})

-- Ferry crossings (route=ferry): the Danube crossings link roads where there is no bridge.
local ferries = osm2pgsql.define_table({
    name = 'osm_ferries',
    schema = SCHEMA,
    ids = { type = 'way', id_column = 'osm_id' },
    columns = {
        { column = 'name', type = 'text' },
        { column = 'tags', type = 'jsonb' },
        { column = 'geom', type = 'linestring', projection = SRID, not_null = true },
    },
})

-- Named places, to orient roads whose official route runs from one locality to another.
local PLACE_TYPES = {
    city = true, town = true, village = true, hamlet = true, suburb = true,
    quarter = true, neighbourhood = true, isolated_dwelling = true, locality = true,
}
local places = osm2pgsql.define_table({
    name = 'osm_places',
    schema = SCHEMA,
    ids = { type = 'node', id_column = 'osm_id' },
    columns = {
        { column = 'name', type = 'text', not_null = true },
        { column = 'place', type = 'text', not_null = true },
        { column = 'geom', type = 'point', projection = SRID, not_null = true },
    },
})

local function point_kind(t)
    if t.highway == 'milestone' then return 'milestone' end
    if t.highway == 'motorway_junction' then return 'motorway_junction' end
    if t.railway == 'level_crossing' then return 'level_crossing' end
    if t.barrier == 'toll_booth' then return 'toll_booth' end
    if t.barrier == 'border_control' then return 'border_control' end
    return nil
end

function osm2pgsql.process_node(object)
    local t = object.tags
    if t.place and PLACE_TYPES[t.place] and t.name then
        places:insert({ name = t.name, place = t.place, geom = object:as_point() })
    end
    local kind = point_kind(t)
    if not kind then return end
    points:insert({
        kind = kind,
        ref = t.ref,
        distance = t.distance or t.pk,
        tags = t,
        geom = object:as_point(),
    })
end

function osm2pgsql.process_way(object)
    local t = object.tags
    if t.route == 'ferry' then
        ferries:insert({ name = t.name, tags = t, geom = object:as_linestring() })
        return
    end
    local hw = t.highway
    if not (ROAD_TYPES[hw] or (hw == 'service' and t.ref)) then return end
    roads:insert({
        highway = hw,
        ref = t.ref,
        int_ref = t.int_ref,
        name = t.name,
        surface = t.surface,
        lanes = t.lanes,
        maxspeed = t.maxspeed,
        oneway = t.oneway,
        bridge = t.bridge,
        tunnel = t.tunnel,
        layer = t.layer,
        tags = t,
        geom = object:as_linestring(),
    })
end

function osm2pgsql.process_relation(object)
    local t = object.tags
    if t.type == 'boundary' and t.boundary == 'administrative' and t.admin_level == '4' then
        counties:insert({ name = t.name, tags = t, geom = object:as_multipolygon() })
        return
    end
    if t.type ~= 'route' or t.route ~= 'road' then return end
    local way_ids = {}
    for _, m in ipairs(object.members) do
        if m.type == 'w' then way_ids[#way_ids + 1] = m.ref end
    end
    routes:insert({
        ref = t.ref,
        network = t.network,
        name = t.name,
        way_ids = way_ids,
        tags = t,
        geom = object:as_multilinestring(),
    })
end
