import geopandas as gpd

# ---------------------------
# Load the shapefile
# ---------------------------
shp_path = r"01-raw-data\shapefile\raw_RBI50K_Administrasi_KabKota_20230907.shp"
gdf = gpd.read_file(shp_path)

# Optionally, convert to WGS84 (EPSG:4326) if needed
# gdf = gdf.to_crs(epsg=4326)

# ---------------------------
# Export full-attribute GeoJSON
# ---------------------------
geojson_path = r"01-raw-data\geojson\raw_Administrasi_KabKota_20230907.geojson"
gdf.to_file(geojson_path, driver='GeoJSON')

# ---------------------------
# Create simplified GeoDataFrame
# Keep only the necessary columns plus the geometry column:
# OBJECTID, WADMKK, WADMPR
# ---------------------------
desired_columns = ['OBJECTID', 'WADMKK', 'WADMPR']
# Ensure to include the 'geometry' column in the resulting GeoDataFrame
gdf_simplified = gpd.GeoDataFrame(
    gdf[desired_columns + ['geometry']].copy(),
    geometry='geometry'
)

# ---------------------------
# Export simplified GeoJSON
# ---------------------------
simplified_geojson_path = r"02-processed-data\merged-geojson\simplified_Administrasi_KabKota_20230907.geojson"
gdf_simplified.to_file(simplified_geojson_path, driver='GeoJSON')

print("Finished generating two GeoJSON files:")
print(f"  1) All attributes: {geojson_path}")
print(f"  2) Simplified attributes: {simplified_geojson_path}")
