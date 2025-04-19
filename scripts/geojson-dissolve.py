import os
import geopandas as gpd

def dissolve_by_wadmpr(input_geojson, output_geojson):
    # ---------------------------
    # Load the GeoJSON file
    # ---------------------------
    try:
        gdf = gpd.read_file(input_geojson)
    except Exception as e:
        print(f"Error reading input GeoJSON: {e}")
        return

    # ---------------------------
    # Check if the required column exists
    # ---------------------------
    if "WADMPR" not in gdf.columns:
        print("Error: 'WADMPR' column not found in the input file.")
        return

    # ---------------------------
    # Option 1: Keep only province and geometry (recommended)
    # ---------------------------
    gdf = gdf[["WADMPR", "geometry"]]

    # ---------------------------
    # Dissolve all features by the WADMPR column.
    # The default dissolve union combines all geometries for a given province.
    # ---------------------------
    dissolved = gdf.dissolve(by="WADMPR")
    
    # ---------------------------
    # Reset the index so that 'WADMPR' becomes a column again
    # ---------------------------
    dissolved = dissolved.reset_index()
    
    # ---------------------------
    # Save the dissolved GeoDataFrame as a GeoJSON file
    # ---------------------------
    try:
        dissolved.to_file(output_geojson, driver="GeoJSON")
        print(f"Dissolved GeoJSON saved as: {output_geojson}")
    except Exception as e:
        print(f"Error writing output GeoJSON: {e}")

if __name__ == "__main__":
    input_file = r"02-processed-data\merged-geojson\District_Administration_20230907_fixed.geojson"
    output_file = r"02-processed-data\merged-geojson\Province_Administration_20230907.geojson"
    dissolve_by_wadmpr(input_file, output_file)
