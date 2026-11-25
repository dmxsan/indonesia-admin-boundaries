#!/usr/bin/env python3
"""Validate generated GeoJSON boundaries, topological coverage, and provenance metadata."""

from __future__ import annotations

import argparse
import json
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
    return parser.parse_args(argv)


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
            pr = features[0].get("properties", {}).get("WADMPR")
            if not _non_empty_str(pr):
                errors.append(f"high-detail/02-provinces/province-only/{path.name}: missing/empty WADMPR.")
            if len(geoms) == 1:
                prov_only_geoms.append(geoms[0])
                prov_only_labels.append(f"{path.name} ({pr})")
                prov_holes_total += count_interior_holes(geoms[0])

    if len(prov_only_geoms) == len(prov_only_files):
        check_coverage(prov_only_geoms, prov_only_labels, "high-detail/02-provinces/province-only (all)", errors)

    total_with_dist_features = 0
    for path in with_dist_files:
        features, geoms, size = validate_geojson_file(
            path, f"high-detail/02-provinces/with-districts/{path.name}", max_bytes, errors
        )
        max_seen_bytes = max(max_seen_bytes, size)
        files_checked += 1
        total_with_dist_features += len(features)
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
    for path in dist_files:
        features, geoms, size = validate_geojson_file(path, f"high-detail/03-districts/{path.name}", max_bytes, errors)
        max_seen_bytes = max(max_seen_bytes, size)
        files_checked += 1
        if len(features) != 1:
            errors.append(f"high-detail/03-districts/{path.name}: expected 1 feature, found {len(features)}.")
        else:
            props = features[0].get("properties", {})
            pr, kk = props.get("WADMPR"), props.get("WADMKK")
            if not _non_empty_str(pr) or not _non_empty_str(kk):
                errors.append(f"high-detail/03-districts/{path.name}: missing/empty WADMPR or WADMKK.")
            if len(geoms) == 1:
                dist_geoms.append(geoms[0])
                dist_labels.append(f"{path.name} ({pr}/{kk})")

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
