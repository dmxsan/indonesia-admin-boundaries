#!/usr/bin/env python3
"""Ingest administrative boundaries from BIG Geoportal into processed GeoJSON & release assets."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tempfile
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import geopandas as gpd
import pandas as pd
import pyogrio
import shapely
from shapely.geometry import MultiPolygon, mapping
from shapely.ops import unary_union

DEFAULT_BIG_URL = "https://drive.big.go.id/s/9x9dJWSNfQ3wp7S/download"
DEFAULT_METADATA_URL = "https://drive.big.go.id/s/JyaLm2j8HSz3sEC/download"
DEFAULT_DISCLAIMER = "Data batas yang berstatus indikatif tidak dapat dijadikan referensi hukum."
PROVINCE_CODE_COLS = ["KDPPUM", "KDPBPS"]
DISTRICT_CODE_COLS = ["KDPPUM", "KDPKAB", "KDPBPS", "KDBBPS"]
NULL_TOKENS = {"", "none", "null", "nan"}


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
    parser.add_argument("--precision", type=int, default=6, help="GeoJSON coordinate decimal places.")
    parser.add_argument(
        "--simplify-tolerance",
        type=float,
        default=0.001,
        help="Topology-preserving simplification tolerance (degrees) for 01-national/ outputs.",
    )
    parser.add_argument("--edition", default=None, help="Optional edition label override.")

    args = parser.parse_args(argv)
    if args.input is None and not args.download:
        parser.error("Provide --input PATH or --download.")
    if args.precision < 0:
        parser.error("--precision must be >= 0.")
    if args.simplify_tolerance < 0:
        parser.error("--simplify-tolerance must be >= 0.")
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
        pass

    if filename:
        dest_path = dest_dir / filename
        if dest_path.exists() and not force:
            print(f"Using existing download: {dest_path}")
            return dest_path

    get_req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(get_req, timeout=120) as resp:
        resolved_name = filename or _filename_from_cd(resp.headers.get("Content-Disposition")) or fallback_name
        dest_path = dest_dir / Path(resolved_name).name
        if dest_path.exists() and not force:
            print(f"Using existing download: {dest_path}")
            return dest_path

        part_path = dest_path.with_suffix(dest_path.suffix + ".part")
        total = 0
        with open(part_path, "wb") as out:
            while chunk := resp.read(1024 * 1024):
                out.write(chunk)
                total += len(chunk)
        part_path.replace(dest_path)
        print(f"Downloaded {dest_path} ({total / 1_000_000:.2f} MB)")
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


def clean_geometry(geom: Any) -> Any:
    if geom is None or geom.is_empty:
        return None
    geom = shapely.force_2d(geom)
    if not geom.is_valid:
        geom = shapely.make_valid(geom)
    geom = _extract_polygonal(geom)
    if geom is None or geom.is_empty:
        return None
    if not geom.is_valid:
        geom = _extract_polygonal(shapely.make_valid(geom))
        if geom is None or geom.is_empty:
            return None
    if hasattr(shapely, "orient_polygons"):
        geom = shapely.orient_polygons(geom, exterior_cw=False)
    return geom


def simplify_polygonal(geom: Any, tolerance: float) -> Any:
    if tolerance <= 0:
        return geom
    simplified = clean_geometry(geom.simplify(tolerance, preserve_topology=True))
    if simplified is None or simplified.is_empty:
        return geom
    return simplified


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


def _round_coords_2d(coords: Any, precision: int) -> Any:
    if isinstance(coords, (list, tuple)):
        if coords and isinstance(coords[0], (int, float)):
            return [round(float(coords[0]), precision), round(float(coords[1]), precision)]
        return [_round_coords_2d(item, precision) for item in coords]
    return coords


def _json_scalar(val: Any) -> Any:
    if val is None or pd.isna(val):
        return None
    if hasattr(val, "item"):
        return val.item()
    return val


def write_geojson(gdf: gpd.GeoDataFrame, path: Path, precision: int) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    prop_cols = [c for c in gdf.columns if c != gdf.geometry.name]
    features: list[dict[str, Any]] = []
    for _, row in gdf.iterrows():
        geom_obj = row[gdf.geometry.name]
        geom_dict = mapping(geom_obj)
        geom_dict["coordinates"] = _round_coords_2d(geom_dict["coordinates"], precision)
        props = {col: _json_scalar(row[col]) for col in prop_cols}
        features.append({"type": "Feature", "properties": props, "geometry": geom_dict})

    payload = {"type": "FeatureCollection", "features": features}
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, separators=(",", ":"), ensure_ascii=False)
        f.write("\n")
    return path.stat().st_size


def _clear_geojson_dir(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for old_file in directory.glob("*.geojson"):
        old_file.unlink()


def _dir_stats(directory: Path) -> dict[str, int]:
    files = list(directory.glob("*.geojson"))
    sizes = [f.stat().st_size for f in files]
    return {"file_count": len(files), "max_file_bytes": max(sizes) if sizes else 0}


def build_datasets(
    dataset_path: Path,
    layer_name: str,
    simplify_tolerance: float,
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame, gpd.GeoDataFrame, gpd.GeoDataFrame, gpd.GeoDataFrame, dict[str, Any]]:
    gdf = gpd.read_file(dataset_path, layer=layer_name, engine="pyogrio")
    raw_feature_count = len(gdf)

    if gdf.crs is None:
        print("Warning: dataset has no CRS; assuming EPSG:4326.", file=sys.stderr)
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

    gdf["geometry"] = gdf["geometry"].map(clean_geometry)
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

    dist_code_cols = [c for c in DISTRICT_CODE_COLS if c in valid_gdf.columns]
    dist_cols = ["WADMPR", "WADMKK", *dist_code_cols, "geometry"]
    if dist_code_cols:
        districts_gdf = valid_gdf[dist_cols].dissolve(
            by=["WADMPR", "WADMKK"],
            aggfunc={c: first_non_null for c in dist_code_cols},
            as_index=False,
        )
    else:
        districts_gdf = valid_gdf[dist_cols].dissolve(by=["WADMPR", "WADMKK"], as_index=False)

    districts_gdf["geometry"] = districts_gdf["geometry"].map(clean_geometry)
    districts_gdf = districts_gdf.loc[~districts_gdf["geometry"].isna()].copy()
    districts_gdf = districts_gdf.set_crs(epsg=4326, allow_override=True)
    districts_gdf = districts_gdf.sort_values(["WADMPR", "WADMKK"]).reset_index(drop=True)

    prov_code_cols = [c for c in PROVINCE_CODE_COLS if c in districts_gdf.columns]
    prov_cols = ["WADMPR", *prov_code_cols, "geometry"]
    if prov_code_cols:
        provinces_gdf = districts_gdf[prov_cols].dissolve(
            by=["WADMPR"],
            aggfunc={c: first_non_null for c in prov_code_cols},
            as_index=False,
        )
    else:
        provinces_gdf = districts_gdf[prov_cols].dissolve(by=["WADMPR"], as_index=False)

    provinces_gdf["geometry"] = provinces_gdf["geometry"].map(clean_geometry)
    provinces_gdf = provinces_gdf.loc[~provinces_gdf["geometry"].isna()].copy()
    provinces_gdf = provinces_gdf.set_crs(epsg=4326, allow_override=True)
    provinces_gdf = provinces_gdf.sort_values(["WADMPR"]).reset_index(drop=True)

    provinces_simplified = provinces_gdf.copy()
    provinces_simplified["geometry"] = provinces_simplified["geometry"].map(
        lambda g: simplify_polygonal(g, simplify_tolerance)
    )

    districts_simplified = districts_gdf.copy()
    districts_simplified["geometry"] = districts_simplified["geometry"].map(
        lambda g: simplify_polygonal(g, simplify_tolerance)
    )

    country_geom = clean_geometry(unary_union(list(provinces_simplified["geometry"])))
    if country_geom is None:
        raise ValueError("Failed to dissolve provinces into national outline.")
    country_simplified = gpd.GeoDataFrame(
        [{"name": "Indonesia", "iso_a2": "ID", "iso_a3": "IDN", "geometry": country_geom}],
        geometry="geometry",
        crs="EPSG:4326",
    )

    counts = {
        "raw_features": int(raw_feature_count),
        "dropped_empty_geometries": dropped_empty_geometries,
        "dropped_null_wadmpr": dropped_null_wadmpr,
        "dropped_null_wadmkk": dropped_null_wadmkk,
        "dropped_null_wadmkk_by_province": dropped_null_wadmkk_by_province,
        "provinces": int(len(provinces_gdf)),
        "districts": int(len(districts_gdf)),
    }
    return (
        country_simplified,
        provinces_simplified,
        districts_simplified,
        provinces_gdf,
        districts_gdf,
        counts,
    )


def write_all_outputs(
    output_dir: Path,
    dist_dir: Path,
    skip_dist: bool,
    precision: int,
    country_simplified: gpd.GeoDataFrame,
    provinces_simplified: gpd.GeoDataFrame,
    districts_simplified: gpd.GeoDataFrame,
    provinces_gdf: gpd.GeoDataFrame,
    districts_gdf: gpd.GeoDataFrame,
) -> dict[str, dict[str, int]]:
    nat_dir = output_dir / "01-national"
    prov_only_dir = output_dir / "02-provinces" / "province-only"
    with_dist_dir = output_dir / "02-provinces" / "with-districts"
    dist_split_dir = output_dir / "03-districts"

    for d in (nat_dir, prov_only_dir, with_dist_dir, dist_split_dir):
        _clear_geojson_dir(d)

    write_geojson(country_simplified, nat_dir / "indonesia_country_simplified.geojson", precision)
    write_geojson(provinces_simplified, nat_dir / "indonesia_provinces_simplified.geojson", precision)
    write_geojson(districts_simplified, nat_dir / "indonesia_districts_simplified.geojson", precision)

    for idx in range(len(provinces_gdf)):
        prov_row_df = provinces_gdf.iloc[[idx]]
        prov_name = str(prov_row_df.iloc[0]["WADMPR"])
        prov_filename = f"{sanitize_filename(prov_name)}.geojson"
        write_geojson(prov_row_df, prov_only_dir / prov_filename, precision)

        prov_districts = districts_gdf.loc[districts_gdf["WADMPR"] == prov_name]
        write_geojson(prov_districts, with_dist_dir / prov_filename, precision)

    base_names = [sanitize_filename(str(v)) for v in districts_gdf["WADMKK"]]
    name_counts = pd.Series(base_names).value_counts().to_dict()
    for idx in range(len(districts_gdf)):
        dist_row_df = districts_gdf.iloc[[idx]]
        prov_name = str(dist_row_df.iloc[0]["WADMPR"])
        base = base_names[idx]
        if name_counts.get(base, 0) > 1:
            filename = f"{base}__{sanitize_filename(prov_name)}.geojson"
        else:
            filename = f"{base}.geojson"
        write_geojson(dist_row_df, dist_split_dir / filename, precision)

    if not skip_dist:
        dist_dir.mkdir(parents=True, exist_ok=True)
        write_geojson(provinces_gdf, dist_dir / "indonesia_provinces.geojson", precision)
        write_geojson(districts_gdf, dist_dir / "indonesia_districts.geojson", precision)
        gpkg_path = dist_dir / "indonesia_admin_boundaries.gpkg"
        if gpkg_path.exists():
            gpkg_path.unlink()
        provinces_gdf.to_file(gpkg_path, layer="provinces", driver="GPKG", engine="pyogrio")
        districts_gdf.to_file(gpkg_path, layer="districts", driver="GPKG", engine="pyogrio")

    return {
        "01-national": _dir_stats(nat_dir),
        "02-provinces/province-only": _dir_stats(prov_only_dir),
        "02-provinces/with-districts": _dir_stats(with_dist_dir),
        "03-districts": _dir_stats(dist_split_dir),
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

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
        print(f"Reading layer '{layer_name}' from {dataset_path}...")
        (
            country_simplified,
            provinces_simplified,
            districts_simplified,
            provinces_gdf,
            districts_gdf,
            counts,
        ) = build_datasets(dataset_path, layer_name, args.simplify_tolerance)

    outputs = write_all_outputs(
        output_dir=args.output_dir,
        dist_dir=args.dist_dir,
        skip_dist=args.skip_dist,
        precision=args.precision,
        country_simplified=country_simplified,
        provinces_simplified=provinces_simplified,
        districts_simplified=districts_simplified,
        provinces_gdf=provinces_gdf,
        districts_gdf=districts_gdf,
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
            "simplify_tolerance": args.simplify_tolerance,
        },
        "counts": counts,
        "outputs": outputs,
    }

    args.metadata_out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.metadata_out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(provenance, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(
        f"Ingest complete: {counts['provinces']} provinces, {counts['districts']} districts "
        f"(dropped null WADMKK: {counts['dropped_null_wadmkk']})."
    )
    print(f"Metadata written to {args.metadata_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
