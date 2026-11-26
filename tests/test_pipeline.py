#!/usr/bin/env python3
"""Unit tests for the Indonesia administrative boundaries ingestion and validation pipeline."""

from __future__ import annotations

import contextlib
import io
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
from shapely.geometry import Polygon

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
for _p in (str(REPO_ROOT), str(SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import ingest  # noqa: E402
import validate  # noqa: E402


def _make_synthetic_districts_gdf() -> gpd.GeoDataFrame:
    """Create a 2-province, 4-district valid coverage with an interior lake hole in Province A."""
    lake_hole = [(100.2, -1.2), (100.8, -1.2), (100.8, -0.8), (100.2, -0.8), (100.2, -1.2)]
    poly_a1 = Polygon(
        [(100.0, -2.0), (101.0, -2.0), (101.0, 0.0), (100.0, 0.0), (100.0, -2.0)],
        holes=[lake_hole],
    )
    poly_a2 = Polygon([(101.0, -2.0), (102.0, -2.0), (102.0, 0.0), (101.0, 0.0), (101.0, -2.0)])
    poly_b1 = Polygon([(102.0, -2.0), (103.0, -2.0), (103.0, -1.0), (102.0, -1.0), (102.0, -2.0)])
    poly_b2 = Polygon([(102.0, -1.0), (103.0, -1.0), (103.0, 0.0), (102.0, 0.0), (102.0, -1.0)])

    rows = [
        {
            "WADMPR": "Provinsi Alpha",
            "WADMKK": "Kabupaten Alpha Satu",
            "KDPPUM": "91",
            "KDPKAB": "91.01",
            "KDPBPS": "91",
            "KDBBPS": "9101",
            "geometry": poly_a1,
        },
        {
            "WADMPR": "Provinsi Alpha",
            "WADMKK": "Kabupaten Alpha Dua",
            "KDPPUM": "91",
            "KDPKAB": "91.02",
            "KDPBPS": "91",
            "KDBBPS": "9102",
            "geometry": poly_a2,
        },
        {
            "WADMPR": "Provinsi Beta",
            "WADMKK": "Kabupaten Beta Satu",
            "KDPPUM": "92",
            "KDPKAB": "92.01",
            "KDPBPS": "92",
            "KDBBPS": "9201",
            "geometry": poly_b1,
        },
        {
            "WADMPR": "Provinsi Beta",
            "WADMKK": "Kota Beta Dua",
            "KDPPUM": "92",
            "KDPKAB": "92.71",
            "KDPBPS": "92",
            "KDBBPS": "9271",
            "geometry": poly_b2,
        },
    ]
    return gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326")


class TestStringAndFilenameHelpers(unittest.TestCase):
    def test_sanitize_filename_edge_cases(self) -> None:
        self.assertEqual(ingest.sanitize_filename("/"), "unnamed")
        self.assertEqual(ingest.sanitize_filename("   \t\n  "), "unnamed")
        self.assertEqual(ingest.sanitize_filename('<>:"|?*'), "unnamed")
        self.assertEqual(ingest.sanitize_filename("  Jawa   Tengah  "), "Jawa_Tengah")
        self.assertEqual(ingest.sanitize_filename("Kab/Kota\\Test"), "Kab_Kota_Test")
        self.assertEqual(
            ingest.sanitize_filename('Kabupaten <Test>:"|?* Name'),
            "Kabupaten_Test_Name",
        )

    def test_district_filenames_duplicate_disambiguation(self) -> None:
        gdf = gpd.GeoDataFrame(
            [
                {"WADMPR": "Banten", "WADMKK": "Tangerang", "geometry": Polygon([(0, 0), (1, 0), (1, 1), (0, 0)])},
                {"WADMPR": "Jawa Barat", "WADMKK": "Tangerang", "geometry": Polygon([(1, 0), (2, 0), (2, 1), (1, 0)])},
                {
                    "WADMPR": "Jawa Tengah",
                    "WADMKK": "Kota Surakarta",
                    "geometry": Polygon([(2, 0), (3, 0), (3, 1), (2, 0)]),
                },
            ],
            geometry="geometry",
            crs="EPSG:4326",
        )
        filenames = ingest._district_filenames(gdf)
        self.assertEqual(
            filenames,
            [
                "Tangerang__Banten.geojson",
                "Tangerang__Jawa_Barat.geojson",
                "Kota_Surakarta.geojson",
            ],
        )

    def test_clean_str_and_first_non_null(self) -> None:
        for null_like in ("", "   ", "None", "none", "null", "NULL", "NaN", "nan", None, np.nan):
            self.assertIsNone(ingest.clean_str(null_like))

        self.assertEqual(ingest.clean_str("  DKI Jakarta \n"), "DKI Jakarta")
        self.assertEqual(ingest.clean_str(33), "33")

        series = pd.Series([None, np.nan, "33", "32", "33"])
        self.assertEqual(ingest.first_non_null(series), "32")
        self.assertIsNone(ingest.first_non_null(pd.Series([None, np.nan])))


class TestLoadFullDistricts(unittest.TestCase):
    def test_load_full_districts_filters_water_strips_3d_and_repairs_self_intersection(self) -> None:
        # 1. 3D polygon with Z coordinates
        poly_3d = Polygon(
            [
                (106.0, -6.0, 10.0),
                (107.0, -6.0, 20.0),
                (107.0, -5.0, 30.0),
                (106.0, -5.0, 40.0),
                (106.0, -6.0, 10.0),
            ]
        )
        # 2. Self-intersecting bowtie polygon
        bowtie_invalid = Polygon(
            [
                (107.0, -6.0),
                (108.0, -5.0),
                (108.0, -6.0),
                (107.0, -5.0),
                (107.0, -6.0),
            ]
        )
        self.assertFalse(bowtie_invalid.is_valid)

        # 3 & 4. Water polygons with null/token WADMKK across two provinces
        water_a = Polygon([(108.0, -6.0), (109.0, -6.0), (109.0, -5.0), (108.0, -5.0), (108.0, -6.0)])
        water_b = Polygon([(109.0, -6.0), (110.0, -6.0), (110.0, -5.0), (109.0, -5.0), (109.0, -6.0)])

        raw_gdf = gpd.GeoDataFrame(
            [
                {
                    "WADMPR": "Provinsi Satu",
                    "WADMKK": "Kabupaten 3D",
                    "KDPPUM": "11",
                    "KDPKAB": "11.01",
                    "geometry": poly_3d,
                },
                {
                    "WADMPR": "Provinsi Satu",
                    "WADMKK": "Kabupaten Bowtie",
                    "KDPPUM": "11",
                    "KDPKAB": "11.02",
                    "geometry": bowtie_invalid,
                },
                {
                    "WADMPR": "Provinsi Satu",
                    "WADMKK": None,
                    "KDPPUM": "11",
                    "KDPKAB": None,
                    "geometry": water_a,
                },
                {
                    "WADMPR": "Provinsi Dua",
                    "WADMKK": "None",
                    "KDPPUM": "12",
                    "KDPKAB": "NaN",
                    "geometry": water_b,
                },
            ],
            geometry="geometry",
            crs="EPSG:4326",
        )

        with tempfile.TemporaryDirectory() as tmp:
            gpkg_path = Path(tmp) / "synthetic_raw.gpkg"
            raw_gdf.to_file(gpkg_path, layer="raw_admin", driver="GPKG", engine="pyogrio")

            districts_full, raw_counts = ingest.load_full_districts(gpkg_path, "raw_admin")

        self.assertEqual(raw_counts["raw_features"], 4)
        self.assertEqual(raw_counts["dropped_null_wadmkk"], 2)
        self.assertEqual(
            raw_counts["dropped_null_wadmkk_by_province"],
            {"Provinsi Dua": 1, "Provinsi Satu": 1},
        )
        self.assertEqual(len(districts_full), 2)
        self.assertEqual(
            list(districts_full["WADMKK"]),
            ["Kabupaten 3D", "Kabupaten Bowtie"],
        )
        for geom in districts_full.geometry:
            self.assertFalse(shapely.has_z(geom), "Expected 3D Z coordinates to be stripped to 2D.")
            self.assertTrue(geom.is_valid, "Expected self-intersecting geometry to be repaired.")
            self.assertGreater(geom.area, 0.0)


class TestSyntheticCoveragePipelineAndValidator(unittest.TestCase):
    def _build_synthetic_data_dir(self, output_dir: Path) -> dict[str, object]:
        districts_full = _make_synthetic_districts_gdf()
        prov_code_cols = [c for c in ingest.PROVINCE_CODE_COLS if c in districts_full.columns]

        districts_detail = districts_full.copy()
        districts_detail["geometry"] = ingest.simplify_coverage_geometries(
            list(districts_full["geometry"]),
            tolerance=2e-05,
            precision=6,
            workers=1,
        )

        (
            country_simplified,
            provinces_simplified,
            districts_simplified,
            provinces_detail,
            topology,
        ) = ingest.build_derived_tiers(
            districts_detail=districts_detail,
            prov_code_cols=prov_code_cols,
            simplify_tolerance=0.001,
            precision=6,
            workers=1,
        )

        outputs = ingest.write_processed_outputs(
            output_dir=output_dir,
            precision=6,
            country_simplified=country_simplified,
            provinces_simplified=provinces_simplified,
            districts_simplified=districts_simplified,
            provinces_detail=provinces_detail,
            districts_detail=districts_detail,
        )
        return {
            "country_simplified": country_simplified,
            "provinces_simplified": provinces_simplified,
            "districts_simplified": districts_simplified,
            "provinces_detail": provinces_detail,
            "districts_detail": districts_detail,
            "topology": topology,
            "outputs": outputs,
        }

    def test_end_to_end_synthetic_coverage_pipeline_and_validate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "processed-data"
            result = self._build_synthetic_data_dir(out_dir)
            topology = result["topology"]
            assert isinstance(topology, dict)

            self.assertTrue(topology["districts_detail_coverage_valid"])
            self.assertTrue(topology["provinces_detail_coverage_valid"])
            self.assertTrue(topology["districts_simplified_coverage_valid"])
            self.assertTrue(topology["provinces_simplified_coverage_valid"])
            self.assertEqual(topology["country_interior_holes"], 1)
            self.assertEqual(topology["province_interior_holes_total"], 1)
            self.assertEqual(topology["province_interior_holes_by_province"], {"Provinsi Alpha": 1})

            exit_code = validate.main(
                [
                    "--data-dir",
                    str(out_dir),
                    "--no-metadata",
                    "--expected-provinces",
                    "2",
                    "--expected-districts",
                    "4",
                ]
            )
            self.assertEqual(exit_code, 0)

    def test_validate_rejects_forbidden_filename_none_geojson(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "processed-data"
            self._build_synthetic_data_dir(out_dir)

            forbidden_path = out_dir / "high-detail" / "03-districts" / "none.geojson"
            sample_path = out_dir / "high-detail" / "03-districts" / "Kabupaten_Alpha_Satu.geojson"
            forbidden_path.write_bytes(sample_path.read_bytes())

            stderr_buf = io.StringIO()
            with contextlib.redirect_stderr(stderr_buf):
                exit_code = validate.main(
                    [
                        "--data-dir",
                        str(out_dir),
                        "--no-metadata",
                        "--expected-provinces",
                        "0",
                        "--expected-districts",
                        "0",
                    ]
                )
            self.assertEqual(exit_code, 1)
            self.assertIn("Forbidden filename found", stderr_buf.getvalue())

    def test_validate_rejects_legacy_crs_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "processed-data"
            self._build_synthetic_data_dir(out_dir)

            nat_path = out_dir / "simplified" / "01-national" / "indonesia.geojson"
            payload = json.loads(nat_path.read_text(encoding="utf-8"))
            payload["crs"] = {"type": "name", "properties": {"name": "EPSG:4326"}}
            nat_path.write_text(json.dumps(payload), encoding="utf-8")

            stderr_buf = io.StringIO()
            with contextlib.redirect_stderr(stderr_buf):
                exit_code = validate.main(
                    [
                        "--data-dir",
                        str(out_dir),
                        "--no-metadata",
                        "--expected-provinces",
                        "2",
                        "--expected-districts",
                        "4",
                    ]
                )
            self.assertEqual(exit_code, 1)
            self.assertIn("legacy top-level 'crs' key is forbidden", stderr_buf.getvalue())

    def test_validate_rejects_3d_coordinates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "processed-data"
            self._build_synthetic_data_dir(out_dir)

            nat_path = out_dir / "simplified" / "01-national" / "indonesia.geojson"
            payload = json.loads(nat_path.read_text(encoding="utf-8"))
            payload["features"][0]["geometry"]["coordinates"] = [
                [[100.0, -2.0, 5.0], [103.0, -2.0, 5.0], [103.0, 0.0, 5.0], [100.0, 0.0, 5.0], [100.0, -2.0, 5.0]]
            ]
            nat_path.write_text(json.dumps(payload), encoding="utf-8")

            stderr_buf = io.StringIO()
            with contextlib.redirect_stderr(stderr_buf):
                exit_code = validate.main(
                    [
                        "--data-dir",
                        str(out_dir),
                        "--no-metadata",
                        "--expected-provinces",
                        "2",
                        "--expected-districts",
                        "4",
                    ]
                )
            self.assertEqual(exit_code, 1)
            self.assertIn("expected 2D coordinates, got length 3", stderr_buf.getvalue())

    def test_validate_rejects_overlapping_coverage_polygons(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "processed-data"
            self._build_synthetic_data_dir(out_dir)

            prov_path = out_dir / "simplified" / "02-provinces" / "indonesia_provinces.geojson"
            payload = json.loads(prov_path.read_text(encoding="utf-8"))
            # Expand the first province so it overlaps the second province (x from 100..102.5 overlaps 102..103)
            payload["features"][0]["geometry"] = {
                "type": "Polygon",
                "coordinates": [
                    [[100.0, -2.0], [102.5, -2.0], [102.5, 0.0], [100.0, 0.0], [100.0, -2.0]]
                ],
            }
            prov_path.write_text(json.dumps(payload), encoding="utf-8")

            stderr_buf = io.StringIO()
            with contextlib.redirect_stderr(stderr_buf):
                exit_code = validate.main(
                    [
                        "--data-dir",
                        str(out_dir),
                        "--no-metadata",
                        "--expected-provinces",
                        "2",
                        "--expected-districts",
                        "4",
                    ]
                )
            self.assertEqual(exit_code, 1)
            self.assertIn("invalid topological coverage", stderr_buf.getvalue())

    def test_validate_rejects_missing_wadmpr_or_wadmkk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "processed-data"
            self._build_synthetic_data_dir(out_dir)

            dist_path = out_dir / "simplified" / "03-districts" / "indonesia_districts.geojson"
            payload = json.loads(dist_path.read_text(encoding="utf-8"))
            payload["features"][0]["properties"]["WADMPR"] = ""
            payload["features"][1]["properties"]["WADMKK"] = None
            dist_path.write_text(json.dumps(payload), encoding="utf-8")

            stderr_buf = io.StringIO()
            with contextlib.redirect_stderr(stderr_buf):
                exit_code = validate.main(
                    [
                        "--data-dir",
                        str(out_dir),
                        "--no-metadata",
                        "--expected-provinces",
                        "2",
                        "--expected-districts",
                        "4",
                    ]
                )
            self.assertEqual(exit_code, 1)
            self.assertIn("missing/empty WADMPR or WADMKK", stderr_buf.getvalue())

    def test_catalog_build_and_validation_parity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "processed-data"
            result = self._build_synthetic_data_dir(out_dir)
            provinces_detail = result["provinces_detail"]
            districts_detail = result["districts_detail"]
            assert isinstance(provinces_detail, gpd.GeoDataFrame)
            assert isinstance(districts_detail, gpd.GeoDataFrame)

            source_meta = {"edition": "Edisi Uji 2026", "edition_date": "2026-06-12"}
            # Use default repo-relative "processed-data" prefix to exercise --data-dir remapping.
            catalog = ingest.build_catalog(
                provinces_detail=provinces_detail,
                districts_detail=districts_detail,
                source_meta=source_meta,
                output_dir=Path("processed-data"),
            )
            cat_path = Path(tmp) / "catalog.json"
            ingest.write_catalog(catalog, cat_path)

            exit_code = validate.main(
                [
                    "--data-dir",
                    str(out_dir),
                    "--no-metadata",
                    "--catalog",
                    str(cat_path),
                    "--expected-provinces",
                    "2",
                    "--expected-districts",
                    "4",
                ]
            )
            self.assertEqual(exit_code, 0)

            tampered_bbox = json.loads(cat_path.read_text(encoding="utf-8"))
            tampered_bbox["districts"][0]["bbox"][0] += 0.5
            cat_path.write_text(json.dumps(tampered_bbox), encoding="utf-8")

            stderr_buf = io.StringIO()
            with contextlib.redirect_stderr(stderr_buf):
                exit_code = validate.main(
                    [
                        "--data-dir",
                        str(out_dir),
                        "--no-metadata",
                        "--catalog",
                        str(cat_path),
                        "--expected-provinces",
                        "2",
                        "--expected-districts",
                        "4",
                    ]
                )
            self.assertEqual(exit_code, 1)
            self.assertIn("bbox", stderr_buf.getvalue())

            tampered_count = json.loads(json.dumps(catalog))
            tampered_count["provinces"][0]["district_count"] += 1
            cat_path.write_text(json.dumps(tampered_count), encoding="utf-8")

            stderr_buf = io.StringIO()
            with contextlib.redirect_stderr(stderr_buf):
                exit_code = validate.main(
                    [
                        "--data-dir",
                        str(out_dir),
                        "--no-metadata",
                        "--catalog",
                        str(cat_path),
                        "--expected-provinces",
                        "2",
                        "--expected-districts",
                        "4",
                    ]
                )
            self.assertEqual(exit_code, 1)
            self.assertIn("district_count", stderr_buf.getvalue())


class TestDocumentationIntegrity(unittest.TestCase):
    EXPECTED_DISCLAIMER = "Data batas yang berstatus indikatif tidak dapat dijadikan referensi hukum."
    FENCED_CODE_RE = re.compile(r"```[\s\S]*?```")
    MD_LINK_RE = re.compile(r"\]\(([^)]+)\)")
    HTML_LINK_RE = re.compile(r'\b(?:href|src)\s*=\s*["\']([^"\']+)["\']', re.I)

    def test_readme_links_and_big_disclaimer(self) -> None:
        for readme_name in ("README.md", "README.id.md"):
            readme_path = REPO_ROOT / readme_name
            self.assertTrue(readme_path.is_file(), f"Missing documentation file: {readme_name}")
            content = readme_path.read_text(encoding="utf-8")

            self.assertIn(
                self.EXPECTED_DISCLAIMER,
                content,
                f"{readme_name} is missing BIG's verbatim legal disclaimer.",
            )

            prose = self.FENCED_CODE_RE.sub("", content)
            raw_targets = self.MD_LINK_RE.findall(prose) + self.HTML_LINK_RE.findall(prose)
            self.assertGreater(len(raw_targets), 0, f"Expected links in {readme_name}")

            checked_local = 0
            for raw in raw_targets:
                target = raw.strip().split()[0]
                if target.startswith(("http://", "https://", "#")):
                    continue
                rel_path = target.split("#", 1)[0]
                if not rel_path:
                    continue
                resolved = (REPO_ROOT / rel_path).resolve()
                self.assertTrue(
                    resolved.exists(),
                    f"Broken local link in {readme_name}: {raw!r} -> {resolved} does not exist.",
                )
                checked_local += 1

            self.assertGreater(checked_local, 0, f"Expected local links to be verified in {readme_name}")


if __name__ == "__main__":
    unittest.main()
