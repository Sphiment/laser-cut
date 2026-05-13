from __future__ import annotations

from dataclasses import dataclass
import io
import math
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Iterable
import zipfile

import ezdxf
from ezdxf.addons.importer import Importer
from ezdxf import bbox, units, zoom
from ezdxf.enums import TextEntityAlignment
from ezdxf.math import Matrix44, Vec3
from ezdxf.transform import inplace
import streamlit as st


SCALE_FACTOR = 2.0
OUTPUT_SUFFIX = "_mm_scaled"
COMBINED_FOLDER_NAME = "combined"
COMBINED_FILE_NAME = "combine.dxf"
COMBINED_LABEL_LAYER = "FILE_LABELS"
COMBINED_LAYOUT_AUTO_GRID = "Auto grid"
COMBINED_LAYOUT_SINGLE_ROW = "Single row"
OVERKILL_TOLERANCE = 1e-6
MAX_EXPLODE_PASSES = 25
EXPLODE_TYPES = {
    "ARC_DIMENSION",
    "DIMENSION",
    "INSERT",
    "LARGE_RADIAL_DIMENSION",
    "LEADER",
    "LWPOLYLINE",
    "MLEADER",
    "MLINE",
    "MULTILEADER",
    "POLYLINE",
}


@dataclass(frozen=True)
class ConversionResult:
    source: Path
    status: str
    message: str
    output: Path | None = None
    initial_entity_count: int = 0
    final_entity_count: int = 0
    exploded_count: int = 0
    duplicate_count: int = 0


@dataclass(frozen=True)
class CombineItem:
    label: str
    path: Path
    extmin: Vec3
    width: float
    height: float


@dataclass(frozen=True)
class CombinedResult:
    status: str
    message: str
    output: Path | None = None
    item_count: int = 0


def clean_filename(name: str) -> str:
    filename = Path(name).name.strip()
    if not filename:
        filename = "uploaded.dxf"
    if Path(filename).suffix.lower() != ".dxf":
        filename = f"{Path(filename).stem}.dxf"
    return filename


def next_input_path(input_folder: Path, source_name: str) -> Path:
    filename = clean_filename(source_name)
    candidate = input_folder / filename
    if not candidate.exists():
        return candidate

    stem = candidate.stem
    suffix = candidate.suffix
    index = 2
    while True:
        candidate = input_folder / f"{stem}_{index}{suffix}"
        if not candidate.exists():
            return candidate
        index += 1


def discover_dxf_files(input_folder: Path) -> list[Path]:
    return sorted(
        path for path in input_folder.iterdir() if path.is_file() and path.suffix.lower() == ".dxf"
    )


def next_output_path(output_folder: Path, source_name: str) -> Path:
    candidate = output_folder / source_name
    if not candidate.exists():
        return candidate

    stem = Path(source_name).stem
    suffix = Path(source_name).suffix
    candidate = output_folder / f"{stem}{OUTPUT_SUFFIX}{suffix}"
    if not candidate.exists():
        return candidate

    index = 2
    while True:
        candidate = output_folder / f"{stem}{OUTPUT_SUFFIX}_{index}{suffix}"
        if not candidate.exists():
            return candidate
        index += 1


def _bbox_for_entities(entities: Iterable[ezdxf.entities.DXFEntity]) -> bbox.BoundingBox:
    return bbox.extents(list(entities), fast=False)


def _has_extents(extents: bbox.BoundingBox) -> bool:
    return extents.has_data


def _can_explode(entity: ezdxf.entities.DXFEntity) -> bool:
    return entity.dxftype() in EXPLODE_TYPES and callable(getattr(entity, "explode", None))


def explode_modelspace(modelspace: ezdxf.layouts.Modelspace) -> tuple[int, int, list[str]]:
    exploded_count = 0
    created_count = 0
    warnings: list[str] = []

    for _ in range(MAX_EXPLODE_PASSES):
        candidates = [
            entity
            for entity in list(modelspace)
            if entity.is_alive and _can_explode(entity)
        ]
        if not candidates:
            break

        exploded_this_pass = 0
        for entity in candidates:
            if not entity.is_alive:
                continue
            try:
                parts = entity.explode()
            except Exception as exc:
                warnings.append(f"{entity.dxftype()}: {exc}")
                continue
            exploded_count += 1
            exploded_this_pass += 1
            created_count += len(parts)

        if exploded_this_pass == 0:
            break
    else:
        warnings.append(f"Stopped exploding after {MAX_EXPLODE_PASSES} passes")

    modelspace.purge()
    return exploded_count, created_count, warnings


def _quantize(value: float, tolerance: float = OVERKILL_TOLERANCE) -> int:
    return int(round(float(value) / tolerance))


def _quantized_vec3(value: Vec3 | tuple[float, ...]) -> tuple[int, int, int]:
    vec = Vec3(value)
    return (_quantize(vec.x), _quantize(vec.y), _quantize(vec.z))


def _normalized_pair(
    first: tuple[int, ...], second: tuple[int, ...]
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    return (first, second) if first <= second else (second, first)


def _angle_key(value: float) -> int:
    return _quantize(float(value) % 360.0)


def _property_key(entity: ezdxf.entities.DXFEntity) -> tuple[object, ...]:
    dxf = entity.dxf
    return (
        getattr(dxf, "layer", None),
        getattr(dxf, "color", None),
        getattr(dxf, "true_color", None),
        getattr(dxf, "linetype", None),
        getattr(dxf, "lineweight", None),
    )


def _polyline_key(entity: ezdxf.entities.DXFEntity) -> tuple[object, ...] | None:
    if entity.dxftype() == "LWPOLYLINE":
        points = tuple(
            tuple(_quantize(value) for value in point)
            for point in entity.get_points("xyseb")
        )
        return ("LWPOLYLINE", bool(entity.closed), points)

    if entity.dxftype() == "POLYLINE" and hasattr(entity, "points"):
        points = tuple(_quantized_vec3(point) for point in entity.points())
        return ("POLYLINE", bool(entity.is_closed), points)

    return None


def _entity_overkill_key(entity: ezdxf.entities.DXFEntity) -> tuple[object, ...] | None:
    dxftype = entity.dxftype()
    props = _property_key(entity)

    if dxftype == "LINE":
        start = _quantized_vec3(entity.dxf.start)
        end = _quantized_vec3(entity.dxf.end)
        return (dxftype, props, _normalized_pair(start, end))

    if dxftype == "CIRCLE":
        return (
            dxftype,
            props,
            _quantized_vec3(entity.dxf.center),
            _quantize(entity.dxf.radius),
            _quantized_vec3(getattr(entity.dxf, "extrusion", (0, 0, 1))),
        )

    if dxftype == "ARC":
        return (
            dxftype,
            props,
            _quantized_vec3(entity.dxf.center),
            _quantize(entity.dxf.radius),
            _angle_key(entity.dxf.start_angle),
            _angle_key(entity.dxf.end_angle),
            _quantized_vec3(getattr(entity.dxf, "extrusion", (0, 0, 1))),
        )

    if dxftype == "ELLIPSE":
        return (
            dxftype,
            props,
            _quantized_vec3(entity.dxf.center),
            _quantized_vec3(entity.dxf.major_axis),
            _quantize(entity.dxf.ratio),
            _quantize(entity.dxf.start_param),
            _quantize(entity.dxf.end_param),
            _quantized_vec3(getattr(entity.dxf, "extrusion", (0, 0, 1))),
        )

    if dxftype == "POINT":
        return (dxftype, props, _quantized_vec3(entity.dxf.location))

    polyline_key = _polyline_key(entity)
    if polyline_key:
        return (props, polyline_key)

    return None


def overkill_modelspace(modelspace: ezdxf.layouts.Modelspace) -> int:
    seen: dict[tuple[object, ...], ezdxf.entities.DXFEntity] = {}
    duplicate_count = 0

    for entity in list(modelspace):
        if not entity.is_alive:
            continue
        key = _entity_overkill_key(entity)
        if key is None:
            continue
        existing = seen.get(key)
        if existing is not None and existing.is_alive:
            modelspace.delete_entity(entity)
            duplicate_count += 1
        else:
            seen[key] = entity

    modelspace.purge()
    return duplicate_count


def _write_modelspace_extents(doc: ezdxf.document.Drawing, extents: bbox.BoundingBox) -> None:
    modelspace = doc.modelspace()
    modelspace.dxf.extmin = extents.extmin
    modelspace.dxf.extmax = extents.extmax
    modelspace.dxf.limmin = (extents.extmin.x, extents.extmin.y)
    modelspace.dxf.limmax = (extents.extmax.x, extents.extmax.y)
    doc.header["$EXTMIN"] = extents.extmin
    doc.header["$EXTMAX"] = extents.extmax
    doc.header["$LIMMIN"] = (extents.extmin.x, extents.extmin.y)
    doc.header["$LIMMAX"] = (extents.extmax.x, extents.extmax.y)


def _ensure_layer(doc: ezdxf.document.Drawing, layer_name: str) -> None:
    if layer_name not in doc.layers:
        doc.layers.add(layer_name)


def _combined_label_height(items: list[CombineItem]) -> float:
    largest_dimension = max(max(item.width, item.height) for item in items)
    return max(10.0, min(largest_dimension * 0.05, 100.0))


def _combined_gap(items: list[CombineItem], label_height: float) -> float:
    largest_dimension = max(max(item.width, item.height) for item in items)
    return max(label_height * 3.0, largest_dimension * 0.15, 50.0)


def _combined_columns(item_count: int, layout_mode: str) -> int:
    if layout_mode == COMBINED_LAYOUT_SINGLE_ROW:
        return max(1, item_count)
    return max(1, math.ceil(item_count ** 0.5))


def _collect_combine_items(results: list[ConversionResult]) -> tuple[list[CombineItem], list[str]]:
    items: list[CombineItem] = []
    warnings: list[str] = []

    for result in results:
        if result.status != "converted" or result.output is None:
            continue
        try:
            doc = ezdxf.readfile(result.output)
            entities = list(doc.modelspace())
            extents = _bbox_for_entities(entities)
        except Exception as exc:
            warnings.append(f"{result.source.name}: {exc}")
            continue
        if not _has_extents(extents):
            warnings.append(f"{result.source.name}: no measurable extents")
            continue

        size = extents.size
        items.append(
            CombineItem(
                label=result.source.name,
                path=result.output,
                extmin=extents.extmin,
                width=max(float(size.x), 1.0),
                height=max(float(size.y), 1.0),
            )
        )

    return items, warnings


def _layout_combine_items(
    items: list[CombineItem], layout_mode: str, gap: float, label_clearance: float
) -> list[tuple[CombineItem, float, float]]:
    columns = _combined_columns(len(items), layout_mode)
    rows = (len(items) + columns - 1) // columns

    column_widths = [0.0 for _ in range(columns)]
    row_heights = [0.0 for _ in range(rows)]
    for index, item in enumerate(items):
        row = index // columns
        column = index % columns
        column_widths[column] = max(column_widths[column], item.width)
        row_heights[row] = max(row_heights[row], item.height)

    x_offsets: list[float] = []
    x_cursor = 0.0
    for width in column_widths:
        x_offsets.append(x_cursor)
        x_cursor += width + gap

    y_offsets: list[float] = []
    y_cursor = 0.0
    for height in row_heights:
        y_offsets.append(-y_cursor)
        y_cursor += height + label_clearance + gap

    placements: list[tuple[CombineItem, float, float]] = []
    for index, item in enumerate(items):
        row = index // columns
        column = index % columns
        placements.append((item, x_offsets[column], y_offsets[row]))
    return placements


def build_combined_dxf(
    results: list[ConversionResult], output_folder: Path, layout_mode: str
) -> CombinedResult:
    items, item_warnings = _collect_combine_items(results)
    if not items:
        return CombinedResult(
            status="skipped",
            message="No successfully converted DXFs were available to combine",
        )

    combined_folder = output_folder / COMBINED_FOLDER_NAME
    combined_path = combined_folder / COMBINED_FILE_NAME

    try:
        combined_doc = ezdxf.new("R2010")
        combined_doc.units = units.MM
        combined_msp = combined_doc.modelspace()
        _ensure_layer(combined_doc, COMBINED_LABEL_LAYER)

        label_height = _combined_label_height(items)
        label_gap = label_height * 0.8
        label_clearance = label_height + label_gap
        gap = _combined_gap(items, label_height)
        placements = _layout_combine_items(items, layout_mode, gap, label_clearance)

        imported_count = 0
        import_warnings: list[str] = []
        for item, slot_x, slot_y in placements:
            source_doc = ezdxf.readfile(item.path)
            source_entities = list(source_doc.modelspace())
            before_count = len(combined_msp)

            importer = Importer(source_doc, combined_doc)
            importer.import_entities(source_entities, combined_msp)
            importer.finalize()

            imported_entities = list(combined_msp)[before_count:]
            if not imported_entities:
                import_warnings.append(f"{item.label}: no supported entities imported")
                continue

            move = Matrix44.translate(slot_x - item.extmin.x, slot_y - item.extmin.y, 0.0)
            move_log = inplace(imported_entities, move)
            imported_count += len(imported_entities)
            if len(move_log):
                import_warnings.append(f"{item.label}: {len(move_log)} transform warning(s)")

            text = combined_msp.add_text(
                item.label,
                height=label_height,
                dxfattribs={"layer": COMBINED_LABEL_LAYER},
            )
            text.set_placement(
                (slot_x + item.width / 2.0, slot_y + item.height + label_gap, 0.0),
                align=TextEntityAlignment.BOTTOM_CENTER,
            )

        if imported_count == 0:
            return CombinedResult(
                status="failed",
                message="Combined DXF could not import any supported entities",
                item_count=len(items),
            )

        combined_extents = _bbox_for_entities(list(combined_msp))
        if _has_extents(combined_extents):
            inplace(
                list(combined_msp),
                Matrix44.translate(-combined_extents.extmin.x, -combined_extents.extmin.y, 0.0),
            )
            combined_extents = _bbox_for_entities(list(combined_msp))
            _write_modelspace_extents(combined_doc, combined_extents)
            zoom.extents(combined_msp, factor=1.0)

        combined_folder.mkdir(parents=True, exist_ok=True)
        combined_doc.saveas(combined_path)

        warning_count = len(item_warnings) + len(import_warnings)
        message = f"Combined {len(items)} file(s)"
        if warning_count:
            message += f" with {warning_count} warning(s)"
        return CombinedResult(
            status="created",
            message=message,
            output=combined_path,
            item_count=len(items),
        )
    except Exception as exc:
        return CombinedResult(
            status="failed",
            message=f"Combined DXF failed: {exc}",
            output=combined_path,
            item_count=len(items),
        )


def process_dxf_sources(
    sources: list[Path],
    output_folder: Path,
    combined_layout: str,
    progress=None,
) -> tuple[list[ConversionResult], CombinedResult]:
    results: list[ConversionResult] = []

    for index, source in enumerate(sources, start=1):
        if progress is not None:
            progress.progress((index - 1) / len(sources), text=f"Processing {source.name}")
        results.append(convert_dxf(source, output_folder))

    if progress is not None:
        progress.progress(1.0, text="Building combined DXF")
    combined_result = build_combined_dxf(results, output_folder, combined_layout)
    if progress is not None:
        progress.progress(1.0, text="Conversion finished")

    return results, combined_result


def output_folder_zip_bytes(output_folder: Path) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(output_folder.rglob("*.dxf")):
            archive.write(path, path.relative_to(output_folder).as_posix())
    return buffer.getvalue()


def render_results(
    results: list[ConversionResult],
    combined_result: CombinedResult,
    show_full_output_path: bool = True,
) -> None:
    converted = sum(1 for result in results if result.status == "converted")
    skipped = sum(1 for result in results if result.status == "skipped")
    failed = sum(1 for result in results if result.status == "failed")

    col_a, col_b, col_c = st.columns(3)
    col_a.metric("Converted", converted)
    col_b.metric("Skipped", skipped)
    col_c.metric("Failed", failed)

    if combined_result.status == "created" and combined_result.output is not None:
        if show_full_output_path:
            st.success(f"{combined_result.message}: {combined_result.output}")
        else:
            st.success(f"{combined_result.message}; included in the download ZIP")
    elif combined_result.status == "skipped":
        st.warning(combined_result.message)
    else:
        st.error(combined_result.message)

    st.subheader("Results")
    st.dataframe(
        result_rows(results, show_full_output_path),
        use_container_width=True,
        hide_index=True,
    )


def convert_dxf(source: Path, output_folder: Path) -> ConversionResult:
    try:
        doc = ezdxf.readfile(source)
    except Exception as exc:
        return ConversionResult(source=source, status="failed", message=f"Read failed: {exc}")

    modelspace = doc.modelspace()
    initial_entity_count = len(modelspace)
    if initial_entity_count == 0:
        return ConversionResult(source=source, status="skipped", message="Modelspace is empty")

    try:
        exploded_count, _, explode_warnings = explode_modelspace(modelspace)
        duplicate_count = overkill_modelspace(modelspace)
        entities = list(modelspace)
        if not entities:
            return ConversionResult(
                source=source,
                status="skipped",
                message="Modelspace is empty after explode/overkill cleanup",
                initial_entity_count=initial_entity_count,
                exploded_count=exploded_count,
                duplicate_count=duplicate_count,
            )

        upgraded_for_units = doc.dxfversion < ezdxf.DXF2000
        if upgraded_for_units:
            doc.dxfversion = ezdxf.DXF2000
        doc.units = units.MM

        scale_log = inplace(entities, Matrix44.scale(SCALE_FACTOR, SCALE_FACTOR, SCALE_FACTOR))
        scaled_extents = _bbox_for_entities(entities)
        if not _has_extents(scaled_extents):
            return ConversionResult(
                source=source,
                status="skipped",
                message="No measurable modelspace extents after scaling",
                initial_entity_count=initial_entity_count,
                final_entity_count=len(entities),
                exploded_count=exploded_count,
                duplicate_count=duplicate_count,
            )

        min_x = scaled_extents.extmin.x
        min_y = scaled_extents.extmin.y
        move_log = inplace(entities, Matrix44.translate(-min_x, -min_y, 0.0))

        final_extents = _bbox_for_entities(entities)
        if not _has_extents(final_extents):
            return ConversionResult(
                source=source,
                status="skipped",
                message="No measurable modelspace extents after moving to origin",
                initial_entity_count=initial_entity_count,
                final_entity_count=len(entities),
                exploded_count=exploded_count,
                duplicate_count=duplicate_count,
            )

        _write_modelspace_extents(doc, final_extents)
        zoom.extents(modelspace, factor=1.0)

        output_folder.mkdir(parents=True, exist_ok=True)
        output_path = next_output_path(output_folder, source.name)
        doc.saveas(output_path)

        warnings = len(scale_log) + len(move_log)
        message_parts = ["Converted"]
        if exploded_count:
            message_parts.append(f"exploded {exploded_count} entity/entities")
        if duplicate_count:
            message_parts.append(f"overkill removed {duplicate_count} duplicate(s)")
        if upgraded_for_units:
            message_parts.append("saved as DXF R2000 for millimeter units")
        if explode_warnings:
            message_parts.append(f"{len(explode_warnings)} explode warning(s)")
        if warnings:
            message_parts.append(f"{warnings} transform warning(s)")
        message = "; ".join(message_parts)

        return ConversionResult(
            source=source,
            status="converted",
            message=message,
            output=output_path,
            initial_entity_count=initial_entity_count,
            final_entity_count=len(entities),
            exploded_count=exploded_count,
            duplicate_count=duplicate_count,
        )
    except Exception as exc:
        return ConversionResult(
            source=source,
            status="failed",
            message=f"Conversion failed: {exc}",
            initial_entity_count=initial_entity_count,
            final_entity_count=len(modelspace),
        )


def result_rows(
    results: list[ConversionResult], show_full_output_path: bool = True
) -> list[dict[str, str | int]]:
    return [
        {
            "File": result.source.name,
            "Status": result.status,
            "Start entities": result.initial_entity_count,
            "Final entities": result.final_entity_count,
            "Exploded": result.exploded_count,
            "Overkill removed": result.duplicate_count,
            "Output": (
                str(result.output)
                if show_full_output_path and result.output
                else result.output.name
                if result.output
                else ""
            ),
            "Message": result.message,
        }
        for result in results
    ]


def render_app() -> None:
    st.set_page_config(page_title="DXF Millimeter Batch Converter", layout="wide")

    st.title("DXF Millimeter Batch Converter")
    st.caption("Batch-convert top-level DXF files for laser-cut workflows.")

    with st.sidebar:
        st.header("Mode")
        mode = st.radio(
            "Input source",
            ["Upload files", "Local folders"],
            index=0,
        )
        st.header("Combined DXF")
        combined_layout = st.radio(
            "Layout",
            [COMBINED_LAYOUT_AUTO_GRID, COMBINED_LAYOUT_SINGLE_ROW],
            horizontal=True,
        )

    left, right = st.columns([2, 1])
    with left:
        st.subheader("Batch")
        st.write(
            "The app explodes modelspace blocks and compound entities, removes exact "
            "duplicate geometry, sets DXF units to millimeters, scales modelspace by "
            "2, moves the final lower-left extents to (0, 0), and saves the AutoCAD "
            "view zoomed to the converted extents."
        )
    with right:
        st.subheader("Settings")
        st.metric("Scale", "2x")
        st.metric("Layouts", "Modelspace only")
        st.metric("Existing files", "Add suffix")
        st.metric("Cleanup", "Explode + overkill")
        st.metric("Combined output", rf"{COMBINED_FOLDER_NAME}\{COMBINED_FILE_NAME}")

    if mode == "Upload files":
        uploaded_files = st.file_uploader(
            "Upload DXF files",
            type=["dxf"],
            accept_multiple_files=True,
        )
        run_clicked = st.button("Convert uploaded DXF files", type="primary")

        if run_clicked:
            if not uploaded_files:
                st.error("Upload at least one DXF file.")
                return

            with TemporaryDirectory() as temp_dir:
                temp_root = Path(temp_dir)
                input_folder = temp_root / "input"
                output_folder = temp_root / "output"
                input_folder.mkdir(parents=True, exist_ok=True)

                uploaded_paths: list[Path] = []
                for uploaded_file in uploaded_files:
                    source_path = next_input_path(input_folder, uploaded_file.name)
                    source_path.write_bytes(uploaded_file.getbuffer())
                    uploaded_paths.append(source_path)

                sources = sorted(uploaded_paths, key=lambda path: path.name.lower())
                progress = st.progress(0, text="Starting conversion")
                results, combined_result = process_dxf_sources(
                    sources, output_folder, combined_layout, progress
                )
                zip_bytes = output_folder_zip_bytes(output_folder)

            st.session_state["last_results"] = results
            st.session_state["last_combined_result"] = combined_result
            st.session_state["last_zip_bytes"] = zip_bytes

        if "last_results" in st.session_state and "last_combined_result" in st.session_state:
            render_results(
                st.session_state["last_results"],
                st.session_state["last_combined_result"],
                show_full_output_path=False,
            )

        if "last_zip_bytes" in st.session_state:
            st.download_button(
                "Download converted DXFs",
                data=st.session_state["last_zip_bytes"],
                file_name="laser_cut_converted_dxf.zip",
                mime="application/zip",
                type="primary",
            )
        return

    with st.sidebar:
        st.header("Folders")
        input_value = st.text_input("Input folder", placeholder=r"C:\path\to\input")
        output_value = st.text_input("Output folder", placeholder=r"C:\path\to\output")
        run_clicked = st.button("Convert DXF files", type="primary", use_container_width=True)

    input_folder = Path(input_value.strip('" ')) if input_value.strip() else None
    output_folder = Path(output_value.strip('" ')) if output_value.strip() else None

    if input_folder and input_folder.exists() and input_folder.is_dir():
        files = discover_dxf_files(input_folder)
        st.info(f"Found {len(files)} top-level DXF file(s).")
    elif input_folder:
        files = []
        st.warning("Input folder does not exist or is not a folder.")
    else:
        files = []

    if not run_clicked:
        return

    if input_folder is None or output_folder is None:
        st.error("Choose both an input folder and an output folder.")
        return
    if not input_folder.exists() or not input_folder.is_dir():
        st.error("Input folder does not exist or is not a folder.")
        return
    if not files:
        st.warning("No top-level DXF files were found in the input folder.")
        return

    progress = st.progress(0, text="Starting conversion")
    results, combined_result = process_dxf_sources(files, output_folder, combined_layout, progress)
    render_results(results, combined_result)


if __name__ == "__main__":
    render_app()
