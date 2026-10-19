#!/usr/bin/env python3
"""Build the offline cell-tower SQLite DB for pcat_util/cell_locate.py.

Input: a CSV in the Mozilla Location Service cell-export schema
(radio,mcc,net,area,cell,unit,lon,lat,range,samples,...), e.g. the final
public-domain MLS export from archive.org
(MLS_Full_Cell_Export_Final/MLS-full-cell-export-final.csv.gz), optionally
pre-filtered to one country.

    python3 misc/build_celldb.py mls_th.csv cells.sqlite [--mcc 520]

Ship the output to the router as /mnt/<disk>/pcat-celldb/cells.sqlite (or
/etc/pcat-manager-web/celldb.sqlite) and the GPS page's tower locator uses
it offline-first. A one-country slice is typically 10-60 MB.
"""

import argparse
import csv
import gzip
import sqlite3
import sys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv_in", help="MLS-schema CSV (.csv or .csv.gz)")
    ap.add_argument("db_out")
    ap.add_argument("--mcc", type=int, default=None,
                    help="keep only this country code")
    args = ap.parse_args()

    opener = gzip.open if args.csv_in.endswith(".gz") else open
    con = sqlite3.connect(args.db_out)
    con.execute("DROP TABLE IF EXISTS cells")
    con.execute("CREATE TABLE cells (radio TEXT, mcc INT, net INT, area INT,"
                " cell INT, unit INT, lat REAL, lon REAL, range INT,"
                " samples INT)")
    kept = 0
    with opener(args.csv_in, "rt", newline="") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if not header or header[0] != "radio":
            print("input does not look like an MLS cell export", file=sys.stderr)
            return 1
        batch = []
        for row in reader:
            try:
                mcc = int(row[1])
                if args.mcc is not None and mcc != args.mcc:
                    continue
                batch.append((row[0], mcc, int(row[2]), int(row[3]),
                              int(row[4]), int(row[5]) if row[5] else None,
                              float(row[7]), float(row[6]),
                              int(float(row[8] or 0)),
                              int(float(row[9] or 0))))
            except (ValueError, IndexError):
                continue
            if len(batch) >= 50000:
                con.executemany("INSERT INTO cells VALUES (?,?,?,?,?,?,?,?,?,?)",
                                batch)
                kept += len(batch)
                batch = []
        if batch:
            con.executemany("INSERT INTO cells VALUES (?,?,?,?,?,?,?,?,?,?)",
                            batch)
            kept += len(batch)
    con.execute("CREATE INDEX idx_cells ON cells (mcc, net, cell)")
    con.execute("CREATE INDEX idx_cells_pos ON cells (lat, lon)")
    con.commit()
    con.execute("VACUUM")
    con.close()
    print(f"{kept} cells -> {args.db_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
