#!/usr/bin/env python3
"""Ingest administrative boundaries from BIG Geoportal into processed GeoJSON & release assets."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import gc
import gzip
import hashlib
import json
import os
import re
import sys
import tempfile
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
import shapely
from shapely.geometry import MultiPolygon
from shapely.ops import unary_union

DEFAULT_BIG_URL = "https://drive.big.go.id/s/9x9dJWSNfQ3wp7S/download"
DEFAULT_METADATA_URL = "https://drive.big.go.id/s/JyaLm2j8HSz3sEC/download"
DEFAULT_DISCLAIMER = "Data batas yang berstatus indikatif tidak dapat dijadikan referensi hukum."
PROVINCE_CODE_COLS = ["KDPPUM", "KDPBPS"]
DISTRICT_CODE_COLS = ["KDPPUM", "KDPKAB", "KDPBPS", "KDBBPS"]
NULL_TOKENS = {"", "none", "null", "nan"}
LARGE_COMPONENT_COORDS = 3_000_000


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ingest BIG Geoportal boundary archives into national, province, and district GeoJSON files."
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=None,
        help="Path to local input dataset (.gdb, .gdb.zip, .zip, .gpkg, .shp, or .geojson).",
    )
    parser.add_argument(
        "--download",
        action="store_true",
        help="Download BIG dataset archive and ISO metadata XML into --raw-dir before ingesting.",
    )
    parser.add_argument(
        "--force-download",
        action="store_true",
        help="Re-download files even if they already exist in --raw-dir.",
    )
    parser.add_argument("--download-url", default=DEFAULT_BIG_URL, help="BIG dataset archive URL.")
    parser.add_argument(
        "--metadata-url",
        default=DEFAULT_METADATA_URL,
        help="BIG ISO 19115 metadata XML URL (pass empty string to skip).",
    )
    parser.add_argument(
        "--metadata-xml",
        type=Path,
        default=None,
        help="Optional local ISO 19115 XML metadata file.",
    )
    parser.add_argument(
        "--layer",
        default=None,
        help="Vector layer name inside .gdb/.gpkg (auto-detected if omitted).",
    )
    parser.add_argument("--raw-dir", type=Path, default=Path("raw-data"), help="Raw downloads folder.")
    parser.add_argument("--output-dir", type=Path, default=Path("processed-data"), help="Output GeoJSON root.")
    parser.add_argument("--dist-dir", type=Path, default=Path("dist"), help="Release bundle folder.")
    parser.add_argument("--skip-dist", action="store_true", help="Skip generating dist/ release bundles.")
    parser.add_argument(
        "--metadata-out",
        type=Path,
        default=Path("metadata/source.json"),
        help="Path for provenance metadata JSON output.",
    )
    parser.add_argument(
        "--catalog-out",
        type=Path,
        default=Path("metadata/catalog.json"),
        help="Path for machine-readable catalog manifest JSON output.",
    )
    parser.add_argument(
        "--build-catalog-only",
        action="store_true",
        help="Build --catalog-out from existing --output-dir and --metadata-out without raw-data ingestion.",
    )
    parser.add_argument("--precision", type=int, default=6, help="GeoJSON coordinate decimal places.")
    parser.add_argument(
        "--detail-simplify-tolerance",
        type=float,
        default=2e-05,
        help="Coverage simplification tolerance (degrees) for high-detail/ outputs.",
    )
    parser.add_argument(
        "--simplify-tolerance",
        type=float,
        default=0.001,
        help="Coverage simplification tolerance (degrees) for simplified/ nationwide outputs.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=min(4, os.cpu_count() or 1),
        help="Number of worker threads for parallel component/release processing.",
    )
    parser.add_argument("--edition", default=None, help="Optional edition label override.")

    args = parser.parse_args(argv)
    if not args.build_catalog_only and args.input is None and not args.download:
        parser.error("Provide --input PATH or --download.")
    if args.build_catalog_only and args.edition is not None:
        parser.error("--edition cannot be combined with --build-catalog-only.")
    if args.precision < 0:
        parser.error("--precision must be >= 0.")
    if args.detail_simplify_tolerance < 0:
        parser.error("--detail-simplify-tolerance must be >= 0.")
    if args.simplify_tolerance < 0:
        parser.error("--simplify-tolerance must be >= 0.")
    if args.workers < 1:
        parser.error("--workers must be >= 1.")
    return args


def _filename_from_cd(content_disposition: str | None) -> str | None:
    if not content_disposition:
        return None
    star_match = re.search(r"filename\*\s*=\s*[^']*'[^']*'(?P<name>[^;\r\n]+)", content_disposition, re.I)
    if star_match:
        return Path(urllib.parse.unquote(star_match.group("name").strip().strip('"'))).name
    plain_match = re.search(r'filename\s*=\s*"?(?P<name>[^";\r\n]+)"?', content_disposition, re.I)
    if plain_match:
        return Path(plain_match.group("name").strip()).name
    return None


def download_file(url: str, dest_dir: Path, fallback_name: str, force: bool = False) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": "Mozilla/5.0"}

    # Probe filename via HEAD when possible so existing files aren't re-fetched unnecessarily.
    filename = None
    try:
        head_req = urllib.request.Request(url, headers=headers, method="HEAD")
        with urllib.request.urlopen(head_req, timeout=30) as resp:
            filename = _filename_from_cd(resp.headers.get("Content-Disposition"))
            if not filename:
                url_name = Path(urllib.parse.urlparse(resp.geturl()).path).name
                if url_name and url_name != "download":
                    filename = url_name
    except Exception:
        filename = None

    if filename:
        dest_path = dest_dir / filename
        if dest_path.exists() and not force:
            print(f"Using existing download: {dest_path}", flush=True)
            return dest_path

    get_req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(get_req, timeout=120) as resp:
        resolved_name = filename or _filename_from_cd(resp.headers.get("Content-Disposition")) or fallback_name
        dest_path = dest_dir / Path(resolved_name).name
        if dest_path.exists() and not force:
            print(f"Using existing download: {dest_path}", flush=True)
            return dest_path

        part_path = dest_path.with_suffix(dest_path.suffix + ".part")
        total = 0
        with open(part_path, "wb") as out:
            while chunk := resp.read(1024 * 1024):
                out.write(chunk)
                total += len(chunk)
        part_path.replace(dest_path)
        print(f"Downloaded {dest_path} ({total / 1_000_000:.2f} MB)", flush=True)
        return dest_path


def compute_sha256(path: Path) -> str:
    h = hashlib.sha256()
    if path.is_file():
        with open(path, "rb") as f:
            while chunk := f.read(1024 * 1024):
                h.update(chunk)
        return h.hexdigest()
    if path.is_dir():
        entries = sorted(p for p in path.rglob("*") if p.is_file())
        for entry in entries:
            rel = entry.relative_to(path).as_posix()
            h.update(f"{rel}:{entry.stat().st_size}\n".encode("utf-8"))
        return h.hexdigest()
    raise FileNotFoundError(f"Input path does not exist: {path}")


def extract_zip_safely(zip_path: Path, target_dir: Path) -> Path:
    root = target_dir.resolve()
    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            dest = (root / info.filename).resolve()
            if not dest.is_relative_to(root):
                raise ValueError(f"Unsafe path in zip archive: {info.filename}")
        zf.extractall(root)

    for pattern, want_dir in [("*.gdb", True), ("*.gpkg", False), ("*.shp", False), ("*.geojson", False)]:
        matches = sorted(
            (p for p in root.rglob(pattern) if (p.is_dir() if want_dir else p.is_file())),
            key=lambda p: (len(p.parts), str(p)),
        )
        if matches:
            return matches[0]

    raise FileNotFoundError(f"No .gdb, .gpkg, .shp, or .geojson dataset found inside {zip_path}")


def select_layer(dataset_path: Path, requested_layer: str | None) -> str:
    layers = pyogrio.list_layers(dataset_path)
    layer_list = [(str(name), None if gtype is None else str(gtype)) for name, gtype in layers]
    if not layer_list:
        raise ValueError(f"No layers found in dataset: {dataset_path}")

    names = [name for name, _ in layer_list]
    if requested_layer is not None:
        if requested_layer not in names:
            raise ValueError(f"Layer '{requested_layer}' not found. Available layers: {layer_list}")
        return requested_layer

    if len(layer_list) == 1:
        return layer_list[0][0]

    poly_layers = [
        (name, gtype)
        for name, gtype in layer_list
        if gtype and "polygon" in gtype.lower() and "anno" not in name.lower()
    ]
    if len(poly_layers) == 1:
        return poly_layers[0][0]

    candidates = poly_layers or layer_list
    for keywords in [("KABKOTA",), ("ADMINISTRASI", "AR"), ("ADMINISTRASI",), ("_AR",)]:
        matched = [name for name, _ in candidates if all(kw in name.upper() for kw in keywords)]
        if len(matched) == 1:
            return matched[0]

    raise ValueError(
        f"Ambiguous layers in {dataset_path}. Pass --layer explicitly. Available layers: {layer_list}"
    )


def parse_iso_metadata(xml_path: Path) -> dict[str, Any]:
    root = ET.parse(xml_path).getroot()

    def local_name(tag: str) -> str:
        return tag.split("}", 1)[-1]

    def find_path_text(node: ET.Element, path: list[str]) -> str | None:
        cur = [node]
        for seg in path:
            nxt: list[ET.Element] = []
            for item in cur:
                nxt.extend(child for child in item if local_name(child.tag) == seg)
            cur = nxt
            if not cur:
                return None
        for item in cur:
            text = (item.text or "").strip()
            if text:
                return text
            for sub in item.iter():
                if local_name(sub.tag) in {"CharacterString", "Date", "DateTime"}:
                    val = (sub.text or "").strip()
                    if val:
                        return val
        return None

    return {
        "metadata_id": find_path_text(root, ["metadataIdentifier", "MD_Identifier", "code"]),
        "title": find_path_text(root, ["identificationInfo", "MD_DataIdentification", "citation", "CI_Citation", "title"]),
        "edition": find_path_text(root, ["identificationInfo", "MD_DataIdentification", "citation", "CI_Citation", "edition"]),
        "edition_date": find_path_text(root, ["identificationInfo", "MD_DataIdentification", "citation", "CI_Citation", "editionDate"]),
        "use_limitation": find_path_text(root, ["identificationInfo", "MD_DataIdentification", "resourceConstraints", "MD_LegalConstraints", "useLimitation"]),
    }


def _extract_polygonal(geom: Any) -> Any:
    if geom is None or geom.is_empty:
        return None
    gtype = geom.geom_type
    if gtype == "Polygon":
        return geom if geom.area > 0 else None
    if gtype == "MultiPolygon":
        polys = [p for p in geom.geoms if not p.is_empty and p.area > 0]
        if not polys:
            return None
        return polys[0] if len(polys) == 1 else MultiPolygon(polys)
    if gtype == "GeometryCollection":
        polys: list[Any] = []
        for part in geom.geoms:
            extracted = _extract_polygonal(part)
            if extracted is None:
                continue
            if extracted.geom_type == "Polygon":
                polys.append(extracted)
            elif extracted.geom_type == "MultiPolygon":
                polys.extend(extracted.geoms)
        if not polys:
            return None
        return _extract_polygonal(unary_union(polys))
    return None


def _round_geom_2d(geom: Any, precision: int) -> Any:
    return shapely.transform(geom, lambda coords: np.round(coords, precision) + 0.0)


def count_interior_holes(geom: Any) -> int:
    if geom is None or geom.is_empty:
        return 0
    if geom.geom_type == "Polygon":
        return len(geom.interiors)
    if geom.geom_type == "MultiPolygon":
        return sum(len(poly.interiors) for poly in geom.geoms)
    return 0


def _orient_ccw(geom: Any) -> Any:
    if hasattr(shapely, "orient_polygons"):
        return shapely.orient_polygons(geom, exterior_cw=False)
    return geom


def _verify_json_roundtrip(geom: Any, precision: int) -> Any:
    grid = 10 ** (-precision)
    cur = geom
    for _ in range(3):
        rounded_geom = _round_geom_2d(cur, precision)
        if not rounded_geom.is_empty and rounded_geom.is_valid:
            return _orient_ccw(rounded_geom)
        repaired = _extract_polygonal(shapely.make_valid(rounded_geom))
        if repaired is None or repaired.is_empty:
            return None
        repaired = shapely.set_precision(repaired, grid)
        repaired = _extract_polygonal(shapely.make_valid(repaired) if not repaired.is_valid else repaired)
        if repaired is None or repaired.is_empty:
            return None
        cur = repaired

    rounded_geom = _round_geom_2d(cur, precision)
    if rounded_geom.is_empty or not rounded_geom.is_valid:
        return None
    return _orient_ccw(rounded_geom)


def clean_geometry(geom: Any, precision: int | None = None) -> Any:
    if geom is None or geom.is_empty:
        return None
    geom = shapely.force_2d(geom)
    if precision is not None:
        geom = shapely.set_precision(geom, 10 ** (-precision))
    if not geom.is_valid:
        geom = shapely.make_valid(geom)
    geom = _extract_polygonal(geom)
    if geom is None or geom.is_empty:
        return None
    if not geom.is_valid:
        geom = _extract_polygonal(shapely.make_valid(geom))
        if geom is None or geom.is_empty:
            return None
    if precision is not None:
        return _verify_json_roundtrip(geom, precision)
    return _orient_ccw(geom)


def simplify_polygonal(geom: Any, tolerance: float, precision: int | None = None) -> Any:
    if tolerance <= 0:
        return clean_geometry(geom, precision=precision)
    simplified = clean_geometry(geom.simplify(tolerance, preserve_topology=True), precision=precision)
    if simplified is None or simplified.is_empty:
        return clean_geometry(geom, precision=precision)
    return simplified


def _connected_components(geoms: list[Any], pad: float = 0.002) -> list[list[int]]:
    n = len(geoms)
    if n == 0:
        return []
    if n == 1:
        return [[0]]

    tree = shapely.STRtree(geoms)
    left, right = tree.query(geoms, predicate="dwithin", distance=pad)

    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, j in zip(left.tolist(), right.tolist()):
        ri, rj = find(i), find(j)
        if ri != rj:
            if ri < rj:
                parent[rj] = ri
            else:
                parent[ri] = rj

    groups: dict[int, list[int]] = {}
    for idx in range(n):
        groups.setdefault(find(idx), []).append(idx)
    return [groups[k] for k in sorted(groups)]


def _run_components(
    geoms: list[Any],
    components: list[list[int]],
    fn: Any,
    workers: int,
) -> list[Any]:
    result: list[Any] = [None] * len(geoms)
    if workers <= 1 or len(components) <= 1:
        for comp in components:
            comp_geoms = [geoms[i] for i in comp]
            comp_out = fn(comp_geoms)
            for idx, out_g in zip(comp, comp_out):
                result[idx] = out_g
            del comp_geoms, comp_out
            gc.collect()
        return result

    sized = sorted(
        ((sum(int(shapely.get_num_coordinates(geoms[i])) for i in comp), comp) for comp in components),
        key=lambda item: item[0],
        reverse=True,
    )
    large_comps = [comp for pts, comp in sized if pts >= LARGE_COMPONENT_COORDS]
    small_comps = [comp for pts, comp in sized if pts < LARGE_COMPONENT_COORDS]

    # Isolate large components sequentially so multiple multi-million-vertex GEOS graphs don't spike RAM.
    for comp in large_comps:
        comp_geoms = [geoms[i] for i in comp]
        comp_out = fn(comp_geoms)
        for idx, out_g in zip(comp, comp_out):
            result[idx] = out_g
        del comp_geoms, comp_out
        gc.collect()

    if len(small_comps) == 1:
        comp = small_comps[0]
        comp_geoms = [geoms[i] for i in comp]
        comp_out = fn(comp_geoms)
        for idx, out_g in zip(comp, comp_out):
            result[idx] = out_g
        del comp_geoms, comp_out
        gc.collect()
    elif small_comps:
        with ThreadPoolExecutor(max_workers=min(workers, len(small_comps))) as ex:
            futures = [(comp, ex.submit(fn, [geoms[i] for i in comp])) for comp in small_comps]
            for comp, fut in futures:
                for idx, out_g in zip(comp, fut.result()):
                    result[idx] = out_g
        gc.collect()

    return result


def _clean_coverage_component(comp_geoms: list[Any], precision: int | None) -> list[Any]:
    if len(comp_geoms) == 1:
        cleaned_single = clean_geometry(comp_geoms[0], precision=precision)
        if cleaned_single is None:
            raise ValueError("Single geometry became empty during cleaning.")
        return [cleaned_single]

    if precision is None:
        if shapely.coverage_is_valid(comp_geoms):
            result: list[Any] = []
            for idx, g in enumerate(comp_geoms):
                out_g = _orient_ccw(g) if (g is not None and not g.is_empty and g.is_valid) else clean_geometry(g, precision=None)
                if out_g is None:
                    raise ValueError(f"Geometry became empty at component index {idx}.")
                result.append(out_g)
            return result

        cleaned = list(shapely.coverage_clean(comp_geoms))
        result = []
        for idx, cg in enumerate(cleaned):
            g = _extract_polygonal(cg)
            if g is None or g.is_empty:
                g = clean_geometry(comp_geoms[idx], precision=None)
            elif not g.is_valid:
                g = clean_geometry(g, precision=None)
            else:
                g = _orient_ccw(g)
            if g is None:
                raise ValueError(f"Coverage cleaning produced empty geometry at component index {idx}.")
            result.append(g)
        return result

    grid = 10 ** (-precision)
    snapped = [_extract_polygonal(shapely.set_precision(g, grid)) or g for g in comp_geoms]
    cleaned = list(shapely.coverage_clean(snapped))
    result = []
    for idx, cg in enumerate(cleaned):
        g = _extract_polygonal(cg)
        if g is None or g.is_empty:
            g = _extract_polygonal(shapely.set_precision(comp_geoms[idx], grid)) or comp_geoms[idx]
        verified = _verify_json_roundtrip(g, precision)
        if verified is None:
            verified = clean_geometry(comp_geoms[idx], precision=precision)
        if verified is None:
            raise ValueError(f"Coverage precision rounding produced empty geometry at component index {idx}.")
        result.append(verified)

    if len(result) >= 2 and not shapely.coverage_is_valid(result):
        recleaned = list(shapely.coverage_clean(result))
        refined: list[Any] = []
        for idx, cg in enumerate(recleaned):
            g = _extract_polygonal(cg)
            if g is None or g.is_empty:
                refined.append(result[idx])
                continue
            verified = _verify_json_roundtrip(g, precision)
            refined.append(verified if verified is not None else result[idx])
        result = refined

    return result


def clean_coverage_geometries(
    geoms: list[Any],
    precision: int | None = None,
    pad: float = 0.002,
    workers: int = 1,
) -> list[Any]:
    if not geoms:
        return []
    components = _connected_components(geoms, pad=pad)
    effective_workers = workers if precision is not None else 1
    return _run_components(
        geoms,
        components,
        lambda comp_geoms: _clean_coverage_component(comp_geoms, precision=precision),
        workers=effective_workers,
    )


def _simplify_coverage_component(comp_geoms: list[Any], tolerance: float, precision: int) -> list[Any]:
    if len(comp_geoms) == 1:
        simplified = simplify_polygonal(comp_geoms[0], tolerance, precision=precision)
        if simplified is None:
            raise ValueError("Single geometry became empty during simplification.")
        return [simplified]

    simplified_raw = list(shapely.coverage_simplify(comp_geoms, tolerance))
    non_empty = [
        sg if (sg is not None and not sg.is_empty) else comp_geoms[idx]
        for idx, sg in enumerate(simplified_raw)
    ]
    return _clean_coverage_component(non_empty, precision=precision)


def simplify_coverage_geometries(
    geoms: list[Any],
    tolerance: float,
    precision: int,
    workers: int = 1,
) -> list[Any]:
    if not geoms:
        return []
    pad = max(tolerance * 5, 0.002)
    if tolerance <= 0:
        return clean_coverage_geometries(geoms, precision=precision, pad=pad, workers=workers)

    components = _connected_components(geoms, pad=pad)
    return _run_components(
        geoms,
        components,
        lambda comp_geoms: _simplify_coverage_component(comp_geoms, tolerance, precision),
        workers=workers,
    )


def _safe_coverage_union(geoms: list[Any]) -> Any:
    if len(geoms) == 1:
        return geoms[0]
    try:
        return shapely.coverage_union_all(geoms)
    except Exception:
        return unary_union(geoms)


def dissolve_coverage_by(
    gdf: gpd.GeoDataFrame,
    group_col: str,
    code_cols: list[str],
    precision: int | None = None,
    workers: int = 1,
) -> gpd.GeoDataFrame:
    rows: list[dict[str, Any]] = []
    for key in sorted(gdf[group_col].unique()):
        sub = gdf.loc[gdf[group_col] == key]
        merged = _safe_coverage_union(list(sub.geometry))
        merged = _extract_polygonal(merged) or clean_geometry(merged, precision=None)
        row: dict[str, Any] = {group_col: key}
        for col in code_cols:
            row[col] = first_non_null(sub[col])
        row["geometry"] = merged
        rows.append(row)

    out_gdf = gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326")
    out_gdf["geometry"] = clean_coverage_geometries(list(out_gdf["geometry"]), precision=precision, workers=workers)
    return out_gdf.sort_values([group_col]).reset_index(drop=True)


def clean_str(val: Any) -> str | None:
    if val is None or pd.isna(val):
        return None
    s = str(val).strip()
    if s.lower() in NULL_TOKENS:
        return None
    return s


def first_non_null(series: pd.Series) -> str | None:
    vals = sorted({v for v in series if v is not None and not pd.isna(v)})
    return vals[0] if vals else None


def sanitize_filename(name: str) -> str:
    cleaned = re.sub(r"[\s/\\]+", "_", str(name).strip())
    cleaned = re.sub(r'[<>:"|?*]', "", cleaned).strip("_")
    return cleaned or "unnamed"


def _json_scalar(val: Any) -> Any:
    if val is None or pd.isna(val):
        return None
    if hasattr(val, "item"):
        return val.item()
    return val


def stream_geojson(gdf: gpd.GeoDataFrame, fp: BinaryIO, precision: int | None) -> None:
    prop_cols = [c for c in gdf.columns if c != gdf.geometry.name]
    geoms = gdf[gdf.geometry.name].tolist()
    col_vals = [gdf[c].tolist() for c in prop_cols]

    fp.write(b'{"type":"FeatureCollection","features":[')
    for idx, geom_obj in enumerate(geoms):
        if idx > 0:
            fp.write(b",")
        props = {col: _json_scalar(col_vals[j][idx]) for j, col in enumerate(prop_cols)}
        props_bytes = json.dumps(props, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if precision is None:
            # Stream C++ Shapely GeoJSON directly so multi-million-vertex polygons avoid Python dict bloat.
            geom_bytes = shapely.to_geojson(geom_obj).encode("utf-8")
        else:
            geom_bytes = shapely.to_geojson(_round_geom_2d(geom_obj, precision)).encode("utf-8")

        fp.write(b'{"type":"Feature","properties":')
        fp.write(props_bytes)
        fp.write(b',"geometry":')
        fp.write(geom_bytes)
        fp.write(b"}")
    fp.write(b"]}\n")


def write_geojson(gdf: gpd.GeoDataFrame, path: Path, precision: int | None) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() == ".gz":
        with gzip.open(path, "wb", compresslevel=6) as f:
            stream_geojson(gdf, f, precision)
    else:
        with open(path, "wb") as f:
            stream_geojson(gdf, f, precision)
    return path.stat().st_size


def _district_filenames(districts_gdf: gpd.GeoDataFrame) -> list[str]:
    base_names = [sanitize_filename(str(v)) for v in districts_gdf["WADMKK"]]
    name_counts = pd.Series(base_names).value_counts().to_dict()
    filenames: list[str] = []
    for idx in range(len(districts_gdf)):
        prov_name = str(districts_gdf.iloc[idx]["WADMPR"])
        base = base_names[idx]
        if name_counts.get(base, 0) > 1:
            filenames.append(f"{base}__{sanitize_filename(prov_name)}.geojson")
        else:
            filenames.append(f"{base}.geojson")
    return filenames


def _clear_geojson_dir(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for old_file in directory.glob("*.geojson"):
        old_file.unlink()


def _dir_stats(directory: Path) -> dict[str, int]:
    files = list(directory.glob("*.geojson"))
    sizes = [f.stat().st_size for f in files]
    return {"file_count": len(files), "max_file_bytes": max(sizes) if sizes else 0}


def load_full_districts(dataset_path: Path, layer_name: str) -> tuple[gpd.GeoDataFrame, dict[str, Any]]:
    gdf = gpd.read_file(dataset_path, layer=layer_name, engine="pyogrio")
    raw_feature_count = len(gdf)
    print(f"Loaded {raw_feature_count} raw features; cleaning and normalizing...", flush=True)

    if gdf.crs is None:
        print("Warning: dataset has no CRS; assuming EPSG:4326.", file=sys.stderr, flush=True)
        gdf = gdf.set_crs(epsg=4326)
    elif gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(epsg=4326)

    for required in ("WADMPR", "WADMKK"):
        if required not in gdf.columns:
            raise ValueError(f"Required column '{required}' missing from layer '{layer_name}'.")

    norm_cols = ["WADMPR", "WADMKK"] + [
        c for c in sorted(set(PROVINCE_CODE_COLS) | set(DISTRICT_CODE_COLS)) if c in gdf.columns
    ]
    for col in norm_cols:
        gdf[col] = gdf[col].map(clean_str)

    gdf["geometry"] = gdf["geometry"].map(lambda g: clean_geometry(g, precision=None))
    empty_geom_mask = gdf["geometry"].isna()
    dropped_empty_geometries = int(empty_geom_mask.sum())
    gdf = gdf.loc[~empty_geom_mask].copy()

    null_pr_mask = gdf["WADMPR"].isna()
    dropped_null_wadmpr = int(null_pr_mask.sum())
    gdf = gdf.loc[~null_pr_mask].copy()

    null_kk_mask = gdf["WADMKK"].isna()
    dropped_null_wadmkk = int(null_kk_mask.sum())
    dropped_null_wadmkk_by_province = (
        gdf.loc[null_kk_mask, "WADMPR"].value_counts().sort_index().to_dict() if dropped_null_wadmkk else {}
    )
    dropped_null_wadmkk_by_province = {str(k): int(v) for k, v in dropped_null_wadmkk_by_province.items()}

    valid_gdf = gdf.loc[~null_kk_mask].copy()
    if valid_gdf.empty:
        raise ValueError("No valid district features remain after filtering empty geometries and null names.")

    print("Dissolving full-resolution districts...", flush=True)
    dist_code_cols = [c for c in DISTRICT_CODE_COLS if c in valid_gdf.columns]
    dist_cols = ["WADMPR", "WADMKK", *dist_code_cols, "geometry"]
    if dist_code_cols:
        districts_full = valid_gdf[dist_cols].dissolve(
            by=["WADMPR", "WADMKK"],
            aggfunc={c: first_non_null for c in dist_code_cols},
            as_index=False,
        )
    else:
        districts_full = valid_gdf[dist_cols].dissolve(by=["WADMPR", "WADMKK"], as_index=False)

    del gdf, valid_gdf
    gc.collect()

    districts_full["geometry"] = districts_full["geometry"].map(lambda g: clean_geometry(g, precision=None))
    districts_full = districts_full.loc[~districts_full["geometry"].isna()].copy()
    districts_full = districts_full.set_crs(epsg=4326, allow_override=True)
    districts_full = districts_full.sort_values(["WADMPR", "WADMKK"]).reset_index(drop=True)

    print("Cleaning full-resolution district coverage by connected components...", flush=True)
    districts_full["geometry"] = clean_coverage_geometries(list(districts_full["geometry"]), precision=None, workers=1)

    raw_counts = {
        "raw_features": int(raw_feature_count),
        "dropped_empty_geometries": dropped_empty_geometries,
        "dropped_null_wadmpr": dropped_null_wadmpr,
        "dropped_null_wadmkk": dropped_null_wadmkk,
        "dropped_null_wadmkk_by_province": dropped_null_wadmkk_by_province,
    }
    return districts_full, raw_counts


def _zip_gpkg(src_gpkg: Path, zip_path: Path) -> Path:
    try:
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
            zf.write(src_gpkg, arcname="indonesia_admin_boundaries.gpkg")
    finally:
        if src_gpkg.exists():
            src_gpkg.unlink()
    print(f"Wrote {zip_path.name} ({zip_path.stat().st_size / 1_000_000:.2f} MB)", flush=True)
    return zip_path


def _write_gz_release_asset(path: Path, frame: gpd.GeoDataFrame) -> Path:
    size = write_geojson(frame, path, None)
    print(f"Wrote {path.name} ({size / 1_000_000:.2f} MB)", flush=True)
    return path


def _write_provinces_only_zip(zip_path: Path, provinces_full: gpd.GeoDataFrame) -> Path:
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        for idx in range(len(provinces_full)):
            prov_row_df = provinces_full.iloc[[idx]]
            prov_name = str(prov_row_df.iloc[0]["WADMPR"])
            arcname = f"02-provinces/province-only/{sanitize_filename(prov_name)}.geojson"
            with zf.open(arcname, "w", force_zip64=True) as entry_fp:
                stream_geojson(prov_row_df, entry_fp, None)
    print(f"Wrote {zip_path.name} ({zip_path.stat().st_size / 1_000_000:.2f} MB)", flush=True)
    return zip_path


def _write_provinces_with_districts_zip(
    zip_path: Path,
    prov_names: list[str],
    districts_full: gpd.GeoDataFrame,
    prov_to_dist_indices: dict[str, list[int]],
) -> Path:
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        for prov_name in prov_names:
            arcname = f"02-provinces/with-districts/{sanitize_filename(prov_name)}.geojson"
            prov_districts = districts_full.iloc[prov_to_dist_indices[prov_name]]
            with zf.open(arcname, "w", force_zip64=True) as entry_fp:
                stream_geojson(prov_districts, entry_fp, None)
    print(f"Wrote {zip_path.name} ({zip_path.stat().st_size / 1_000_000:.2f} MB)", flush=True)
    return zip_path


def _write_districts_zip(zip_path: Path, districts_full: gpd.GeoDataFrame) -> Path:
    filenames = _district_filenames(districts_full)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        for idx, filename in enumerate(filenames):
            arcname = f"03-districts/{filename}"
            with zf.open(arcname, "w", force_zip64=True) as entry_fp:
                stream_geojson(districts_full.iloc[[idx]], entry_fp, None)
    print(f"Wrote {zip_path.name} ({zip_path.stat().st_size / 1_000_000:.2f} MB)", flush=True)
    return zip_path


def write_dist_release_assets(
    dist_dir: Path,
    districts_full: gpd.GeoDataFrame,
    prov_code_cols: list[str],
    workers: int,
) -> None:
    dist_dir.mkdir(parents=True, exist_ok=True)
    for stale_name in (
        "indonesia_admin_boundaries.gpkg",
        "indonesia_admin_boundaries.gpkg.zip",
        "_tmp_indonesia_admin_boundaries.gpkg",
        "indonesia_country.geojson.gz",
        "indonesia_provinces.geojson",
        "indonesia_provinces.geojson.gz",
        "indonesia_districts.geojson",
        "indonesia_districts.geojson.gz",
        "official-provinces-only.zip",
        "official-provinces-with-districts.zip",
        "official-districts.zip",
        "SHA256SUMS.txt",
    ):
        stale_path = dist_dir / stale_name
        if stale_path.exists():
            stale_path.unlink()

    print("Dissolving full-resolution provinces and national outline...", flush=True)
    provinces_full = dissolve_coverage_by(districts_full, "WADMPR", prov_code_cols, precision=None, workers=1)
    country_full_geom = clean_geometry(_safe_coverage_union(list(provinces_full["geometry"])), precision=None)
    if country_full_geom is None:
        raise ValueError("Failed to dissolve full-resolution provinces into national outline.")
    country_full = gpd.GeoDataFrame(
        [{"name": "Indonesia", "iso_a2": "ID", "iso_a3": "IDN", "geometry": country_full_geom}],
        geometry="geometry",
        crs="EPSG:4326",
    )
    del country_full_geom

    print(f"Writing official unsimplified dist/ release assets (workers={workers})...", flush=True)
    tmp_gpkg = dist_dir / "_tmp_indonesia_admin_boundaries.gpkg"
    gpkg_zip_path = dist_dir / "official_all-levels_indonesia.gpkg.zip"
    asset_paths: list[Path] = []

    def _run_batch(batch: list[tuple[Any, tuple[Any, ...]]], max_threads: int) -> None:
        if workers > 1 and len(batch) > 1:
            with ThreadPoolExecutor(max_workers=min(workers, max_threads, len(batch))) as ex:
                futures = [ex.submit(fn, *fn_args) for fn, fn_args in batch]
                for fut in futures:
                    asset_paths.append(fut.result())
        else:
            for fn, fn_args in batch:
                asset_paths.append(fn(*fn_args))

    try:
        country_full.to_file(tmp_gpkg, layer="country", driver="GPKG", engine="pyogrio")
        # Write and free full-resolution tiers stage-by-stage so country/province heaps don't stack during district zips.
        asset_paths.append(
            _write_gz_release_asset(dist_dir / "official_01-national_indonesia_merged.geojson.gz", country_full)
        )
        del country_full
        gc.collect()

        provinces_full.to_file(tmp_gpkg, layer="provinces", driver="GPKG", engine="pyogrio")
        prov_names = [str(p) for p in provinces_full["WADMPR"].tolist()]
        _run_batch(
            [
                (_write_gz_release_asset, (dist_dir / "official_02-provinces_merged.geojson.gz", provinces_full)),
                (_write_provinces_only_zip, (dist_dir / "official_02-provinces_split-by-province.zip", provinces_full)),
            ],
            max_threads=2,
        )
        del provinces_full
        gc.collect()

        districts_full.to_file(tmp_gpkg, layer="districts", driver="GPKG", engine="pyogrio")
        asset_paths.append(_zip_gpkg(tmp_gpkg, gpkg_zip_path))

        prov_to_dist_indices: dict[str, list[int]] = {}
        for pos, prov_val in enumerate(districts_full["WADMPR"].tolist()):
            prov_to_dist_indices.setdefault(str(prov_val), []).append(pos)

        _run_batch(
            [
                (_write_gz_release_asset, (dist_dir / "official_03-districts_merged.geojson.gz", districts_full)),
                (
                    _write_provinces_with_districts_zip,
                    (
                        dist_dir / "official_02-provinces_split-with-districts.zip",
                        prov_names,
                        districts_full,
                        prov_to_dist_indices,
                    ),
                ),
                (_write_districts_zip, (dist_dir / "official_03-districts_split-by-district.zip", districts_full)),
            ],
            max_threads=2,
        )
    finally:
        if tmp_gpkg.exists():
            tmp_gpkg.unlink()

    checksum_lines = [
        f"{compute_sha256(p)}  {p.name}\n"
        for p in sorted(asset_paths, key=lambda x: x.name)
    ]
    (dist_dir / "SHA256SUMS.txt").write_text("".join(checksum_lines), encoding="utf-8")
    gc.collect()


def build_derived_tiers(
    districts_detail: gpd.GeoDataFrame,
    prov_code_cols: list[str],
    simplify_tolerance: float,
    precision: int,
    workers: int,
) -> tuple[
    gpd.GeoDataFrame,
    gpd.GeoDataFrame,
    gpd.GeoDataFrame,
    gpd.GeoDataFrame,
    dict[str, Any],
]:
    provinces_detail = dissolve_coverage_by(
        districts_detail,
        "WADMPR",
        prov_code_cols,
        precision=precision,
        workers=workers,
    )

    print(
        f"Building national web-simplified coverage tier (tolerance={simplify_tolerance}, workers={workers})...",
        flush=True,
    )
    districts_simplified = districts_detail.copy()
    districts_simplified["geometry"] = simplify_coverage_geometries(
        list(districts_detail["geometry"]),
        simplify_tolerance,
        precision=precision,
        workers=workers,
    )
    provinces_simplified = dissolve_coverage_by(
        districts_simplified,
        "WADMPR",
        prov_code_cols,
        precision=precision,
        workers=workers,
    )

    country_geom = clean_geometry(_safe_coverage_union(list(provinces_simplified["geometry"])), precision=precision)
    if country_geom is None:
        raise ValueError("Failed to dissolve provinces into national outline.")
    country_simplified = gpd.GeoDataFrame(
        [{"name": "Indonesia", "iso_a2": "ID", "iso_a3": "IDN", "geometry": country_geom}],
        geometry="geometry",
        crs="EPSG:4326",
    )

    holes_by_prov = {
        str(row["WADMPR"]): h
        for _, row in provinces_detail.iterrows()
        if (h := count_interior_holes(row["geometry"])) > 0
    }
    topology = {
        "method": "coverage_clean + coverage_simplify + coverage_union_all",
        "districts_detail_coverage_valid": bool(
            len(districts_detail) < 2 or shapely.coverage_is_valid(list(districts_detail["geometry"]))
        ),
        "provinces_detail_coverage_valid": bool(
            len(provinces_detail) < 2 or shapely.coverage_is_valid(list(provinces_detail["geometry"]))
        ),
        "districts_simplified_coverage_valid": bool(
            len(districts_simplified) < 2 or shapely.coverage_is_valid(list(districts_simplified["geometry"]))
        ),
        "provinces_simplified_coverage_valid": bool(
            len(provinces_simplified) < 2 or shapely.coverage_is_valid(list(provinces_simplified["geometry"]))
        ),
        "country_interior_holes": count_interior_holes(country_geom),
        "province_interior_holes_total": sum(holes_by_prov.values()),
        "province_interior_holes_by_province": holes_by_prov,
    }
    return country_simplified, provinces_simplified, districts_simplified, provinces_detail, topology


def write_processed_outputs(
    output_dir: Path,
    precision: int,
    country_simplified: gpd.GeoDataFrame,
    provinces_simplified: gpd.GeoDataFrame,
    districts_simplified: gpd.GeoDataFrame,
    provinces_detail: gpd.GeoDataFrame,
    districts_detail: gpd.GeoDataFrame,
) -> dict[str, dict[str, int]]:
    simp_nat_dir = output_dir / "simplified" / "01-national"
    simp_prov_dir = output_dir / "simplified" / "02-provinces"
    simp_dist_dir = output_dir / "simplified" / "03-districts"
    prov_only_dir = output_dir / "high-detail" / "02-provinces" / "province-only"
    with_dist_dir = output_dir / "high-detail" / "02-provinces" / "with-districts"
    dist_split_dir = output_dir / "high-detail" / "03-districts"

    for d in (simp_nat_dir, simp_prov_dir, simp_dist_dir, prov_only_dir, with_dist_dir, dist_split_dir):
        _clear_geojson_dir(d)

    print("Writing processed-data/ GeoJSON files...", flush=True)
    write_geojson(country_simplified, simp_nat_dir / "indonesia.geojson", precision)
    write_geojson(provinces_simplified, simp_prov_dir / "indonesia_provinces.geojson", precision)
    write_geojson(districts_simplified, simp_dist_dir / "indonesia_districts.geojson", precision)

    for idx in range(len(provinces_detail)):
        prov_row_df = provinces_detail.iloc[[idx]]
        prov_name = str(prov_row_df.iloc[0]["WADMPR"])
        prov_filename = f"{sanitize_filename(prov_name)}.geojson"
        write_geojson(prov_row_df, prov_only_dir / prov_filename, precision)

        prov_districts = districts_detail.loc[districts_detail["WADMPR"] == prov_name]
        write_geojson(prov_districts, with_dist_dir / prov_filename, precision)

    dist_filenames = _district_filenames(districts_detail)
    for idx, filename in enumerate(dist_filenames):
        write_geojson(districts_detail.iloc[[idx]], dist_split_dir / filename, precision)
    gc.collect()

    return {
        "simplified/01-national": _dir_stats(simp_nat_dir),
        "simplified/02-provinces": _dir_stats(simp_prov_dir),
        "simplified/03-districts": _dir_stats(simp_dist_dir),
        "high-detail/02-provinces/province-only": _dir_stats(prov_only_dir),
        "high-detail/02-provinces/with-districts": _dir_stats(with_dist_dir),
        "high-detail/03-districts": _dir_stats(dist_split_dir),
    }


def _derive_catalog_version(edition_date: str | None) -> str:
    if isinstance(edition_date, str):
        stripped = edition_date.strip()
        if len(stripped) >= 7 and re.match(r"^\d{4}-\d{2}", stripped):
            return f"v{stripped[:7].replace('-', '.')}"
    return "v0.0"


def _round_bbox(geom: Any, precision: int) -> list[float]:
    minx, miny, maxx, maxy = geom.bounds
    return [round(float(val), precision) + 0.0 for val in (minx, miny, maxx, maxy)]


def build_catalog(
    provinces_detail: gpd.GeoDataFrame,
    districts_detail: gpd.GeoDataFrame,
    source_meta: dict[str, Any],
    version: str | None = None,
    precision: int = 6,
    output_dir: Path = Path("processed-data"),
) -> dict[str, Any]:
    """Build machine-readable catalog dictionary from processed province and district GeoDataFrames."""
    source_info = source_meta.get("source") if isinstance(source_meta.get("source"), dict) else source_meta
    edition = source_info.get("edition")
    edition_date = source_info.get("edition_date")
    resolved_version = version or _derive_catalog_version(edition_date)
    out_prefix = output_dir.as_posix().rstrip("/")

    provinces_sorted = provinces_detail.sort_values(["WADMPR"]).reset_index(drop=True)
    districts_sorted = districts_detail.sort_values(["WADMPR", "WADMKK"]).reset_index(drop=True)
    dist_counts_map = districts_sorted["WADMPR"].astype(str).value_counts().to_dict()

    provinces_entries: list[dict[str, Any]] = []
    for _, row in provinces_sorted.iterrows():
        prov_name = str(row["WADMPR"])
        prov_filename = f"{sanitize_filename(prov_name)}.geojson"
        provinces_entries.append(
            {
                "WADMPR": prov_name,
                "KDPPUM": _json_scalar(row["KDPPUM"]) if "KDPPUM" in provinces_sorted.columns else None,
                "KDPBPS": _json_scalar(row["KDPBPS"]) if "KDPBPS" in provinces_sorted.columns else None,
                "district_count": int(dist_counts_map.get(prov_name, 0)),
                "bbox": _round_bbox(row["geometry"], precision),
                "province_only_path": f"{out_prefix}/high-detail/02-provinces/province-only/{prov_filename}",
                "with_districts_path": f"{out_prefix}/high-detail/02-provinces/with-districts/{prov_filename}",
                "official_province_only_member": f"02-provinces/province-only/{prov_filename}",
                "official_with_districts_member": f"02-provinces/with-districts/{prov_filename}",
            }
        )

    dist_filenames = _district_filenames(districts_sorted)
    districts_entries: list[dict[str, Any]] = []
    for idx, dist_filename in enumerate(dist_filenames):
        row = districts_sorted.iloc[idx]
        districts_entries.append(
            {
                "WADMPR": str(row["WADMPR"]),
                "WADMKK": str(row["WADMKK"]),
                "KDPPUM": _json_scalar(row["KDPPUM"]) if "KDPPUM" in districts_sorted.columns else None,
                "KDPKAB": _json_scalar(row["KDPKAB"]) if "KDPKAB" in districts_sorted.columns else None,
                "KDPBPS": _json_scalar(row["KDPBPS"]) if "KDPBPS" in districts_sorted.columns else None,
                "KDBBPS": _json_scalar(row["KDBBPS"]) if "KDBBPS" in districts_sorted.columns else None,
                "bbox": _round_bbox(row["geometry"], precision),
                "high_detail_path": f"{out_prefix}/high-detail/03-districts/{dist_filename}",
                "official_zip_member": f"03-districts/{dist_filename}",
            }
        )

    return {
        "version": resolved_version,
        "edition": edition,
        "edition_date": edition_date,
        "crs": "EPSG:4326",
        "simplified": {
            "country": f"{out_prefix}/simplified/01-national/indonesia.geojson",
            "provinces": f"{out_prefix}/simplified/02-provinces/indonesia_provinces.geojson",
            "districts": f"{out_prefix}/simplified/03-districts/indonesia_districts.geojson",
        },
        "provinces": provinces_entries,
        "districts": districts_entries,
    }


def write_catalog(catalog: dict[str, Any], path: Path) -> None:
    """Write catalog manifest as UTF-8 LF-terminated JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(catalog, f, indent=2, ensure_ascii=False)
        f.write("\n")


def _load_single_feature_dir(directory: Path) -> gpd.GeoDataFrame:
    files = sorted(directory.glob("*.geojson"))
    if not files:
        raise FileNotFoundError(f"No GeoJSON files found in {directory}")
    rows: list[dict[str, Any]] = []
    for path in files:
        data = json.loads(path.read_text(encoding="utf-8"))
        features = data.get("features")
        if not isinstance(features, list) or len(features) != 1:
            raise ValueError(f"Expected 1 feature in {path}, found {len(features) if isinstance(features, list) else 0}.")
        feat = features[0]
        props = dict(feat.get("properties") or {})
        props["geometry"] = shapely.geometry.shape(feat["geometry"])
        rows.append(props)
    return gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326")


def build_catalog_from_processed(args: argparse.Namespace) -> int:
    """Generate catalog JSON directly from existing processed-data/ and metadata/source.json."""
    if not args.metadata_out.is_file():
        raise FileNotFoundError(f"Metadata file does not exist: {args.metadata_out}")

    simplified_paths = (
        args.output_dir / "simplified" / "01-national" / "indonesia.geojson",
        args.output_dir / "simplified" / "02-provinces" / "indonesia_provinces.geojson",
        args.output_dir / "simplified" / "03-districts" / "indonesia_districts.geojson",
    )
    for simp_path in simplified_paths:
        if not simp_path.is_file():
            raise FileNotFoundError(f"Required simplified GeoJSON missing: {simp_path}")

    prov_only_dir = args.output_dir / "high-detail" / "02-provinces" / "province-only"
    with_dist_dir = args.output_dir / "high-detail" / "02-provinces" / "with-districts"
    dist_split_dir = args.output_dir / "high-detail" / "03-districts"
    for d in (prov_only_dir, with_dist_dir, dist_split_dir):
        if not d.is_dir():
            raise FileNotFoundError(f"Required processed directory missing: {d}")

    meta = json.loads(args.metadata_out.read_text(encoding="utf-8"))
    source_info = dict(meta.get("source") if isinstance(meta.get("source"), dict) else meta)

    provinces_detail = _load_single_feature_dir(prov_only_dir)
    districts_detail = _load_single_feature_dir(dist_split_dir)

    version = _derive_catalog_version(source_info.get("edition_date"))
    catalog = build_catalog(
        provinces_detail=provinces_detail,
        districts_detail=districts_detail,
        source_meta=source_info,
        version=version,
        precision=args.precision,
        output_dir=args.output_dir,
    )
    write_catalog(catalog, args.catalog_out)
    print(
        f"Catalog written to {args.catalog_out}: "
        f"{len(catalog['provinces'])} provinces, {len(catalog['districts'])} districts.",
        flush=True,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.build_catalog_only:
        return build_catalog_from_processed(args)

    input_path = args.input
    metadata_xml_path = args.metadata_xml
    source_url = args.download_url if args.download else None

    if args.download:
        if input_path is None:
            input_path = download_file(
                args.download_url,
                args.raw_dir,
                "big_boundaries.zip",
                force=args.force_download,
            )
        if metadata_xml_path is None and args.metadata_url:
            metadata_xml_path = download_file(
                args.metadata_url,
                args.raw_dir,
                "big_metadata.xml",
                force=args.force_download,
            )

    if input_path is None or not input_path.exists():
        raise FileNotFoundError(f"Input path does not exist: {input_path}")

    source_sha256 = compute_sha256(input_path)
    xml_meta = parse_iso_metadata(metadata_xml_path) if metadata_xml_path and metadata_xml_path.exists() else {}

    with tempfile.TemporaryDirectory(prefix="big_ingest_") as tmp:
        if input_path.is_file() and input_path.suffix.lower() == ".zip":
            dataset_path = extract_zip_safely(input_path, Path(tmp))
        else:
            dataset_path = input_path

        layer_name = select_layer(dataset_path, args.layer)
        print(f"Reading layer '{layer_name}' from {dataset_path}...", flush=True)
        districts_full, raw_counts = load_full_districts(dataset_path, layer_name)

    prov_code_cols = [c for c in PROVINCE_CODE_COLS if c in districts_full.columns]
    if not args.skip_dist:
        write_dist_release_assets(args.dist_dir, districts_full, prov_code_cols, args.workers)

    print(
        f"Building high-detail coverage tier (tolerance={args.detail_simplify_tolerance}, workers={args.workers})...",
        flush=True,
    )
    districts_detail = districts_full.copy()
    districts_detail["geometry"] = simplify_coverage_geometries(
        list(districts_full["geometry"]),
        args.detail_simplify_tolerance,
        precision=args.precision,
        workers=args.workers,
    )
    del districts_full
    gc.collect()

    (
        country_simplified,
        provinces_simplified,
        districts_simplified,
        provinces_detail,
        topology,
    ) = build_derived_tiers(
        districts_detail,
        prov_code_cols,
        args.simplify_tolerance,
        args.precision,
        args.workers,
    )

    counts = {
        **raw_counts,
        "provinces": int(len(provinces_detail)),
        "districts": int(len(districts_detail)),
    }

    outputs = write_processed_outputs(
        output_dir=args.output_dir,
        precision=args.precision,
        country_simplified=country_simplified,
        provinces_simplified=provinces_simplified,
        districts_simplified=districts_simplified,
        provinces_detail=provinces_detail,
        districts_detail=districts_detail,
    )

    provenance = {
        "source": {
            "provider": "Badan Informasi Geospasial (BIG)",
            "portal_urls": ["https://tanahair.indonesia.go.id/", "https://geoportal.big.go.id/"],
            "download_url": source_url,
            "archive_name": input_path.name,
            "archive_sha256": source_sha256,
            "layer": layer_name,
            "metadata_id": xml_meta.get("metadata_id"),
            "title": xml_meta.get("title"),
            "edition": args.edition or xml_meta.get("edition"),
            "edition_date": xml_meta.get("edition_date"),
            "disclaimer": xml_meta.get("use_limitation") or DEFAULT_DISCLAIMER,
        },
        "processing": {
            "generated_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "crs": "EPSG:4326",
            "geometry_dimensions": 2,
            "coordinate_precision": args.precision,
            "detail_simplify_tolerance": args.detail_simplify_tolerance,
            "national_simplify_tolerance": args.simplify_tolerance,
            "workers": args.workers,
        },
        "counts": counts,
        "topology": topology,
        "outputs": outputs,
    }

    args.metadata_out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.metadata_out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(provenance, f, indent=2, ensure_ascii=False)
        f.write("\n")

    version = _derive_catalog_version(provenance["source"].get("edition_date"))
    catalog = build_catalog(
        provinces_detail=provinces_detail,
        districts_detail=districts_detail,
        source_meta=provenance["source"],
        version=version,
        precision=args.precision,
        output_dir=args.output_dir,
    )
    write_catalog(catalog, args.catalog_out)

    print(
        f"Ingest complete: {counts['provinces']} provinces, {counts['districts']} districts "
        f"(dropped null WADMKK: {counts['dropped_null_wadmkk']}).",
        flush=True,
    )
    print(f"Metadata written to {args.metadata_out}", flush=True)
    print(f"Catalog written to {args.catalog_out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
