// The data list (NVDB-style): which road properties the panel shows, grouped, and which of them
// can colour the map. New data types become rows here, never new tabs.
import type { Strings } from "./i18n";

export type Mode = "category" | "surface" | "maxspeed" | "aadt" | "width" | "owner";
export type ObjectType = "junction" | "level_crossing" | "border" | "toll" | "ferry"
  | "km_post" | "bridge" | "rest_area";
export type DataId = "category" | "owner" | "eroad" | "surface" | "lanes" | "width" | "structures" | "curve"
  | "maxspeed" | "aadt" | `obj_${ObjectType}`;

/** colour: can colour the map; object: a point object type, shown as map dots and listed nearby. */
export interface DataType { id: DataId; label: keyof Strings; colour?: Mode; object?: ObjectType }

export const DATA_GROUPS: { label: keyof Strings; items: DataType[] }[] = [
  { label: "grp_network", items: [
    { id: "category", label: "layer_category", colour: "category" },
    { id: "owner", label: "layer_owner", colour: "owner" },
    { id: "eroad", label: "eroad" },
  ] },
  { label: "grp_road", items: [
    { id: "surface", label: "layer_surface", colour: "surface" },
    { id: "lanes", label: "lanes" },
    { id: "width", label: "layer_width", colour: "width" },
    { id: "structures", label: "structures" },
    { id: "curve", label: "curves" },
  ] },
  { label: "grp_traffic", items: [
    { id: "maxspeed", label: "layer_maxspeed", colour: "maxspeed" },
    { id: "aadt", label: "layer_aadt", colour: "aadt" },
  ] },
  { label: "grp_objects", items: [
    { id: "obj_junction", label: "obj_junction", object: "junction" },
    { id: "obj_level_crossing", label: "obj_level_crossing", object: "level_crossing" },
    { id: "obj_border", label: "obj_border", object: "border" },
    { id: "obj_toll", label: "obj_toll", object: "toll" },
    { id: "obj_ferry", label: "obj_ferry", object: "ferry" },
    { id: "obj_rest_area", label: "obj_rest_area", object: "rest_area" },
    { id: "obj_bridge", label: "obj_bridge", object: "bridge" },
    { id: "obj_km_post", label: "obj_km_post", object: "km_post" },
  ] },
];

export const OBJECT_COLOR: Record<ObjectType, string> = {
  junction: "#2c9c5a", level_crossing: "#c0392b", border: "#6a3d9a", toll: "#d99a00", ferry: "#1f78b4",
  rest_area: "#7a5c2e", bridge: "#4a5568", km_post: "#1d1c1a",
};

export const DATA_TYPES: DataType[] = DATA_GROUPS.flatMap((g) => g.items);
export const COLOUR_MODES: DataType[] = DATA_TYPES.filter((d) => d.colour);
export const DEFAULT_DATA: DataId[] = ["category", "owner", "eroad", "surface", "maxspeed",
  "obj_junction", "obj_border", "obj_toll", "obj_ferry", "obj_rest_area"];
