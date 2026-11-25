#!/usr/bin/env python3
"""Validate generated GeoJSON boundaries and provenance metadata."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from shapely.geometry import shape
from shapely.validation import explain_validity

FORBIDDEN_FILENAMES = {"none.geojson", "null.geojson", "nan.geojson"}
NATIONAL_FILES = {
    "country": "indonesia_country_simplified.geojson",
    "provinces": "indonesia_provinces_simplified.geojson",
    "districts": "indonesia_districts_simplified.geojson",
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate processed GeoJSON boundary outputs and provenance metadata."
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


def validate_geojson_file(
    path: Path,
    rel_label: str,
    max_bytes: int,
    errors: list[str],
) -> tuple[list[dict[str, Any]], int]:
    size = path.stat().st_size
    if size > max_bytes:
        errors.append(f"{rel_label}: size {size / 1_000_000:.2f} MB exceeds {max_bytes / 1_000_000:.2f} MB limit.")

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        errors.append(f"{rel_label}: invalid JSON ({exc}).")
        return [], size

    if not isinstance(data, dict) or data.get("type") != "FeatureCollection":
        errors.append(f"{rel_label}: top-level object must be a GeoJSON FeatureCollection.")
        return [], size
    if "crs" in data:
        errors.append(f"{rel_label}: legacy top-level 'crs' key is forbidden under RFC 7946.")

    features = data.get("features")
    if not isinstance(features, list) or not features:
        errors.append(f"{rel_label}: 'features' must be a non-empty list.")
        return [], size

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

    return features, size


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    data_dir = args.data_dir
    max_bytes = int(args.max_file_mb * 1_000_000)
    errors: list[str] = []

    nat_dir = data_dir / "01-national"
    prov_only_dir = data_dir / "02-provinces" / "province-only"
    with_dist_dir = data_dir / "02-provinces" / "with-districts"
    dist_dir = data_dir / "03-districts"

    for d in (nat_dir, prov_only_dir, with_dist_dir, dist_dir):
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

    # 1. National outputs
    nat_counts: dict[str, int] = {}
    for key, filename in NATIONAL_FILES.items():
        path = nat_dir / filename
        if not path.is_file():
            errors.append(f"Missing national file: {path}")
            continue
        features, size = validate_geojson_file(path, f"01-national/{filename}", max_bytes, errors)
        max_seen_bytes = max(max_seen_bytes, size)
        files_checked += 1
        nat_counts[key] = len(features)
        if key == "country" and len(features) != 1:
            errors.append(f"01-national/{filename}: expected 1 feature, found {len(features)}.")
        elif key == "provinces":
            for i, feat in enumerate(features):
                if not _non_empty_str(feat.get("properties", {}).get("WADMPR")):
                    errors.append(f"01-national/{filename} feature #{i}: missing/empty WADMPR.")
        elif key == "districts":
            for i, feat in enumerate(features):
                props = feat.get("properties", {})
                if not _non_empty_str(props.get("WADMPR")) or not _non_empty_str(props.get("WADMKK")):
                    errors.append(f"01-national/{filename} feature #{i}: missing/empty WADMPR or WADMKK.")

    # 2. Province-only & with-districts outputs
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

    for path in prov_only_files:
        features, size = validate_geojson_file(
            path, f"02-provinces/province-only/{path.name}", max_bytes, errors
        )
        max_seen_bytes = max(max_seen_bytes, size)
        files_checked += 1
        if len(features) != 1:
            errors.append(
                f"02-provinces/province-only/{path.name}: expected 1 feature, found {len(features)}."
            )
        elif not _non_empty_str(features[0].get("properties", {}).get("WADMPR")):
            errors.append(f"02-provinces/province-only/{path.name}: missing/empty WADMPR.")

    total_with_dist_features = 0
    for path in with_dist_files:
        features, size = validate_geojson_file(
            path, f"02-provinces/with-districts/{path.name}", max_bytes, errors
        )
        max_seen_bytes = max(max_seen_bytes, size)
        files_checked += 1
        total_with_dist_features += len(features)
        for i, feat in enumerate(features):
            props = feat.get("properties", {})
            if not _non_empty_str(props.get("WADMPR")) or not _non_empty_str(props.get("WADMKK")):
                errors.append(
                    f"02-provinces/with-districts/{path.name} feature #{i}: missing/empty WADMPR or WADMKK."
                )

    # 3. Individual district files
    dist_files = sorted(dist_dir.glob("*.geojson"))
    for path in dist_files:
        features, size = validate_geojson_file(path, f"03-districts/{path.name}", max_bytes, errors)
        max_seen_bytes = max(max_seen_bytes, size)
        files_checked += 1
        if len(features) != 1:
            errors.append(f"03-districts/{path.name}: expected 1 feature, found {len(features)}.")
        else:
            props = features[0].get("properties", {})
            if not _non_empty_str(props.get("WADMPR")) or not _non_empty_str(props.get("WADMKK")):
                errors.append(f"03-districts/{path.name}: missing/empty WADMPR or WADMKK.")

    # 4. Cross-level consistency & expected counts
    prov_count = len(prov_only_files)
    dist_count = len(dist_files)

    if len(with_dist_files) != prov_count:
        errors.append(f"Province file count mismatch: province-only={prov_count}, with-districts={len(with_dist_files)}.")
    if "provinces" in nat_counts and nat_counts["provinces"] != prov_count:
        errors.append(
            f"Province count mismatch: national={nat_counts['provinces']}, province-only={prov_count}."
        )
    if total_with_dist_features != dist_count:
        errors.append(
            f"District count mismatch: sum(with-districts)={total_with_dist_features}, 03-districts={dist_count}."
        )
    if "districts" in nat_counts and nat_counts["districts"] != dist_count:
        errors.append(f"District count mismatch: national={nat_counts['districts']}, 03-districts={dist_count}.")

    if args.expected_provinces > 0 and prov_count != args.expected_provinces:
        errors.append(f"Expected {args.expected_provinces} provinces, found {prov_count}.")
    if args.expected_districts > 0 and dist_count != args.expected_districts:
        errors.append(f"Expected {args.expected_districts} districts, found {dist_count}.")

    # 5. Provenance metadata check
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
            except Exception as exc:
                errors.append(f"Failed to parse metadata file {args.metadata}: {exc}")

    print(
        f"Validation checked {files_checked} GeoJSON files: "
        f"{prov_count} provinces, {dist_count} districts, "
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
