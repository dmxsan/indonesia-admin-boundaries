import os
import geopandas as gpd
from geopandas import GeoDataFrame

# input folder: each file is one province with its districts inside
IN_DIR  = r"02-processed-data/02-provinces/with-districts"
# output folder: same names, but dissolved to province-only
OUT_DIR = r"02-processed-data/02-provinces/province-only"

os.makedirs(OUT_DIR, exist_ok=True)

# loop through all files in the input directory
for fname in os.listdir(IN_DIR):
    if not fname.lower().endswith(".geojson"):
        continue

    in_path  = os.path.join(IN_DIR, fname)
    out_path = os.path.join(OUT_DIR, fname)

    # read
    gdf = gpd.read_file(in_path)

    # dissolve by province code (WADMPR)
    dissolved = gdf.dissolve(by="WADMPR")

    # bring WADMPR back from index to column
    dissolved = dissolved.reset_index()

    # drop the district column if present
    if "WADMKK" in dissolved.columns:
        dissolved = dissolved.drop(columns=["WADMKK"])

    # optionally keep only the columns you want
    keep = [c for c in ["OBJECTID","WADMPR", "geometry"] if c in dissolved.columns]
    dissolved = dissolved[keep]

    # **Re-wrap as a GeoDataFrame** so we can call .to_file()
    dissolved = GeoDataFrame(dissolved, geometry="geometry", crs=gdf.crs)    

    # write out
    dissolved.to_file(out_path, driver="GeoJSON")
    print(f"Created {out_path}")
