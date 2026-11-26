#!/usr/bin/env python3
"""Validate generated GeoJSON boundaries, topological coverage, and provenance metadata."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

import shapely
from shapely.geometry import shape
from shapely.validation import explain_validity

FORBIDDEN_FILENAMES = {"none.geojson", "null.geojson", "nan.geojson"}
SIMPLIFIED_FILES = {
    "country": ("simplified/01-national", "indonesia.geojson"),
    "provinces": ("simplified/02-provinces", "indonesia_provinces.geojson"),
    "districts": ("simplified/03-districts", "indonesia_districts.geojson"),
}
LEGACY_TOP_DIRS = ("01-national", "02-provinces", "03-districts")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate processed GeoJSON boundary outputs, coverage topology, and provenance metadata."
    )
    parser.add_argument("--data-dir", type=Path, default=Path("processed-data"), help="Processed GeoJSON root.")
    parser.add_argument(
        "--metadata",
        type=Path,
        default=Path("metadata/source.json"),
        help="Provenance metadata JSON path.",
    )
    parser.add_argument("--no-metadata", action="store_true", help="Skip metadata/source.json cross-check.")
    parser.add_argument(
        "--catalog",
        type=Path,
        default=None,
        help="Catalog manifest JSON path (defaults to metadata/catalog.json unless --no-metadata or --no-catalog is set).",
    )
    parser.add_argument("--no-catalog", action="store_true", help="Skip metadata/catalog.json validation.")
    parser.add_argument(
        "--expected-provinces",
        type=int,
        default=38,
        help="Expected province count (0 disables exact count check).",
    )
    parser.add_argument(
        "--expected-districts",
        type=int,
        default=514,
        help="Expected district count (0 disables exact count check).",
    )
    parser.add_argument(
        "--max-file-mb",
        type=float,
        default=45.0,
        help="Maximum allowed GeoJSON file size in decimal MB (1,000,000 bytes).",
    )
    args = parser.parse_args(argv)
    if args.no_catalog and args.catalog is not None:
        parser.error("--catalog cannot be combined with --no-catalog.")
    return args


def _check_coords_2d_wgs84(coords: Any, path_label: str, errors: list[str]) -> bool:
    stack = [coords]
    while stack:
        item = stack.pop()
        if not isinstance(item, list):
            errors.append(f"{path_label}: coordinate structure is not a list.")
            return False
        if not item:
            errors.append(f"{path_label}: empty coordinate array.")
            return False
        first = item[0]
        if isinstance(first, (int, float)) and not isinstance(first, bool):
            if len(item) != 2:
                errors.append(f"{path_label}: expected 2D coordinates, got length {len(item)}.")
                return False
            lon, lat = item[0], item[1]
            if not (isinstance(lon, (int, float)) and not isinstance(lon, bool)):
                errors.append(f"{path_label}: invalid longitude {lon!r}.")
                return False
            if not (isinstance(lat, (int, float)) and not isinstance(lat, bool)):
                errors.append(f"{path_label}: invalid latitude {lat!r}.")
                return False
            if not (-180.0 <= float(lon) <= 180.0 and -90.0 <= float(lat) <= 90.0):
                errors.append(f"{path_label}: coordinates out of WGS84 bounds: [{lon}, {lat}].")
                return False
        else:
            stack.extend(item)
    return True


def _non_empty_str(val: Any) -> bool:
    return isinstance(val, str) and bool(val.strip())


def count_interior_holes(geom: Any) -> int:
    if geom is None or geom.is_empty:
        return 0
    if geom.geom_type == "Polygon":
        return len(geom.interiors)
    if geom.geom_type == "MultiPolygon":
        return sum(len(poly.interiors) for poly in geom.geoms)
    return 0


def check_coverage(geoms: list[Any], labels: list[str], context: str, errors: list[str]) -> None:
    if len(geoms) < 2:
        return
    if not shapely.coverage_is_valid(geoms):
        inv_edges = shapely.coverage_invalid_edges(geoms)
        bad_labels = [
            labels[i]
            for i, edge in enumerate(inv_edges)
            if edge is not None and not edge.is_empty
        ]
        errors.append(
            f"{context}: invalid topological coverage ({len(bad_labels)} offending feature(s), "
            f"sample: {bad_labels[:10]})."
        )


def validate_geojson_file(
    path: Path,
    rel_label: str,
    max_bytes: int,
    errors: list[str],
) -> tuple[list[dict[str, Any]], list[Any], int]:
    size = path.stat().st_size
    if size > max_bytes:
        errors.append(f"{rel_label}: size {size / 1_000_000:.2f} MB exceeds {max_bytes / 1_000_000:.2f} MB limit.")

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        errors.append(f"{rel_label}: invalid JSON ({exc}).")
        return [], [], size

    if not isinstance(data, dict) or data.get("type") != "FeatureCollection":
        errors.append(f"{rel_label}: top-level object must be a GeoJSON FeatureCollection.")
        return [], [], size
    if "crs" in data:
        errors.append(f"{rel_label}: legacy top-level 'crs' key is forbidden under RFC 7946.")

    features = data.get("features")
    if not isinstance(features, list) or not features:
        errors.append(f"{rel_label}: 'features' must be a non-empty list.")
        return [], [], size

    valid_geoms: list[Any] = []
    for idx, feat in enumerate(features):
        feat_label = f"{rel_label} feature #{idx}"
        if not isinstance(feat, dict):
            errors.append(f"{feat_label}: feature is not an object.")
            continue
        props = feat.get("properties")
        if not isinstance(props, dict):
            errors.append(f"{feat_label}: missing 'properties' object.")
        geom_raw = feat.get("geometry")
        if not isinstance(geom_raw, dict):
            errors.append(f"{feat_label}: missing 'geometry' object.")
            continue

        _check_coords_2d_wgs84(geom_raw.get("coordinates"), feat_label, errors)

        try:
            geom = shape(geom_raw)
        except Exception as exc:
            errors.append(f"{feat_label}: failed to parse geometry ({exc}).")
            continue

        if geom.geom_type not in {"Polygon", "MultiPolygon"}:
            errors.append(f"{feat_label}: expected Polygon/MultiPolygon, got {geom.geom_type}.")
        elif geom.is_empty:
            errors.append(f"{feat_label}: geometry is empty.")
        elif not geom.is_valid:
            errors.append(f"{feat_label}: invalid geometry ({explain_validity(geom)}).")
        else:
            valid_geoms.append(geom)

    return features, valid_geoms, size


def _resolve_catalog_path(
    raw_path: Any,
    data_dir: Path,
    expected_parent: Path,
    label: str,
    errors: list[str],
) -> Path | None:
    if not _non_empty_str(raw_path):
        errors.append(f"{label}: missing/empty path.")
        return None
    raw_str = str(raw_path)
    candidates: list[Path] = []
    if data_dir != Path("processed-data") and raw_str.startswith("processed-data/"):
        candidates.append(data_dir / raw_str.removeprefix("processed-data/"))
    candidates.append(Path(raw_str))
    resolved = next((cand for cand in candidates if cand.is_file()), None)
    if resolved is None:
        errors.append(f"{label}: referenced file does not exist ({raw_str}).")
        return None
    if resolved.resolve().parent != expected_parent.resolve():
        errors.append(f"{label}: path {raw_str} is outside expected directory {expected_parent}.")
        return None
    return resolved


def _validate_wgs84_bbox(bbox: Any, label: str, errors: list[str]) -> None:
    if not isinstance(bbox, list) or len(bbox) != 4:
        errors.append(f"{label}: bbox must be a 4-element list, got {bbox!r}.")
        return
    nums: list[float] = []
    for coord in bbox:
        if not isinstance(coord, (int, float)) or isinstance(coord, bool) or not math.isfinite(float(coord)):
            errors.append(f"{label}: bbox contains invalid coordinate {coord!r}.")
            return
        nums.append(float(coord))
    minx, miny, maxx, maxy = nums
    if not (-180.0 <= minx <= maxx <= 180.0 and -90.0 <= miny <= maxy <= 90.0):
        errors.append(f"{label}: bbox out of WGS84 bounds or inverted: {bbox!r}.")


def _validate_catalog_provinces(
    provinces: Any,
    data_dir: Path,
    prov_count: int,
    expected_provinces: int,
    prov_only_props: dict[str, dict[str, Any]],
    prov_only_bboxes: dict[str, list[float]],
    with_dist_props: dict[str, list[dict[str, Any]]],
    errors: list[str],
) -> tuple[dict[str, int], int]:
    prov_district_counts: dict[str, int] = {}
    sum_district_count = 0
    if not isinstance(provinces, list):
        errors.append("Catalog 'provinces' must be a list.")
        return prov_district_counts, sum_district_count

    if len(provinces) != prov_count:
        errors.append(f"Catalog provinces length={len(provinces)} does not match {prov_count}.")
    if expected_provinces > 0 and len(provinces) != expected_provinces:
        errors.append(f"Catalog provinces length={len(provinces)} does not match expected {expected_provinces}.")

    prov_only_dir = data_dir / "high-detail" / "02-provinces" / "province-only"
    with_dist_dir = data_dir / "high-detail" / "02-provinces" / "with-districts"
    seen_names: set[str] = set()
    seen_files: set[str] = set()

    for idx, entry in enumerate(provinces):
        label = f"Catalog provinces[{idx}]"
        if not isinstance(entry, dict):
            errors.append(f"{label}: entry must be an object.")
            continue
        wadmpr = entry.get("WADMPR")
        if not _non_empty_str(wadmpr):
            errors.append(f"{label}: missing/empty WADMPR.")
            continue
        label = f"Catalog provinces[{idx}] ({wadmpr})"
        if wadmpr in seen_names:
            errors.append(f"{label}: duplicate WADMPR entry.")
        seen_names.add(wadmpr)

        for key in ("KDPPUM", "KDPBPS"):
            if key not in entry:
                errors.append(f"{label}: missing required key '{key}'.")

        dc = entry.get("district_count")
        if not isinstance(dc, int) or isinstance(dc, bool) or dc < 1:
            errors.append(f"{label}: district_count must be a positive int, got {dc!r}.")
        else:
            sum_district_count += dc
            prov_district_counts[wadmpr] = dc

        _validate_wgs84_bbox(entry.get("bbox"), label, errors)

        po_resolved = _resolve_catalog_path(
            entry.get("province_only_path"), data_dir, prov_only_dir, f"{label} province_only_path", errors
        )
        wd_resolved = _resolve_catalog_path(
            entry.get("with_districts_path"), data_dir, with_dist_dir, f"{label} with_districts_path", errors
        )
        if po_resolved is not None:
            if po_resolved.name in seen_files:
                errors.append(f"{label}: duplicate province_only_path file {po_resolved.name}.")
            seen_files.add(po_resolved.name)
            expected_po_member = f"02-provinces/province-only/{po_resolved.name}"
            if entry.get("official_province_only_member") != expected_po_member:
                errors.append(
                    f"{label}: official_province_only_member={entry.get('official_province_only_member')!r} "
                    f"does not match {expected_po_member!r}."
                )
            geo_props = prov_only_props.get(po_resolved.name)
            if geo_props is not None:
                for attr in ("WADMPR", "KDPPUM", "KDPBPS"):
                    if entry.get(attr) != geo_props.get(attr):
                        errors.append(
                            f"{label}: {attr}={entry.get(attr)!r} does not match {po_resolved.name} ({geo_props.get(attr)!r})."
                        )
            actual_bbox = prov_only_bboxes.get(po_resolved.name)
            if actual_bbox is not None and entry.get("bbox") != actual_bbox:
                errors.append(f"{label}: bbox={entry.get('bbox')!r} does not match {po_resolved.name} ({actual_bbox!r}).")
        if wd_resolved is not None:
            expected_wd_member = f"02-provinces/with-districts/{wd_resolved.name}"
            if entry.get("official_with_districts_member") != expected_wd_member:
                errors.append(
                    f"{label}: official_with_districts_member={entry.get('official_with_districts_member')!r} "
                    f"does not match {expected_wd_member!r}."
                )
            wd_feats = with_dist_props.get(wd_resolved.name)
            if wd_feats is not None:
                if isinstance(dc, int) and not isinstance(dc, bool) and len(wd_feats) != dc:
                    errors.append(
                        f"{label}: district_count={dc} does not match {wd_resolved.name} feature count ({len(wd_feats)})."
                    )
                if any(feat.get("WADMPR") != wadmpr for feat in wd_feats):
                    errors.append(f"{label}: {wd_resolved.name} contains mismatched WADMPR properties.")
        if po_resolved is not None and wd_resolved is not None and po_resolved.stem != wd_resolved.stem:
            errors.append(
                f"{label}: province_only_path stem '{po_resolved.stem}' != with_districts_path stem '{wd_resolved.stem}'."
            )

    return prov_district_counts, sum_district_count


def _validate_catalog_districts(
    districts: Any,
    data_dir: Path,
    dist_count: int,
    expected_districts: int,
    dist_props: dict[str, dict[str, Any]],
    dist_bboxes: dict[str, list[float]],
    prov_district_counts: dict[str, int],
    errors: list[str],
) -> None:
    if not isinstance(districts, list):
        errors.append("Catalog 'districts' must be a list.")
        return

    if len(districts) != dist_count:
        errors.append(f"Catalog districts length={len(districts)} does not match {dist_count}.")
    if expected_districts > 0 and len(districts) != expected_districts:
        errors.append(f"Catalog districts length={len(districts)} does not match expected {expected_districts}.")

    dist_dir = data_dir / "high-detail" / "03-districts"
    seen_keys: set[tuple[str, str]] = set()
    seen_files: set[str] = set()
    dist_count_by_prov: dict[str, int] = {}

    for idx, entry in enumerate(districts):
        label = f"Catalog districts[{idx}]"
        if not isinstance(entry, dict):
            errors.append(f"{label}: entry must be an object.")
            continue
        wadmpr = entry.get("WADMPR")
        wadmkk = entry.get("WADMKK")
        if not _non_empty_str(wadmpr) or not _non_empty_str(wadmkk):
            errors.append(f"{label}: missing/empty WADMPR or WADMKK.")
            continue
        label = f"Catalog districts[{idx}] ({wadmpr}/{wadmkk})"
        key = (wadmpr, wadmkk)
        if key in seen_keys:
            errors.append(f"{label}: duplicate (WADMPR, WADMKK) entry.")
        seen_keys.add(key)
        dist_count_by_prov[wadmpr] = dist_count_by_prov.get(wadmpr, 0) + 1

        for col in ("KDPPUM", "KDPKAB", "KDPBPS", "KDBBPS"):
            if col not in entry:
                errors.append(f"{label}: missing required key '{col}'.")

        _validate_wgs84_bbox(entry.get("bbox"), label, errors)

        dist_resolved = _resolve_catalog_path(
            entry.get("high_detail_path"), data_dir, dist_dir, f"{label} high_detail_path", errors
        )
        if dist_resolved is not None:
            if dist_resolved.name in seen_files:
                errors.append(f"{label}: duplicate high_detail_path file {dist_resolved.name}.")
            seen_files.add(dist_resolved.name)
            expected_member = f"03-districts/{dist_resolved.name}"
            if entry.get("official_zip_member") != expected_member:
                errors.append(
                    f"{label}: official_zip_member={entry.get('official_zip_member')!r} "
                    f"does not match {expected_member!r}."
                )
            geo_props = dist_props.get(dist_resolved.name)
            if geo_props is not None:
                for attr in ("WADMPR", "WADMKK", "KDPPUM", "KDPKAB", "KDPBPS", "KDBBPS"):
                    if entry.get(attr) != geo_props.get(attr):
                        errors.append(
                            f"{label}: {attr}={entry.get(attr)!r} does not match {dist_resolved.name} ({geo_props.get(attr)!r})."
                        )
            actual_bbox = dist_bboxes.get(dist_resolved.name)
            if actual_bbox is not None and entry.get("bbox") != actual_bbox:
                errors.append(f"{label}: bbox={entry.get('bbox')!r} does not match {dist_resolved.name} ({actual_bbox!r}).")

    if prov_district_counts and dist_count_by_prov != prov_district_counts:
        errors.append("Catalog per-province district counts in 'districts' do not match 'provinces[*].district_count'.")


def validate_catalog(
    catalog_path: Path,
    data_dir: Path,
    prov_count: int,
    dist_count: int,
    expected_provinces: int,
    expected_districts: int,
    prov_only_props: dict[str, dict[str, Any]],
    prov_only_bboxes: dict[str, list[float]],
    with_dist_props: dict[str, list[dict[str, Any]]],
    dist_props: dict[str, dict[str, Any]],
    dist_bboxes: dict[str, list[float]],
    meta: dict[str, Any] | None,
    errors: list[str],
) -> None:
    """Validate metadata/catalog.json schema, paths, bboxes, counts, and attribute parity."""
    if not catalog_path.is_file():
        errors.append(f"Missing catalog manifest file: {catalog_path}")
        return

    try:
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    except Exception as exc:
        errors.append(f"Failed to parse catalog file {catalog_path}: {exc}")
        return

    if not isinstance(catalog, dict):
        errors.append(f"Catalog file {catalog_path}: top-level JSON must be an object.")
        return

    if not _non_empty_str(catalog.get("version")):
        errors.append(f"Catalog {catalog_path}: missing/empty 'version'.")
    if catalog.get("crs") != "EPSG:4326":
        errors.append(f"Catalog {catalog_path}: expected crs='EPSG:4326', got {catalog.get('crs')!r}.")
    for top_key in ("edition", "edition_date"):
        if top_key not in catalog:
            errors.append(f"Catalog {catalog_path}: missing '{top_key}' key.")

    if isinstance(meta, dict) and isinstance(meta.get("source"), dict):
        src = meta["source"]
        if src.get("edition") is not None and catalog.get("edition") != src.get("edition"):
            errors.append(f"Catalog edition={catalog.get('edition')!r} != metadata source.edition={src.get('edition')!r}.")
        if src.get("edition_date") is not None and catalog.get("edition_date") != src.get("edition_date"):
            errors.append(
                f"Catalog edition_date={catalog.get('edition_date')!r} != metadata source.edition_date={src.get('edition_date')!r}."
            )

    simplified = catalog.get("simplified")
    if not isinstance(simplified, dict):
        errors.append(f"Catalog {catalog_path}: 'simplified' must be an object.")
    else:
        for key, (rel_dir, filename) in SIMPLIFIED_FILES.items():
            resolved = _resolve_catalog_path(
                simplified.get(key), data_dir, data_dir / rel_dir, f"Catalog simplified.{key}", errors
            )
            if resolved is not None and resolved.name != filename:
                errors.append(f"Catalog simplified.{key}: expected filename {filename}, got {resolved.name}.")

    prov_district_counts, sum_district_count = _validate_catalog_provinces(
        catalog.get("provinces"),
        data_dir,
        prov_count,
        expected_provinces,
        prov_only_props,
        prov_only_bboxes,
        with_dist_props,
        errors,
    )
    if sum_district_count != dist_count:
        errors.append(f"Catalog sum(provinces[*].district_count)={sum_district_count} does not match {dist_count}.")

    _validate_catalog_districts(
        catalog.get("districts"),
        data_dir,
        dist_count,
        expected_districts,
        dist_props,
        dist_bboxes,
        prov_district_counts,
        errors,
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    data_dir = args.data_dir
    max_bytes = int(args.max_file_mb * 1_000_000)
    errors: list[str] = []

    simp_nat_dir = data_dir / "simplified" / "01-national"
    simp_prov_dir = data_dir / "simplified" / "02-provinces"
    simp_dist_dir = data_dir / "simplified" / "03-districts"
    prov_only_dir = data_dir / "high-detail" / "02-provinces" / "province-only"
    with_dist_dir = data_dir / "high-detail" / "02-provinces" / "with-districts"
    dist_dir = data_dir / "high-detail" / "03-districts"

    for legacy_name in LEGACY_TOP_DIRS:
        legacy_dir = data_dir / legacy_name
        if legacy_dir.exists():
            errors.append(
                f"Legacy top-level directory {legacy_dir} still exists; migrate to simplified/ and high-detail/."
            )

    for d in (simp_nat_dir, simp_prov_dir, simp_dist_dir, prov_only_dir, with_dist_dir, dist_dir):
        if not d.is_dir():
            errors.append(f"Missing required directory: {d}")

    if errors:
        for err in errors:
            print(f"ERROR: {err}", file=sys.stderr)
        return 1

    all_geojsons = sorted(data_dir.rglob("*.geojson"))
    for p in all_geojsons:
        if p.name.lower() in FORBIDDEN_FILENAMES:
            errors.append(f"Forbidden filename found: {p.relative_to(data_dir)}")

    max_seen_bytes = 0
    files_checked = 0
    country_holes = 0

    # 1. Simplified nationwide outputs (0.001 deg)
    nat_counts: dict[str, int] = {}
    for key, (rel_dir, filename) in SIMPLIFIED_FILES.items():
        rel_path = f"{rel_dir}/{filename}"
        path = data_dir / rel_dir / filename
        if not path.is_file():
            errors.append(f"Missing simplified file: {path}")
            continue
        features, geoms, size = validate_geojson_file(path, rel_path, max_bytes, errors)
        max_seen_bytes = max(max_seen_bytes, size)
        files_checked += 1
        nat_counts[key] = len(features)
        if key == "country":
            if len(features) != 1:
                errors.append(f"{rel_path}: expected 1 feature, found {len(features)}.")
            elif len(geoms) == 1:
                country_holes = count_interior_holes(geoms[0])
        elif key == "provinces":
            labels = []
            for i, feat in enumerate(features):
                pr = feat.get("properties", {}).get("WADMPR")
                if not _non_empty_str(pr):
                    errors.append(f"{rel_path} feature #{i}: missing/empty WADMPR.")
                labels.append(f"#{i} ({pr})")
            if len(geoms) == len(features):
                check_coverage(geoms, labels, rel_path, errors)
        elif key == "districts":
            labels = []
            for i, feat in enumerate(features):
                props = feat.get("properties", {})
                pr, kk = props.get("WADMPR"), props.get("WADMKK")
                if not _non_empty_str(pr) or not _non_empty_str(kk):
                    errors.append(f"{rel_path} feature #{i}: missing/empty WADMPR or WADMKK.")
                labels.append(f"#{i} ({pr}/{kk})")
            if len(geoms) == len(features):
                check_coverage(geoms, labels, rel_path, errors)

    # 2. High-detail province-only & with-districts outputs (2e-05 deg)
    prov_only_files = sorted(prov_only_dir.glob("*.geojson"))
    with_dist_files = sorted(with_dist_dir.glob("*.geojson"))
    prov_only_names = {p.name for p in prov_only_files}
    with_dist_names = {p.name for p in with_dist_files}
    if prov_only_names != with_dist_names:
        only_a = sorted(prov_only_names - with_dist_names)
        only_b = sorted(with_dist_names - prov_only_names)
        errors.append(
            f"Province filename mismatch between province-only and with-districts: only_in_province_only={only_a}, only_in_with_districts={only_b}"
        )

    prov_only_geoms: list[Any] = []
    prov_only_labels: list[str] = []
    prov_only_props: dict[str, dict[str, Any]] = {}
    prov_only_bboxes: dict[str, list[float]] = {}
    prov_holes_total = 0
    for path in prov_only_files:
        features, geoms, size = validate_geojson_file(
            path, f"high-detail/02-provinces/province-only/{path.name}", max_bytes, errors
        )
        max_seen_bytes = max(max_seen_bytes, size)
        files_checked += 1
        if len(features) != 1:
            errors.append(
                f"high-detail/02-provinces/province-only/{path.name}: expected 1 feature, found {len(features)}."
            )
        else:
            props = features[0].get("properties", {})
            if isinstance(props, dict):
                prov_only_props[path.name] = props
            pr = props.get("WADMPR") if isinstance(props, dict) else None
            if not _non_empty_str(pr):
                errors.append(f"high-detail/02-provinces/province-only/{path.name}: missing/empty WADMPR.")
            if len(geoms) == 1:
                prov_only_geoms.append(geoms[0])
                prov_only_labels.append(f"{path.name} ({pr})")
                prov_only_bboxes[path.name] = [round(float(v), 6) + 0.0 for v in geoms[0].bounds]
                prov_holes_total += count_interior_holes(geoms[0])

    if len(prov_only_geoms) == len(prov_only_files):
        check_coverage(prov_only_geoms, prov_only_labels, "high-detail/02-provinces/province-only (all)", errors)

    total_with_dist_features = 0
    with_dist_props: dict[str, list[dict[str, Any]]] = {}
    for path in with_dist_files:
        features, geoms, size = validate_geojson_file(
            path, f"high-detail/02-provinces/with-districts/{path.name}", max_bytes, errors
        )
        max_seen_bytes = max(max_seen_bytes, size)
        files_checked += 1
        total_with_dist_features += len(features)
        with_dist_props[path.name] = [
            feat.get("properties", {}) if isinstance(feat.get("properties"), dict) else {}
            for feat in features
        ]
        labels = []
        for i, feat in enumerate(features):
            props = feat.get("properties", {})
            pr, kk = props.get("WADMPR"), props.get("WADMKK")
            if not _non_empty_str(pr) or not _non_empty_str(kk):
                errors.append(
                    f"high-detail/02-provinces/with-districts/{path.name} feature #{i}: missing/empty WADMPR or WADMKK."
                )
            labels.append(f"#{i} ({kk})")
        if len(geoms) == len(features):
            check_coverage(geoms, labels, f"high-detail/02-provinces/with-districts/{path.name}", errors)

    # 3. High-detail individual district files (2e-05 deg)
    dist_files = sorted(dist_dir.glob("*.geojson"))
    dist_geoms: list[Any] = []
    dist_labels: list[str] = []
    dist_props: dict[str, dict[str, Any]] = {}
    dist_bboxes: dict[str, list[float]] = {}
    for path in dist_files:
        features, geoms, size = validate_geojson_file(path, f"high-detail/03-districts/{path.name}", max_bytes, errors)
        max_seen_bytes = max(max_seen_bytes, size)
        files_checked += 1
        if len(features) != 1:
            errors.append(f"high-detail/03-districts/{path.name}: expected 1 feature, found {len(features)}.")
        else:
            props = features[0].get("properties", {})
            if isinstance(props, dict):
                dist_props[path.name] = props
            pr, kk = (props.get("WADMPR"), props.get("WADMKK")) if isinstance(props, dict) else (None, None)
            if not _non_empty_str(pr) or not _non_empty_str(kk):
                errors.append(f"high-detail/03-districts/{path.name}: missing/empty WADMPR or WADMKK.")
            if len(geoms) == 1:
                dist_geoms.append(geoms[0])
                dist_labels.append(f"{path.name} ({pr}/{kk})")
                dist_bboxes[path.name] = [round(float(v), 6) + 0.0 for v in geoms[0].bounds]

    if len(dist_geoms) == len(dist_files):
        check_coverage(dist_geoms, dist_labels, "high-detail/03-districts (all)", errors)

    # 4. Cross-level consistency & expected counts
    prov_count = len(prov_only_files)
    dist_count = len(dist_files)

    if len(with_dist_files) != prov_count:
        errors.append(f"Province file count mismatch: province-only={prov_count}, with-districts={len(with_dist_files)}.")
    if "provinces" in nat_counts and nat_counts["provinces"] != prov_count:
        errors.append(
            f"Province count mismatch: simplified={nat_counts['provinces']}, high-detail province-only={prov_count}."
        )
    if total_with_dist_features != dist_count:
        errors.append(
            f"District count mismatch: sum(with-districts)={total_with_dist_features}, high-detail 03-districts={dist_count}."
        )
    if "districts" in nat_counts and nat_counts["districts"] != dist_count:
        errors.append(
            f"District count mismatch: simplified={nat_counts['districts']}, high-detail 03-districts={dist_count}."
        )

    if args.expected_provinces > 0 and prov_count != args.expected_provinces:
        errors.append(f"Expected {args.expected_provinces} provinces, found {prov_count}.")
    if args.expected_districts > 0 and dist_count != args.expected_districts:
        errors.append(f"Expected {args.expected_districts} districts, found {dist_count}.")

    # 5. Provenance metadata & topology parity check
    meta: dict[str, Any] | None = None
    if not args.no_metadata:
        if not args.metadata.is_file():
            errors.append(f"Missing provenance metadata file: {args.metadata}")
        else:
            try:
                meta = json.loads(args.metadata.read_text(encoding="utf-8"))
                meta_counts = meta.get("counts", {})
                if meta_counts.get("provinces") != prov_count:
                    errors.append(
                        f"Metadata counts.provinces={meta_counts.get('provinces')} does not match {prov_count}."
                    )
                if meta_counts.get("districts") != dist_count:
                    errors.append(
                        f"Metadata counts.districts={meta_counts.get('districts')} does not match {dist_count}."
                    )
                topo = meta.get("topology")
                if isinstance(topo, dict):
                    if topo.get("country_interior_holes") != country_holes:
                        errors.append(
                            f"Metadata topology.country_interior_holes={topo.get('country_interior_holes')} "
                            f"does not match observed {country_holes}."
                        )
                    if topo.get("province_interior_holes_total") != prov_holes_total:
                        errors.append(
                            f"Metadata topology.province_interior_holes_total={topo.get('province_interior_holes_total')} "
                            f"does not match observed {prov_holes_total}."
                        )
                    for bool_key in (
                        "districts_detail_coverage_valid",
                        "provinces_detail_coverage_valid",
                        "districts_simplified_coverage_valid",
                        "provinces_simplified_coverage_valid",
                    ):
                        if topo.get(bool_key) is not True:
                            errors.append(f"Metadata topology.{bool_key} is {topo.get(bool_key)!r}, expected True.")
            except Exception as exc:
                errors.append(f"Failed to parse metadata file {args.metadata}: {exc}")

    # 6. Catalog manifest parity check
    if args.no_catalog:
        catalog_path = None
    elif args.catalog is not None:
        catalog_path = args.catalog
    elif not args.no_metadata:
        catalog_path = args.metadata.parent / "catalog.json"
    else:
        catalog_path = None

    if catalog_path is not None:
        validate_catalog(
            catalog_path=catalog_path,
            data_dir=data_dir,
            prov_count=prov_count,
            dist_count=dist_count,
            expected_provinces=args.expected_provinces,
            expected_districts=args.expected_districts,
            prov_only_props=prov_only_props,
            prov_only_bboxes=prov_only_bboxes,
            with_dist_props=with_dist_props,
            dist_props=dist_props,
            dist_bboxes=dist_bboxes,
            meta=meta,
            errors=errors,
        )

    print(
        f"Validation checked {files_checked} GeoJSON files: "
        f"{prov_count} provinces, {dist_count} districts, "
        f"country holes={country_holes}, province holes={prov_holes_total}, "
        f"max file size {max_seen_bytes / 1_000_000:.2f} MB."
    )
    if errors:
        for err in errors:
            print(f"ERROR: {err}", file=sys.stderr)
        return 1

    print("VALIDATION PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
