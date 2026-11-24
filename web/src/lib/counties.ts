// ISO 3166-2:HU county codes (19 megye + Budapest) -> names, as in the data files.
export const COUNTIES: Record<string, string> = {
  BK: "Bács-Kiskun", BA: "Baranya", BE: "Békés", BZ: "Borsod-Abaúj-Zemplén", BU: "Budapest",
  CS: "Csongrád-Csanád", FE: "Fejér", GS: "Győr-Moson-Sopron", HB: "Hajdú-Bihar", HE: "Heves",
  JN: "Jász-Nagykun-Szolnok", KE: "Komárom-Esztergom", NO: "Nógrád", PE: "Pest", SO: "Somogy",
  SZ: "Szabolcs-Szatmár-Bereg", TO: "Tolna", VA: "Vas", VE: "Veszprém", ZA: "Zala",
};

export const countyName = (code: string | null | undefined) =>
  code ? COUNTIES[code] ?? code : "";

export const countiesSorted = () =>
  Object.entries(COUNTIES).sort((a, b) => a[1].localeCompare(b[1], "hu"));
