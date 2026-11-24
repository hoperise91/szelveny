// ISO 3166-2:RO county codes -> names (as used in HG 540 and the data files).
export const COUNTIES: Record<string, string> = {
  AB: "Alba", AR: "Arad", AG: "Argeș", BC: "Bacău", BH: "Bihor", BN: "Bistrița-Năsăud",
  BT: "Botoșani", BV: "Brașov", BR: "Brăila", B: "București", BZ: "Buzău", CS: "Caraș-Severin",
  CL: "Călărași", CJ: "Cluj", CT: "Constanța", CV: "Covasna", DB: "Dâmbovița", DJ: "Dolj",
  GL: "Galați", GR: "Giurgiu", GJ: "Gorj", HR: "Harghita", HD: "Hunedoara", IL: "Ialomița",
  IS: "Iași", IF: "Ilfov", MM: "Maramureș", MH: "Mehedinți", MS: "Mureș", NT: "Neamț",
  OT: "Olt", PH: "Prahova", SM: "Satu Mare", SJ: "Sălaj", SB: "Sibiu", SV: "Suceava",
  TR: "Teleorman", TM: "Timiș", TL: "Tulcea", VS: "Vaslui", VL: "Vâlcea", VN: "Vrancea",
};

export const countyName = (code: string | null | undefined) =>
  code ? COUNTIES[code] ?? code : "";

export const countiesSorted = () =>
  Object.entries(COUNTIES).sort((a, b) => a[1].localeCompare(b[1], "ro"));
