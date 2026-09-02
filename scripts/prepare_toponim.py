from __future__ import annotations
import argparse
import re
import sqlite3
import unicodedata
import zipfile
from pathlib import Path
import tempfile
import geopandas as gpd

PRIORITY = {
    "Permukiman Lainnya": 0,
    "Ibukota Desa": 0,
    "Ibukota Kecamatan": 0,
    "Desa": 1,
    "Kecamatan": 2,
    "Kota": 3,
    "Ibukota Kabupaten": 4,
}

def norm(value: str) -> str:
    value = unicodedata.normalize("NFKD", str(value or "").strip().lower())
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", value).strip()

def build(src: Path, out: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        if src.suffix.lower() == ".zip":
            with zipfile.ZipFile(src) as zf:
                zf.extractall(tmpdir)
            shp = next(tmpdir.rglob("*.shp"))
        else:
            shp = src
        gdf = gpd.read_file(shp).to_crs(4326)
        required = {"NAMOBJ", "REMARK"}
        missing = required - set(gdf.columns)
        if missing:
            raise SystemExit(f"Field toponim tidak lengkap: {sorted(missing)}")
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.exists():
            out.unlink()
        conn = sqlite3.connect(out)
        cur = conn.cursor()
        cur.executescript("""
        PRAGMA journal_mode=OFF;
        PRAGMA synchronous=OFF;
        CREATE TABLE toponim(
          id INTEGER PRIMARY KEY,
          name TEXT NOT NULL,
          name_norm TEXT NOT NULL,
          category TEXT,
          lon REAL NOT NULL,
          lat REAL NOT NULL,
          settlement_priority INTEGER
        );
        CREATE VIRTUAL TABLE toponim_rtree USING rtree(id,min_lon,max_lon,min_lat,max_lat);
        CREATE INDEX idx_toponim_name_norm ON toponim(name_norm);
        CREATE INDEX idx_toponim_category ON toponim(category);
        CREATE INDEX idx_toponim_settlement_priority ON toponim(settlement_priority);
        """)
        rows=[]; rtree=[]
        for _, row in gdf.iterrows():
            geom=row.geometry
            if geom is None or geom.is_empty:
                continue
            name=str(row.get("NAMOBJ") or "").strip()
            if not name:
                continue
            cat=str(row.get("REMARK") or "").strip()
            rid=len(rows)+1
            lon=float(geom.x); lat=float(geom.y)
            rows.append((rid,name,norm(name),cat,lon,lat,PRIORITY.get(cat)))
            rtree.append((rid,lon,lon,lat,lat))
        cur.executemany("INSERT INTO toponim VALUES (?,?,?,?,?,?,?)", rows)
        cur.executemany("INSERT INTO toponim_rtree VALUES (?,?,?,?,?)", rtree)
        conn.commit()
        cur.execute("ANALYZE")
        conn.commit()
        conn.close()
        print(f"PASS: {len(rows)} toponim -> {out}")

if __name__ == "__main__":
    ap=argparse.ArgumentParser()
    ap.add_argument("source", type=Path, help="ZIP/SHP toponim dengan field NAMOBJ dan REMARK")
    ap.add_argument("--out", type=Path, default=Path("data/reference/toponim.sqlite"))
    args=ap.parse_args()
    build(args.source, args.out)
