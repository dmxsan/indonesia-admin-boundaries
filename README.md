# Indonesia Admin Boundaries

This repository contains Indonesia's administrative boundaries data (from national to district level) derived from the latest shapefiles provided by Badan Informasi Geospasial (BIG).

## Data Source
The data is obtained from [Badan Informasi Geospasial (BIG)](https://geoportal.big.go.id/) (latest version). Please refer to their terms of use when utilizing this dataset.

## Repository Structure
Below is an overview of the folder organization:
```
INDONESIA-ADMIN-BOUNDARIES/
├── raw-data/ 
│   └──  raw-data.md                   # Notes containing stored raw data
├── processed-data/
│   ├── 01-national/                   # Contains processed national-level GeoJSON files
│   ├── 02-provinces/                  # Contains processed province-level GeoJSON files
│   └── 03-districts/                  # Contains processed district-level GeoJSON files
├── scripts/
│   ├── geojson-dissolve.py            # Script to dissolve GeoJSON features by a chosen attribute (e.g. provinces)
|   ├── geojson-split.py               # Script to split a GeoJSON file into individual files based on an attribute (e.g. district)
|   ├── repair-invalid-geometries.py   # Script to repair invalid geometries using the buffer(0) method
|   ├── shapefile-to-geojson.py        # Script to convert shapefiles to GeoJSON
│   └── test-geojson.py                # Script to test if a GeoJSON file parses correctly using the json module
├── .gitignore
├── .gitattributes
├── README.md
└── requirements.txt
```

