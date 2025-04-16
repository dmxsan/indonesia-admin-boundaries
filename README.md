# Indonesia Admin Boundaries

This repository contains Indonesia's administrative boundaries data (from national to district level) derived from the latest shapefiles provided by Badan Informasi Geospasial (BIG).

## Data Source
The data is obtained from [Badan Informasi Geospasial (BIG)](https://geoportal.big.go.id/) (latest version). Please refer to their terms of use when utilizing this dataset.

## Repository Structure
Below is an overview of the folder organization:
```
INDONESIA-ADMIN-BOUNDARIES/
├── 01-raw-data/
│   ├── geojson/
│   └── shapefile/
├── 02-processed-data/
│   ├── geojson/
│   │   ├── 01-national/       
│   │   ├── 02-provinces/
│   │   └── 03-districts/
│   └── merged-geojson/
├── 03-scripts/
│   └── shapefile-to-geojson.py
├── .gitignore
├── .gitattributes
├── README.md
└── requirements.txt
```

