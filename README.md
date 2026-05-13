# Laser Cut DXF Converter

Streamlit tool for batch-processing DXF files for laser cutting.

## Features

- Browser upload mode for hosted Streamlit deployments.
- Downloadable ZIP output containing converted DXFs and the combined file.
- Local folder mode for running on your own machine.
- Processes top-level `.dxf` files from an input folder.
- Explodes modelspace blocks and compound entities.
- Runs an overkill pass to remove duplicate geometry.
- Sets DXF units to millimeters.
- Scales modelspace geometry by `1000`.
- Moves final lower-left extents to `(0, 0)`.
- Saves AutoCAD view zoomed to the final extents.
- Creates `combined/combine.dxf` with all converted files laid out in a grid or row, labeled by filename.

## Run

```powershell
pip install -r requirements.txt
streamlit run dxf_batch_converter.py
```

## Deploy

Deploy `dxf_batch_converter.py` from the `main` branch on Streamlit Community Cloud.
