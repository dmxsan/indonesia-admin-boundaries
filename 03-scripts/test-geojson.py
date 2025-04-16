import json

input_geojson = r"02-processed-data\merged-geojson\District_Administration_20230907_fixed.geojson"

try:
    with open(input_geojson, "r", encoding="utf-8") as f:
        data = json.load(f)
    print("GeoJSON parsed successfully using the json module.")
except Exception as e:
    print("Error parsing GeoJSON:", e)
