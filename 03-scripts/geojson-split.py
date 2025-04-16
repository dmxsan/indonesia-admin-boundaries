import os
import geopandas as gpd

def split_by_province(input_geojson, output_directory):
    # ---------------------------
    # Print the current working directory for debugging
    # ---------------------------
    cwd = os.getcwd()
    print(f"Current working directory: {cwd}")
    
    # Get the absolute path of the input file
    abs_input_path = os.path.abspath(input_geojson)
    print(f"Looking for input file: {abs_input_path}")

    # ---------------------------
    # Check if the input file exists
    # ---------------------------
    if not os.path.exists(abs_input_path):
        print(f"Error: The file '{abs_input_path}' does not exist.")
        return

    # ---------------------------
    # Attempt to load the GeoJSON file
    # ---------------------------
    try:
        gdf = gpd.read_file(abs_input_path)
    except Exception as e:
        print(f"Error reading the GeoJSON file: {e}")
        return

    # ---------------------------
    # Ensure that the 'WADMPR' column exists
    # ---------------------------
    if "WADMKK" not in gdf.columns:
        print("Error: 'WADMKK' column not found in the input file.")
        return

    # ---------------------------
    # Ensure the output directory exists
    # ---------------------------
    os.makedirs(output_directory, exist_ok=True)

    # ---------------------------
    # Get the unique province names from the WADMPR column
    # ---------------------------
    provinces = gdf["WADMKK"].unique()

    # ---------------------------
    # Loop through each province and export it as a separate GeoJSON
    # ---------------------------
    for province in provinces:
        try:
            # Filter rows belonging to this province
            subset = gdf[gdf["WADMKK"] == province]

            # Create a filename-friendly version of the province name (remove spaces, slashes, etc.)
            province_clean = str(province).replace(" ", "_").replace("/", "_").replace("\\", "_")

            # Construct the output file path
            output_file = os.path.join(output_directory, f"{province_clean}.geojson")

            # Save the subset to a new GeoJSON file
            subset.to_file(output_file, driver="GeoJSON")
            print(f"Created {output_file}")
        except Exception as e:
            print(f"Error processing province '{province}': {e}")

if __name__ == "__main__":
    # Use raw strings for Windows paths
    input_geojson = r"02-processed-data/merged-geojson/District_Administration_20230907.geojson"
    output_folder = r"02-processed-data/geojson/03-disctricts"
    split_by_province(input_geojson, output_folder)
