"""Convert raw HEC-HMS exports in data/source into web-ready data/hms.

Example source folder:
  data/source/Oyo/
    Oyo.basin
    Oyo.sqlite
    T_0002.dss, T_0010.dss, T_0025.dss

The SQLite file is the HEC-HMS basin spatial database. The preprocessor
automatically selects the spatial layers whose IDs match the .basin topology
(e.g. reach2d and subbasin2d in HEC-HMS 4.13 exports).
The reach2d layer is treated as the routing centerline layer: its ``name`` field
may identify both Reach and Subbasin elements from the .basin topology.

Runtime never opens data/source. It reads only data/hms.
"""
from __future__ import annotations
import argparse, gzip, json, math, os, re, shutil, sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any
import geopandas as gpd
import numpy as np
from shapely import force_2d, make_valid
from shapely.geometry import GeometryCollection, LineString, MultiPolygon, Point, Polygon
from shapely.ops import unary_union

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "data" / "source"
HMS_ROOT = ROOT / "data" / "hms"
REFERENCE_GPKG = ROOT / "data" / "reference" / "official_reference.gpkg"
WEB_CRS = "EPSG:4326"
START_RE = re.compile(r"^(Subbasin|Reach|Junction|Sink|Source|Reservoir|Diversion):\s*(.+?)\s*$", re.I)
KV_RE = re.compile(r"^([^:]+):\s*(.*?)\s*$")
DSS_RE = re.compile(rb"//([A-Za-z0-9_.:-]+)/(FLOW(?:-COMBINE)?)/[^/\x00]{0,48}/([^/\x00]{1,32})/([^/\x00]{1,96})/", re.I)

def _load_dotenv():
    path=ROOT/".env"
    if not path.exists(): return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line=raw.strip()
        if not line or line.startswith("#") or "=" not in line: continue
        k,v=line.split("=",1); os.environ.setdefault(k.strip(),v.strip().strip('"').strip("'"))

_load_dotenv()

def log(s): print(f"[HMS] {s}")

def parse_basin(path: Path) -> dict[str, Any]:
    nodes, current = {}, None; model_name = path.stem
    for raw in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = raw.strip()
        if not line: continue
        if line.lower().startswith("basin:"):
            model_name = line.split(":",1)[1].strip() or model_name; continue
        m = START_RE.match(line)
        if m:
            current = {"id": m.group(2).strip(), "type": m.group(1).lower(), "_attrs": {}}
            nodes[current["id"]] = current; continue
        if line == "End:": current = None; continue
        if current:
            kv = KV_RE.match(line)
            if kv: current["_attrs"][kv.group(1).strip()] = kv.group(2).strip()
    for n in nodes.values():
        a = n.pop("_attrs", {}); n["downstream"] = a.get("Downstream") or None
        for src,dst in [("Canvas X","canvas_x"),("Canvas Y","canvas_y"),("From Canvas X","from_canvas_x"),("From Canvas Y","from_canvas_y"),("Latitude Degrees","lat"),("Longitude Degrees","lon"),("Length","length_m"),("Area","area_km2")]:
            try: n[dst] = float(a[src]) if src in a else None
            except Exception: n[dst] = None
    upstream = defaultdict(list); edges=[]
    for n in nodes.values():
        if n.get("downstream"): upstream[n["downstream"]].append(n["id"]); edges.append({"from":n["id"],"to":n["downstream"]})
    for nid,n in nodes.items(): n["upstream"] = upstream.get(nid,[])
    def first_reach(nid):
        seen=set(); cur=str(nodes.get(nid,{}).get("downstream") or "")
        while cur and cur not in seen:
            seen.add(cur); n=nodes.get(cur)
            if not n: return None
            if n["type"]=="reach": return cur
            cur=str(n.get("downstream") or "")
        return None
    first={nid:first_reach(nid) for nid in nodes}; counts=defaultdict(int)
    for n in nodes.values(): counts[n["type"]]+=1
    return {"schema_version":1,"model_name":model_name,"nodes":nodes,"edges":edges,"first_downstream_reach":first,"counts":dict(counts)}

def find_one(folder, patterns):
    for pat in patterns:
        xs=sorted(folder.glob(pat))
        if xs: return xs[0]
    raise FileNotFoundError(f"Tidak ditemukan {' / '.join(patterns)} di {folder}")

def id_col(gdf, names):
    lookup={str(c).lower():c for c in gdf.columns}
    for n in names:
        if n.lower() in lookup: return lookup[n.lower()]
    raise ValueError(f"Kolom ID tidak ditemukan; kolom: {list(gdf.columns)}")

def _as_gdf(source):
    return source.copy() if isinstance(source, gpd.GeoDataFrame) else gpd.read_file(source)

def sqlite_spatial_layers(path: Path) -> list[str]:
    """List spatial tables without assuming a specific HEC-HMS schema version."""
    names=[]
    try:
        with sqlite3.connect(path) as con:
            names=[str(r[0]) for r in con.execute(
                "SELECT f_table_name FROM geometry_columns ORDER BY f_table_name"
            ).fetchall() if r and r[0]]
    except Exception:
        names=[]
    if not names:
        try:
            listed=gpd.list_layers(path)
            names=[str(x) for x in listed["name"].tolist()]
        except Exception:
            names=[]
    return list(dict.fromkeys(names))

def _geometry_family(gdf: gpd.GeoDataFrame, family: str) -> int:
    if gdf.empty or "geometry" not in gdf:
        return 0
    types=gdf.geometry.geom_type.fillna("")
    if family == "line":
        return int(types.isin(["LineString","MultiLineString"]).sum())
    if family == "polygon":
        return int(types.isin(["Polygon","MultiPolygon"]).sum())
    if family == "point":
        return int(types.isin(["Point","MultiPoint"]).sum())
    return int((types != "").sum())

def _read_matching_sqlite_layer(path: Path, expected_ids: set[str], preferred_names: list[str], id_names: list[str], family: str, role: str, required_name_tokens: tuple[str, ...] = ()):
    """Pick the spatial table that best matches topology IDs and geometry family.

    Real HMS SQLite exports may keep the legacy reach/subbasin tables empty while
    the usable geometries live in reach2d/subbasin2d. Selecting by topology-ID
    overlap prevents terrain/network helper rows from leaking into the web layer.
    """
    layers=sqlite_spatial_layers(path)
    if not layers:
        raise ValueError(f"Tidak ada layer spasial yang terbaca dari {path.name}")
    pref={name.lower():len(preferred_names)-i for i,name in enumerate(preferred_names)}
    ordered=sorted(layers,key=lambda x:(pref.get(x.lower(),0),x.lower()),reverse=True)
    best=None
    diagnostics=[]
    for layer in ordered:
        lname=layer.lower()
        if required_name_tokens and not all(token.lower() in lname for token in required_name_tokens):
            continue
        try:
            g=gpd.read_file(path,layer=layer)
        except Exception as e:
            diagnostics.append(f"{layer}: gagal baca ({e})")
            continue
        if g.empty or g.crs is None:
            diagnostics.append(f"{layer}: kosong/CRS tidak ada")
            continue
        try:
            col=id_col(g,id_names)
        except ValueError:
            continue
        ids=g[col].fillna("").astype(str).str.strip()
        overlap=len(set(ids) & expected_ids)
        geom_count=_geometry_family(g,family)
        if not overlap or not geom_count:
            continue
        score=(overlap,pref.get(layer.lower(),0),geom_count)
        if best is None or score > best[0]:
            best=(score,layer,g,col)
    if best is None:
        detail="; ".join(diagnostics[:6])
        raise ValueError(f"Layer {role} tidak ditemukan di {path.name}. {detail}".strip())
    _,layer,g,col=best
    g=g.copy(); g[col]=g[col].fillna("").astype(str).str.strip()
    g=g[g[col].isin(expected_ids)].copy()
    dup=sorted(set(g.loc[g[col].duplicated(False),col].astype(str)))
    if dup:
        raise ValueError(f"Layer {layer} memiliki ID {role} duplikat: {dup[:8]}")
    return g,layer

def load_sqlite_spatial(path: Path, topo: dict[str, Any]):
    """Load the two HMS spatial layers required by the web runtime.

    ``reach2d`` is intentionally matched against *both* Reach and Subbasin IDs
    from the .basin topology.  In recent HMS basin SQLite files this layer is a
    stream-centerline topology layer whose ``name`` field can represent either
    element type.  This removes the previous dependency on longest_flowpath.
    """
    reach_ids={nid for nid,n in topo["nodes"].items() if n["type"]=="reach"}
    sub_ids={nid for nid,n in topo["nodes"].items() if n["type"]=="subbasin"}
    route_ids=reach_ids | sub_ids
    routing_lines,route_layer=_read_matching_sqlite_layer(
        path,route_ids,["reach2d","reach"],["name","reach_id","reach","subbasin_id","subbasin"],"line","routing centerline")
    subbasins,sub_layer=_read_matching_sqlite_layer(
        path,sub_ids,["subbasin2d","subbasin"],["subbasin_id","name","subbasin"],"polygon","subbasin")
    return {
        "routing_lines":routing_lines,"subbasins":subbasins,
        "layers":{"routing_lines":route_layer,"subbasins":sub_layer},
    }

def line2d(g):
    if g is None or g.is_empty: return g
    try: return force_2d(g)
    except Exception: return LineString([(c[0],c[1]) for c in g.coords]) if g.geom_type=="LineString" else g

def _xy(row, xname, yname):
    try:
        x=float(row.get(xname)); y=float(row.get(yname))
        return (x,y) if math.isfinite(x) and math.isfinite(y) else None
    except Exception:
        return None

def orient_routing_line(g,row,node=None):
    """Orient a reach2d centerline upstream -> downstream.

    HMS reach2d provides explicit upstream_x/y and dnstream_x/y columns.  They
    are preferred because they also work for Subbasin-named stream segments.
    Older exports fall back to Reach ``From Canvas`` coordinates from .basin.
    """
    g=line2d(g)
    if g is None or g.is_empty or g.geom_type!="LineString": return g
    cs=list(g.coords); p0,p1=Point(cs[0]),Point(cs[-1])
    up=_xy(row,"upstream_x","upstream_y")
    dn=_xy(row,"dnstream_x","dnstream_y")
    if up:
        target=Point(up)
        return g if p0.distance(target)<=p1.distance(target) else LineString(cs[::-1])
    if dn:
        target=Point(dn)
        return g if p1.distance(target)<=p0.distance(target) else LineString(cs[::-1])
    if node:
        fx,fy=node.get("from_canvas_x"),node.get("from_canvas_y")
        if fx is not None and fy is not None:
            target=Point(float(fx),float(fy))
            return g if p0.distance(target)<=p1.distance(target) else LineString(cs[::-1])
    return g

def orient_reach(g,node):
    # Backward-compatible alias for tests/importers; reach2d preprocessing uses
    # orient_routing_line so Subbasin stream segments are oriented too.
    return orient_routing_line(g,{},node)

def _stream_order_column(gdf: gpd.GeoDataFrame):
    lookup={str(c).lower().replace("_", ""):c for c in gdf.columns}
    for name in ("strmorder", "streamorder", "strahlerorder", "strahler"):
        key=name.lower().replace("_", "")
        if key in lookup:
            return lookup[key]
    return None

def _normalize_stream_order(value, fallback=2):
    try:
        order=int(round(float(value)))
    except Exception:
        order=int(fallback)
    return max(1,min(8,order))

def stream_order_base_width(order):
    """Nominal MapLibre line width (px) before Q/Qp class multiplier."""
    o=_normalize_stream_order(order)
    return round(min(4.4,max(1.1,0.8+(0.45*o))),2)

def prep_routing_lines(source,topo,mid):
    g=_as_gdf(source)
    if g.crs is None: raise ValueError("CRS routing centerline tidak ada")
    col=id_col(g,["name","reach_id","reach","subbasin_id","subbasin"]); g=g.copy(); g["element_id"]=g[col].astype(str).str.strip()
    g["element_type"]=g["element_id"].map(lambda x:(topo["nodes"].get(x) or {}).get("type"))
    g=g[g["element_type"].isin(["reach","subbasin"])].copy()
    if g["element_id"].duplicated().any():
        dup=sorted(set(g.loc[g["element_id"].duplicated(False),"element_id"].astype(str)))
        raise ValueError(f"routing centerline memiliki ID topologi duplikat: {dup[:8]}")
    order_col=_stream_order_column(g)
    if order_col is None:
        log("atribut strmorder tidak ditemukan pada routing centerline; memakai fallback orde 2")
        g["strmorder"]=2
    else:
        g["strmorder"]=g[order_col].map(_normalize_stream_order)
    g["base_width"]=g["strmorder"].map(stream_order_base_width)
    oriented=[]
    for _,row in g.iterrows():
        oriented.append(orient_routing_line(row.geometry,row,(topo["nodes"].get(str(row["element_id"])))))
    g["geometry"]=oriented
    g["model_id"]=mid
    g["route_id"]=g["element_id"].map(lambda x:f"{mid}:{x}")
    g["reach_id"]=g.apply(lambda r:r["element_id"] if r["element_type"]=="reach" else None,axis=1)
    g["subbasin_id"]=g.apply(lambda r:r["element_id"] if r["element_type"]=="subbasin" else None,axis=1)
    g["downstream_element"]=g["element_id"].map(lambda x:(topo["nodes"].get(x) or {}).get("downstream"))
    g["downstream_reach_id"]=g["element_id"].map(lambda x:topo["first_downstream_reach"].get(x))
    g["route_len_m"]=[float(x.length) for x in g.geometry]
    g["reach_len_m"]=g.apply(lambda r:r["route_len_m"] if r["element_type"]=="reach" else None,axis=1)
    slim=g[["model_id","route_id","element_id","element_type","reach_id","subbasin_id","downstream_element","downstream_reach_id","route_len_m","reach_len_m","strmorder","base_width","geometry"]].copy()
    return slim.to_crs(WEB_CRS),slim

def prep_reaches(source,topo,mid):
    """Backward-compatible wrapper; output now includes Reach + Subbasin routes."""
    return prep_routing_lines(source,topo,mid)

def _valid_polygonal(g):
    if g is None or g.is_empty:
        return g
    g=force_2d(g)
    try:
        if not g.is_valid:
            g=make_valid(g)
    except Exception:
        pass
    if isinstance(g,(Polygon,MultiPolygon)):
        return g
    if isinstance(g,GeometryCollection):
        parts=[]
        for part in g.geoms:
            if isinstance(part,Polygon): parts.append(part)
            elif isinstance(part,MultiPolygon): parts.extend(list(part.geoms))
        return unary_union(parts) if parts else g
    return g

def prep_subbasins(source,mid):
    g=_as_gdf(source)
    if g.crs is None: raise ValueError("CRS subbasins tidak ada")
    col=id_col(g,["subbasin_id","name","subbasin"]); g=g.copy(); g["geometry"]=g.geometry.map(_valid_polygonal)
    g=g[~g.geometry.is_empty & g.geometry.notna()].copy()
    g["model_id"]=mid; g["subbasin_id"]=g[col].astype(str)
    slim=g[["model_id","subbasin_id","geometry"]].copy(); return slim.to_crs(WEB_CRS),slim

def write_geo(g,path): path.parent.mkdir(parents=True,exist_ok=True); path.write_text(g.to_json(drop_id=True),encoding="utf-8")

def modeled_area(subs,mid):
    fixed=subs.copy(); fixed["geometry"]=fixed.geometry.map(_valid_polygonal)
    fixed=fixed[~fixed.geometry.is_empty & fixed.geometry.notna()].copy()
    if fixed.empty:
        raise ValueError("Tidak ada polygon subbasin valid untuk membentuk modeled area")
    try:
        geom=fixed.geometry.union_all() if hasattr(fixed.geometry,"union_all") else fixed.unary_union
    except Exception:
        geom=unary_union([_valid_polygonal(x) for x in fixed.geometry if x is not None and not x.is_empty])
    geom=_valid_polygonal(geom)
    return gpd.GeoDataFrame([{"model_id":mid,"geometry":geom}],geometry="geometry",crs=WEB_CRS)

def _reference_rivers_for_preprocess():
    if REFERENCE_GPKG.exists():
        return gpd.read_file(REFERENCE_GPKG,layer="official_rivers")
    cache=ROOT/".cache"/"preprocess"/"official_rivers.geojson"
    if cache.exists() and cache.stat().st_size>0:
        return gpd.read_file(cache)
    access=os.getenv("R2_ACCESS_KEY_ID","").strip(); secret=os.getenv("R2_SECRET_ACCESS_KEY","").strip()
    account=os.getenv("R2_ACCOUNT_ID","").strip(); endpoint=os.getenv("R2_ENDPOINT_URL","").strip().rstrip("/")
    if not (access and secret and (endpoint or account)):
        return None
    try:
        import boto3
        from botocore.config import Config
        if not endpoint: endpoint=f"https://{account}.r2.cloudflarestorage.com"
        client=boto3.client("s3",endpoint_url=endpoint,aws_access_key_id=access,aws_secret_access_key=secret,region_name="auto",config=Config(signature_version="s3v4"))
        bucket=os.getenv("R2_REFERENCE_BUCKET","dta-map-assets").strip() or "dta-map-assets"
        prefix=os.getenv("R2_REFERENCE_PREFIX","").strip().strip("/")
        filename=os.getenv("R2_OFFICIAL_RIVERS_KEY","official_rivers.geojson").strip() or "official_rivers.geojson"
        key=f"{prefix}/{filename}" if prefix else filename
        cache.parent.mkdir(parents=True,exist_ok=True)
        client.download_file(bucket,key,str(cache))
        log(f"official rivers reuse dari R2 {bucket}/{key}")
        return gpd.read_file(cache)
    except Exception as exc:
        log(f"official rivers R2 tidak dapat dimuat: {exc}")
        return None

def model_rivers(area,mid):
    cols=["model_id","river_name","river_order","basin_name","river_label","geometry"]
    rivers=_reference_rivers_for_preprocess()
    if rivers is None or rivers.empty:
        log("official rivers belum tersedia; model_rivers kosong")
        return gpd.GeoDataFrame(columns=cols,geometry="geometry",crs=WEB_CRS)
    a=area.to_crs(rivers.crs); mask=a.geometry.union_all() if hasattr(a.geometry,"union_all") else a.unary_union
    out=gpd.clip(rivers,mask).to_crs(WEB_CRS)
    if out.empty: return gpd.GeoDataFrame(columns=cols,geometry="geometry",crs=WEB_CRS)
    out["model_id"]=mid
    if "river_name" not in out:
        for c in ["NAMOBJ","name","NAME"]:
            if c in out: out["river_name"]=out[c]; break
    if "river_order" not in out: out["river_order"]=None
    if "basin_name" not in out: out["basin_name"]=None
    def label(v):
        s=str(v or "").strip()
        for p in ["Kali ","K. ","K ","Sungai ","S. ","S "]:
            if s.lower().startswith(p.lower()): s=s[len(p):].strip(); break
        return f"K. {s}" if s else None
    out["river_label"]=out["river_name"].map(label); return out[[c for c in cols if c in out.columns]].copy()

def rp(stem):
    m=re.search(r"T[_-]?(\d+)",stem,re.I); return int(m.group(1)) if m else None

def pydss_open():
    try:
        from pydsstools.heclib.dss import HecDss
        return HecDss.Open
    except Exception:
        try:
            from pydsstools.heclib.dss.HecDss import Open
            return Open
        except Exception as e:
            raise RuntimeError("pydsstools belum tersedia. Jalankan preprocess_hms.bat atau: py -3 -m pip install -r requirements-preprocess.txt") from e

def _pathname_rows(path):
    """Return DSS catalog rows as (B-element, C-parameter, E-interval, F-version).

    Reach ``FLOW`` is the outflow hydrograph and Reach ``FLOW-COMBINE`` is
    the inflow hydrograph. Both parameters are read from the same Reach
    B-element. Prefer the official pydsstools catalog; the byte scan is only a
    fallback for older pydsstools builds.
    """
    rows=[]
    try:
        Open=pydss_open()
        with Open(path, mode="r") as fid:
            pathnames=list(fid.getPathnameList() or [])
        for pathname in pathnames:
            parts=str(pathname).split("/")
            if len(parts) < 8:
                continue
            b,c,e,f=parts[2],parts[3],parts[5],parts[6]
            c=str(c).strip().upper()
            if c in {"FLOW", "FLOW-COMBINE"} and str(b).strip():
                rows.append((str(b).strip(),c,str(e).strip(),str(f).strip()))
    except Exception:
        rows=[]
    if rows:
        return rows
    try:
        blob=path.read_bytes()
    except Exception:
        return []
    for e,c,i,v in DSS_RE.findall(blob):
        try: rows.append((e.decode(),c.decode().upper(),i.decode(),v.decode()))
        except Exception: pass
    return rows

def dss_meta(path, parameter="FLOW"):
    parameter=str(parameter or "FLOW").strip().upper()
    rows=[x for x in _pathname_rows(path) if x[1] == parameter]
    years=rp(path.stem)
    if not rows:
        return {"parameter":parameter,"interval":"5Minute","version":f"RUN:T={years or ''}","elements":[],"available":False}
    token=path.stem.replace("_","=").upper()
    year_token=f"T={int(years):04d}" if years is not None else token
    chosen=[x for x in rows if token in x[3].upper() or year_token in x[3].upper()] or rows
    counts=defaultdict(int)
    for _,_,i,v in chosen:
        counts[(i,v)]+=1
    interval,version=max(counts,key=counts.get)
    return {"parameter":parameter,"interval":interval,"version":version,
            "elements":sorted({e for e,_,i,v in chosen if i==interval and v==version}),"available":True}

def clean(v):
    try: x=float(v)
    except Exception: return None
    return x if math.isfinite(x) and abs(x)<1e30 else None

def times_of(ts,n):
    for attr in ("pytimes", "times"):
        try:
            x=getattr(ts,attr); x=x() if callable(x) else x
            r=[t.isoformat() if hasattr(t,"isoformat") else str(t) for t in list(x)]
            if len(r)==n:return r
        except Exception: pass
    return [str(i) for i in range(n)]

def extract_dss(path,topo):
    out_meta=dss_meta(path,"FLOW")
    in_meta=dss_meta(path,"FLOW-COMBINE")
    interval,version=out_meta["interval"],out_meta["version"]
    in_interval=in_meta["interval"] if in_meta.get("available") else interval
    in_version=in_meta["version"] if in_meta.get("available") else version
    out_avail=set(out_meta["elements"]); in_avail=set(in_meta["elements"])
    Open=pydss_open()

    raw_out={}; raw_combine={}
    wanted=sorted(topo["nodes"].keys())
    reach_ids=sorted(eid for eid,n in topo["nodes"].items() if n["type"]=="reach")
    with Open(path,mode="r") as fid:
        for eid in wanted:
            if out_meta.get("available") and eid not in out_avail: continue
            try: ts=fid.read_ts(f"//{eid}/FLOW//{interval}/{version}/",trim_missing=True,reg=True,value_precision="float")
            except Exception: continue
            vals=[clean(v) for v in np.asarray(getattr(ts,"values",[]),dtype=float).reshape(-1).tolist()]
            if vals: raw_out[eid]={"times":times_of(ts,len(vals)),"values":vals}
        # Reach inflow is FLOW-COMBINE on that same Reach. Never substitute
        # FLOW/outflow when this record is missing.
        for rid in reach_ids:
            if in_meta.get("available") and rid not in in_avail: continue
            try: ts=fid.read_ts(f"//{rid}/FLOW-COMBINE//{in_interval}/{in_version}/",trim_missing=True,reg=True,value_precision="float")
            except Exception: continue
            vals=[clean(v) for v in np.asarray(getattr(ts,"values",[]),dtype=float).reshape(-1).tolist()]
            if vals: raw_combine[rid]={"times":times_of(ts,len(vals)),"values":vals}

    items=list(raw_out.values())+list(raw_combine.values())
    if not items: raise RuntimeError(f"Tidak ada FLOW/FLOW-COMBINE yang terbaca dari {path.name}")
    axis=list(max(items,key=lambda x:len(x["times"]))["times"]); idx={t:i for i,t in enumerate(axis)}
    def align(raw):
        aligned={}
        for eid,item in raw.items():
            arr=[None]*len(axis)
            for t,v in zip(item["times"],item["values"]):
                if t in idx: arr[idx[t]]=v
            aligned[eid]=arr
        return aligned
    aligned=align(raw_out); aligned_combine=align(raw_combine)

    def group(kind):
        rows={eid:aligned[eid] for eid,n in topo["nodes"].items() if n["type"]==kind and eid in aligned}
        peaks={}; peak_indices={}
        for eid,arr in rows.items():
            valid=[float(v) for v in arr if v is not None]
            peak=max(valid) if valid else 0.0; peaks[eid]=peak
            hits=[i for i,v in enumerate(arr) if v is not None and float(v)==peak]
            peak_indices[eid]=hits[-1] if hits else None
        return rows,peaks,peak_indices
    r,rpks,ridx=group("reach"); s,spks,sidx=group("subbasin"); j,jpks,jidx=group("junction")

    reach_inflows={}; reach_inflow_peaks={}; reach_inflow_peak_indices={}; reach_inflow_sources={}; reach_inflow_modes={}
    for rid in reach_ids:
        if rid not in aligned_combine: continue
        arr=aligned_combine[rid]
        reach_inflows[rid]=arr
        reach_inflow_sources[rid]=[f"{rid}/FLOW-COMBINE"]
        reach_inflow_modes[rid]="dss_reach_flow_combine"
        valid=[float(v) for v in arr if v is not None]
        peak=max(valid) if valid else 0.0; reach_inflow_peaks[rid]=peak
        hits=[i for i,v in enumerate(arr) if v is not None and float(v)==peak]
        reach_inflow_peak_indices[rid]=hits[-1] if hits else None

    return {"schema_version":5,"dataset_id":path.stem,"return_period_years":rp(path.stem),"parameter":"FLOW","inflow_parameter":"FLOW-COMBINE","inflow_location":"reach","units":"M3/S","interval":interval,"version":version,"times":axis,
            "reaches":r,"reach_peaks":rpks,"reach_peak_indices":ridx,
            "reach_inflows":reach_inflows,"reach_inflow_peaks":reach_inflow_peaks,"reach_inflow_peak_indices":reach_inflow_peak_indices,"reach_inflow_sources":reach_inflow_sources,"reach_inflow_modes":reach_inflow_modes,
            "subbasins":s,"subbasin_peaks":spks,"subbasin_peak_indices":sidx,
            "junctions":j,"junction_peaks":jpks,"junction_peak_indices":jidx}

def gz(payload,path):
    path.parent.mkdir(parents=True,exist_ok=True)
    with gzip.open(path,"wt",encoding="utf-8",compresslevel=6) as f: json.dump(payload,f,separators=(",",":"),ensure_ascii=False,allow_nan=False)
def validate_spatial_ids(topo,rw,sw):
    topo_reaches={nid for nid,n in topo["nodes"].items() if n["type"]=="reach"}
    topo_subs={nid for nid,n in topo["nodes"].items() if n["type"]=="subbasin"}
    route_reaches=set(rw.loc[rw["element_type"]=="reach","element_id"].astype(str))
    route_subs=set(rw.loc[rw["element_type"]=="subbasin","element_id"].astype(str))
    gis_subs=set(sw["subbasin_id"].astype(str))
    problems=[]
    if topo_reaches != route_reaches:
        problems.append(f"reach routing mismatch: basin={len(topo_reaches)}, reach2d={len(route_reaches)}, hilang={sorted(topo_reaches-route_reaches)[:8]}")
    if topo_subs != gis_subs:
        problems.append(f"subbasin polygon mismatch: basin={len(topo_subs)}, GIS={len(gis_subs)}, hilang={sorted(topo_subs-gis_subs)[:8]}")
    if not route_subs:
        problems.append("reach2d tidak memuat satu pun centerline bernama Subbasin; jaringan hulu akan terbatas pada Reach")
    if problems and any(not x.startswith("reach2d tidak memuat") for x in problems):
        raise ValueError("Validasi HMS/GIS gagal; " + " | ".join(problems))
    for warning in problems:
        log(f"PERINGATAN: {warning}")

def process(folder,skip_dss=False):
    mid=folder.name
    basin=find_one(folder,["*.basin","*.BASIN"])
    sqlite_path=find_one(folder,["*.sqlite","*.SQLITE"])
    topo=parse_basin(basin)
    spatial=load_sqlite_spatial(sqlite_path,topo)
    out=HMS_ROOT/mid; shutil.rmtree(out,ignore_errors=True); out.mkdir(parents=True,exist_ok=True)
    log(f"{mid}: topologi {topo['counts']}")
    log(f"{mid}: SQLite {sqlite_path.name} -> layer {spatial['layers']}")
    rw,rs=prep_routing_lines(spatial["routing_lines"],topo,mid)
    sw,_=prep_subbasins(spatial["subbasins"],mid)
    validate_spatial_ids(topo,rw,sw); area=modeled_area(sw,mid); rivers=model_rivers(area,mid)
    write_geo(rw,out/"reaches.geojson"); write_geo(sw,out/"subbasins.geojson"); write_geo(area,out/"modeled_area.geojson"); write_geo(rivers,out/"model_rivers.geojson"); (out/"topology.json").write_text(json.dumps(topo,ensure_ascii=False,indent=2),encoding="utf-8")
    dss_files=sorted({*folder.glob("T_*.dss"),*folder.glob("T_*.DSS")},key=lambda x:x.name.lower())
    scenarios=[]
    for dss in dss_files:
        years=rp(dss.stem); rel=f"scenarios/{dss.stem}.flow.json.gz"; rec={"id":dss.stem,"return_period_years":years,"label":f"{years} Tahun" if years is not None else dss.stem,"flow":rel,"ready":False}
        if not skip_dss:
            log(f"{mid}: {dss.name} -> {rel}"); p=extract_dss(dss,topo); gz(p,out/rel); rec.update(ready=True,interval=p["interval"],version=p["version"],reach_series_count=len(p["reaches"]),reach_inflow_series_count=len(p["reach_inflows"]),subbasin_series_count=len(p["subbasins"]),junction_series_count=len(p["junctions"]))
        scenarios.append(rec)
    source_manifest={
        "schema_version":4,
        "model_id":mid,
        "source_folder":folder.name,
        "basin":basin.name,
        "sqlite":sqlite_path.name,
        "sqlite_layers":spatial["layers"],
        "routing_centerline_rule":"reach2d.name matched to Reach/Subbasin IDs in .basin",
        "dss":[d.name for d in dss_files],
    }
    (out/"source_manifest.json").write_text(json.dumps(source_manifest,ensure_ascii=False,indent=2),encoding="utf-8")
    counts={
        "routing_lines":len(rw),
        "reach_lines":int((rw["element_type"]=="reach").sum()),
        "subbasin_lines":int((rw["element_type"]=="subbasin").sum()),
        "subbasins":len(sw),
        "model_rivers":len(rivers),
    }
    meta={"schema_version":2,"id":mid,"name":topo["model_name"],"path":mid,"topology":"topology.json","source_manifest":"source_manifest.json","reaches":"reaches.geojson","routing_lines":"reaches.geojson","subbasins":"subbasins.geojson","modeled_area":"modeled_area.geojson","model_rivers":"model_rivers.geojson","scenarios":scenarios,"default_scenario":scenarios[0]["id"] if scenarios else None,"counts":counts}
    (out/"model.json").write_text(json.dumps(meta,ensure_ascii=False,indent=2),encoding="utf-8"); return meta
def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--model"); ap.add_argument("--skip-dss",action="store_true"); a=ap.parse_args(); SOURCE_ROOT.mkdir(parents=True,exist_ok=True); HMS_ROOT.mkdir(parents=True,exist_ok=True)
    folders=[SOURCE_ROOT/a.model] if a.model else sorted(p for p in SOURCE_ROOT.iterdir() if p.is_dir()); folders=[p for p in folders if p.exists()]
    if not folders: log(f"Belum ada model. Buat {SOURCE_ROOT/'Oyo'} lalu isi *.basin, *.sqlite, dan T_*.dss"); return 2
    models=[]
    for f in folders:
        try: models.append(process(f,a.skip_dss))
        except Exception as e: log(f"GAGAL {f.name}: {e}"); return 1
    (HMS_ROOT/"index.json").write_text(json.dumps({"schema_version":1,"generated_by":"scripts/preprocess_hms.py","models":models},ensure_ascii=False,indent=2),encoding="utf-8"); log(f"Selesai: {HMS_ROOT}"); return 0
if __name__=="__main__": raise SystemExit(main())
