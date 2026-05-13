import os
import processing

from qgis.core import (
    QgsProject,
    QgsVectorFileWriter,
    QgsWkbTypes
)

project = QgsProject.instance()

layer_name = "input"
area_layer_name = "area"
cut_id_field = "ELEV_MAX"

output_folder = r"C:\laser_cut"
os.makedirs(output_folder, exist_ok=True)

src_layers = project.mapLayersByName(layer_name)
area_layers = project.mapLayersByName(area_layer_name)

if not src_layers:
    raise Exception(f"Layer not found: {layer_name}")

if not area_layers:
    raise Exception(f"Layer not found: {area_layer_name}")

src = src_layers[0]
area = area_layers[0]

# Set this to False if you want polygon DXFs instead of line DXFs
convert_polygons_to_lines = True

# Prepare area layer once, same as generated cut layer
area_dissolved = processing.run(
    "native:dissolve",
    {
        "INPUT": area,
        "FIELD": [],
        "SEPARATE_DISJOINT": False,
        "OUTPUT": "TEMPORARY_OUTPUT"
    }
)["OUTPUT"]

area_dissolved.setName("area_merged")

area_export_layer = area_dissolved

if convert_polygons_to_lines:
    area_export_layer = processing.run(
        "native:polygonstolines",
        {
            "INPUT": area_dissolved,
            "OUTPUT": "TEMPORARY_OUTPUT"
        }
    )["OUTPUT"]

    area_export_layer.setName("area_lines")

# Get min/max cut_id values
cut_ids = []

for feat in src.getFeatures():
    value = feat[cut_id_field]
    if value is not None:
        cut_ids.append(int(float(value)))

if not cut_ids:
    raise Exception(f"No valid values found in field: {cut_id_field}")

min_cut_id = min(cut_ids)
max_cut_id = max(cut_ids)

for cut_id in range(min_cut_id, max_cut_id + 1):

    # Features higher than current cut_id
    expression = f'"{cut_id_field}" > {cut_id}'

    matching_count = sum(
        1 for f in src.getFeatures()
        if f[cut_id_field] is not None and int(float(f[cut_id_field])) > cut_id
    )

    if matching_count == 0:
        print(f"Skipped {cut_id}.dxf - no features higher than {cut_id}")
        continue

    # Extract all features where cut_id is higher than current value
    extracted = processing.run(
        "native:extractbyexpression",
        {
            "INPUT": src,
            "EXPRESSION": expression,
            "OUTPUT": "TEMPORARY_OUTPUT"
        }
    )["OUTPUT"]

    # Merge/dissolve all extracted features into one layer
    dissolved = processing.run(
        "native:dissolve",
        {
            "INPUT": extracted,
            "FIELD": [],
            "SEPARATE_DISJOINT": False,
            "OUTPUT": "TEMPORARY_OUTPUT"
        }
    )["OUTPUT"]

    dissolved.setName(f"{cut_id}_merged")

    export_layer = dissolved

    # Convert generated polygons to lines before DXF export
    if convert_polygons_to_lines:
        export_layer = processing.run(
            "native:polygonstolines",
            {
                "INPUT": dissolved,
                "OUTPUT": "TEMPORARY_OUTPUT"
            }
        )["OUTPUT"]

        export_layer.setName(f"{cut_id}_lines")

    # Merge generated cut layer with prepared area layer
    merged_for_dxf = processing.run(
        "native:mergevectorlayers",
        {
            "LAYERS": [
                export_layer,
                area_export_layer
            ],
            "CRS": src.crs(),
            "OUTPUT": "TEMPORARY_OUTPUT"
        }
    )["OUTPUT"]

    merged_for_dxf.setName(f"{cut_id}_with_area")

    # Optional: add result layer to QGIS project
    project.addMapLayer(merged_for_dxf)

    # Export to DXF
    output_path = os.path.join(output_folder, f"{cut_id}.dxf")

    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = "DXF"
    options.fileEncoding = "UTF-8"
    options.layerName = str(cut_id)
    options.actionOnExistingFile = QgsVectorFileWriter.CreateOrOverwriteFile

    # Avoid DXF field/attribute errors
    options.skipAttributeCreation = True

    result = QgsVectorFileWriter.writeAsVectorFormatV3(
        merged_for_dxf,
        output_path,
        project.transformContext(),
        options
    )

    if result[0] == QgsVectorFileWriter.NoError:
        print(f"Exported: {output_path}")
    else:
        print(f"Failed: {output_path}")
        print(result)

print("Done.")
