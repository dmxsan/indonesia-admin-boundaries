import geopandas as gpd
from shapely.geometry import shape

def repair_geometry(geom):
    """
    Attempt to fix invalid geometry using the buffer(0) trick.
    If geom is None or buffer fails, return None.
    """
    if geom is None:
        return None
    # Check if already valid
    if geom.is_valid:
        return geom
    try:
        # The buffer(0) approach often fixes minor invalidities (e.g., self-intersections)
        fixed = geom.buffer(0)
        if fixed.is_valid:
            return fixed
        else:
            # If it's still invalid, return None or the original geometry
            return None
    except:
        return None

def repair_and_dissolve(input_file, output_file, dissolve_col="WADMPR"):
    """
    1) Load GeoDataFrame
    2) Repair invalid geometries
    3) Dissolve by a specified column (e.g., 'WADMPR')
    4) Write out to GeoJSON
    """
    # Load the raw data
    gdf = gpd.read_file(input_file)
    
    # Repair invalid geometries
    gdf["geometry"] = gdf["geometry"].apply(repair_geometry)

    # Optionally drop rows with None geometry
    # gdf = gdf[~gdf["geometry"].isnull()].copy()
    
    # Dissolve by the specified column
    # Keep only the relevant columns plus geometry
    # (If you also want other columns, define an aggfunc in dissolve)
    keep_cols = [dissolve_col, "geometry"]
    gdf = gdf[keep_cols].copy()
    
    dissolved = gdf.dissolve(by=dissolve_col)

    # Reset index so the grouping column is a normal column again
    dissolved = dissolved.reset_index()
    
    # Write to GeoJSON
    dissolved.to_file(output_file, driver="GeoJSON")
    print(f"Repaired and dissolved output saved to: {output_file}")

if __name__ == "__main__":
    input_file = r"01-raw-data\geojson\District_Administration_20230907.geojson"
    output_file = r"01-raw-data\geojson\District_Administration_20230907_fixed.geojson"
    
    repair_and_dissolve(input_file, output_file, dissolve_col="WADMPR")
