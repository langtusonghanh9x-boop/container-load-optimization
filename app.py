import hashlib
import re
import sys
import types
from dataclasses import dataclass
from pathlib import Path

# GitHub web upload can accidentally flatten package folders. If the engine
# files are in the repository root, expose them as a package without writing files.
APP_DIR = Path(__file__).resolve().parent
ENGINE_DIR = APP_DIR / "container_optimizer"
ENGINE_FILES = [
    "__init__.py",
    "cargo.py",
    "containers.py",
    "manager.py",
    "models.py",
    "optimization.py",
    "packing.py",
    "reporting.py",
    "visualization.py",
]
if not ENGINE_DIR.exists() and all((APP_DIR / filename).exists() for filename in ENGINE_FILES):
    package = types.ModuleType("container_optimizer")
    package.__path__ = [str(APP_DIR)]
    sys.modules["container_optimizer"] = package

import io
import zipfile
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter
from reportlab.lib.utils import ImageReader
import pandas as pd
import streamlit as st
import json
from container_optimizer.cargo import product_rows_to_cargo_items
from container_optimizer.containers import get_container_spec
import container_optimizer.models as optimizer_models

if not hasattr(optimizer_models, "LoadingConfig"):
    @dataclass(frozen=True)
    class LoadingConfig:
        load_direction: str = "inside_out"
        heavy_priority: str = "heavy_bottom"
        placement_strategy: str = "stable_floor_first"
        max_additional_containers: int = 10
        minimum_support_ratio: float = 0.65
        contact_compaction: bool = True

    optimizer_models.LoadingConfig = LoadingConfig
else:
    LoadingConfig = optimizer_models.LoadingConfig

from container_optimizer.optimization import optimize_loading
from container_optimizer.reporting import container_summary_df, detail_plan_df, summarize_container
from container_optimizer.visualization import build_container_figure

# --- PAGE CONFIG ---
st.set_page_config(page_title="Container Load 3D Optimization", layout="wide")

# 1. Manage current tab state
if 'current_tab' not in st.session_state:
    st.session_state.current_tab = "PRODUCTS"

# 2. Initialize default product list
if 'product_list' not in st.session_state:
    st.session_state.product_list = [
        {"name": "Boxes 1", "l": 0, "w": 0, "h": 0, "wt": 0, "qty": 80, "color": "#2ecc71", "cargo_type": "General Cargo"},
        {"name": "Sacks", "l": 0, "w": 0, "h": 0, "wt": 0, "qty": 100, "color": "#9b59b6", "cargo_type": "General Cargo"},
        {"name": "Big bags", "l": 0, "w": 0, "h": 0, "wt": 0, "qty": 10, "color": "#3498db", "cargo_type": "General Cargo"}
    ]
# Force product row widgets to refresh when imported data changes
if 'product_list_version' not in st.session_state:
    st.session_state.product_list_version = 0

# Standard container dimension catalog for calculations
CONTAINER_DICT = {
    "20' Standard": {"l": 5898, "w": 2352, "h": 2393, "m": 28000},
    "40' Standard": {"l": 12032, "w": 2352, "h": 2393, "m": 28000},
    "40' High-Cube (40HQ)": {"l": 12032, "w": 2352, "h": 2698, "m": 28000},
    "45' High-Cube": {"l": 13556, "w": 2352, "h": 2698, "m": 29000},
    "Truck 1.5T": {"l": 3100, "w": 1600, "h": 1700, "m": 1500},
    "Truck 2T": {"l": 3800, "w": 1800, "h": 1850, "m": 2000},
    "Truck 2.5T": {"l": 4300, "w": 1900, "h": 1950, "m": 2500},
    "Truck 3.5T": {"l": 4300, "w": 2000, "h": 2000, "m": 3500},
    "Truck 5T": {"l": 6200, "w": 2300, "h": 2300, "m": 5000},
    "Truck 8T": {"l": 8000, "w": 2350, "h": 2400, "m": 8000},
    "Truck 10T": {"l": 9500, "w": 2400, "h": 2500, "m": 10000},
    "Truck 15T": {"l": 12000, "w": 2400, "h": 2600, "m": 15000},
}

# Keep the selected container across screens
if 'selected_container' not in st.session_state:
    st.session_state.selected_container = "40' High-Cube (40HQ)"
if 'selected_container_quantity' not in st.session_state:
    st.session_state.selected_container_quantity = 1
if 'calculation_requested' not in st.session_state:
    st.session_state.calculation_requested = False
if 'recalculate_loading' not in st.session_state:
    st.session_state.recalculate_loading = False
if 'load_direction' not in st.session_state:
    st.session_state.load_direction = "inside_out"
if 'heavy_priority' not in st.session_state:
    st.session_state.heavy_priority = "heavy_bottom"
    if 'placement_strategy' not in st.session_state:
        st.session_state.placement_strategy = "stable_floor_first"
    # Second container selection (optional)
    if 'selected_container2' not in st.session_state:
        st.session_state.selected_container2 = ""
    if 'selected_container2_quantity' not in st.session_state:
        st.session_state.selected_container2_quantity = 1

if 'max_additional_containers' not in st.session_state:
    st.session_state.max_additional_containers = 10
if 'contact_compaction' not in st.session_state:
    st.session_state.contact_compaction = True

# Load persisted custom containers configuration
if 'custom_containers' not in st.session_state:
    try:
        with open('custom_containers.json', 'r') as f:
            st.session_state.custom_containers = json.load(f)
    except FileNotFoundError:
        st.session_state.custom_containers = []
# Version counter for custom container UI keys
if 'custom_container_version' not in st.session_state:
    st.session_state.custom_container_version = 0
if 'minimum_support_ratio' not in st.session_state:
    st.session_state.minimum_support_ratio = 0.65


@st.cache_data(show_spinner=False)
def calculate_loading_cached(products, selected_containers, custom_dimensions, selected_quantity, loading_config):
    items = product_rows_to_cargo_items(products)
    specs = [
        get_container_spec(name, custom_dimensions.get(name))
        for name in selected_containers
    ]
    try:
        return optimize_loading(
            items,
            specs,
            selected_quantity=selected_quantity,
            allow_auto_add=True,
            config=loading_config,
        )
    except TypeError as exc:
        if "config" not in str(exc):
            raise
        return optimize_loading(items, specs, selected_quantity=selected_quantity, allow_auto_add=True)


def render_color_summary_table(rows):
    if not rows:
        st.info("No cargo in this container.")
        return

    html = [
        "<table style='width:100%; border-collapse:collapse; font-size:14px;'>",
        "<thead><tr style='border-bottom:1px solid #e5e7eb;'>"
        "<th style='text-align:left; padding:8px;'>Color</th>"
        "<th style='text-align:left; padding:8px;'>Name</th>"
        "<th style='text-align:right; padding:8px;'>Packages</th>"
        "<th style='text-align:right; padding:8px;'>Volume (m3)</th>"
        "<th style='text-align:right; padding:8px;'>Weight (kg)</th>"
        "</tr></thead><tbody>"
    ]
    for row in rows:
        color = row.get("Color", "#7f8c8d")
        color_name = color_name_bilingual(color)
        html.append(
            "<tr style='border-bottom:1px solid #f1f5f9;'>"
            f"<td style='padding:8px; white-space:nowrap;'><span style='display:inline-block;width:18px;height:18px;border-radius:4px;background:{color};border:1px solid #94a3b8;vertical-align:middle;'></span><span style='margin-left:8px;'>{color_name}</span></td>"
            f"<td style='padding:8px; font-weight:600;'>{row['Name']}</td>"
            f"<td style='padding:8px; text-align:right;'>{row['Packages']}</td>"
            f"<td style='padding:8px; text-align:right;'>{row['Volume (m3)']:.3f}</td>"
            f"<td style='padding:8px; text-align:right;'>{row['Weight (kg)']:.2f}</td>"
            "</tr>"
        )
    html.append("</tbody></table>")
    st.markdown("".join(html), unsafe_allow_html=True)


def color_name_bilingual(hex_color):
    """Return the closest common colour name in English with a Vietnamese subtitle."""
    named_colors = {
        "Black (Đen)": (0, 0, 0),
        "White (Trắng)": (255, 255, 255),
        "Gray (Xám)": (127, 140, 141),
        "Red (Đỏ)": (231, 76, 60),
        "Orange (Cam)": (230, 126, 34),
        "Yellow (Vàng)": (241, 196, 15),
        "Green (Xanh lá)": (46, 204, 113),
        "Cyan (Xanh ngọc)": (26, 188, 156),
        "Blue (Xanh dương)": (52, 152, 219),
        "Purple (Tím)": (155, 89, 182),
        "Pink (Hồng)": (236, 112, 193),
        "Brown (Nâu)": (142, 90, 42),
    }
    clean = str(hex_color).lstrip("#")
    if len(clean) != 6:
        return "Color (Màu)"
    try:
        rgb = tuple(int(clean[index:index + 2], 16) for index in (0, 2, 4))
    except ValueError:
        return "Color (Màu)"
    return min(named_colors, key=lambda name: sum((rgb[i] - value) ** 2 for i, value in enumerate(named_colors[name])))


def make_loading_config(**kwargs):
    fields = getattr(LoadingConfig, "__dataclass_fields__", {})
    if fields:
        kwargs = {key: value for key, value in kwargs.items() if key in fields}
    return LoadingConfig(**kwargs)


def export_plotly_png(fig):
    """Return a PNG when Kaleido/Chrome is available; keep the app usable when it is not."""
    try:
        return fig.to_image(format="png")
    except Exception:
        return None


def safe_download_name(value):
    """Create a portable filename stem from a container or truck name."""
    return re.sub(r"[^A-Za-z0-9_-]+", "_", value).strip("_") or "loading_result"


def build_3d_html(containers, title):
    """Create one self-contained interactive HTML file for one or more vehicles."""
    sections = []
    for index, packed_container in enumerate(containers):
        figure = build_container_figure(packed_container)
        graph_html = figure.to_html(full_html=False, include_plotlyjs=index == 0)
        sections.append(
            f"<section><h2>{packed_container.spec.name} {index + 1}</h2>{graph_html}</section>"
        )
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{title}</title>"
        "<style>body{font-family:Arial,sans-serif;margin:24px;color:#172033;}"
        "section{margin:0 0 32px;}h1{margin-bottom:28px;}h2{font-size:18px;}</style>"
        f"</head><body><h1>{title}</h1>{''.join(sections)}</body></html>"
    ).encode("utf-8")


def build_loading_pdf(containers, title):
    """Create a concise PDF report, with a 3D preview when image export is available."""
    pdf_buffer = io.BytesIO()
    report = canvas.Canvas(pdf_buffer, pagesize=letter)
    page_width, page_height = letter
    for index, packed_container in enumerate(containers, start=1):
        report.setTitle(title)
        report.setFont("Helvetica-Bold", 16)
        report.drawString(42, page_height - 48, title)
        report.setFont("Helvetica-Bold", 13)
        report.drawString(42, page_height - 76, f"{index}. {packed_container.spec.name}")
        report.setFont("Helvetica", 10)
        report.drawString(42, page_height - 94, f"Packages: {packed_container.package_count}")
        report.drawString(42, page_height - 109, f"Volume used: {packed_container.cargo_volume_m3:.3f} m3 ({packed_container.volume_pct:.1f}%)")
        report.drawString(42, page_height - 124, f"Weight used: {packed_container.cargo_weight_kg:.1f} kg ({packed_container.weight_pct:.1f}%)")

        png_data = export_plotly_png(build_container_figure(packed_container))
        if png_data is not None:
            report.drawImage(
                ImageReader(io.BytesIO(png_data)), 42, 360,
                width=528, height=200, preserveAspectRatio=True, anchor='c', mask='auto'
            )
        else:
            report.setFont("Helvetica-Oblique", 10)
            report.drawString(42, 540, "Interactive 3D model is included in the accompanying HTML file.")

        table_y = 335
        report.setFont("Helvetica-Bold", 10)
        report.drawString(42, table_y, "Cargo summary")
        report.setFont("Helvetica-Bold", 8)
        report.drawString(42, table_y - 14, "Name")
        report.drawRightString(330, table_y - 14, "Packages")
        report.drawRightString(445, table_y - 14, "Volume (m3)")
        report.drawRightString(570, table_y - 14, "Weight (kg)")
        row_y = table_y - 29
        report.setFont("Helvetica", 8)
        for row in summarize_container(packed_container)[:8]:
            report.drawString(42, row_y, str(row["Name"])[:42])
            report.drawRightString(330, row_y, str(row["Packages"]))
            report.drawRightString(445, row_y, f"{row['Volume (m3)']:.3f}")
            report.drawRightString(570, row_y, f"{row['Weight (kg)']:.1f}")
            row_y -= 14

        report.showPage()
    report.save()
    return pdf_buffer.getvalue()


def build_download_bundle(containers, title, filename_stem):
    """Bundle one 3D HTML model and one matching PDF into a ZIP download."""
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr(f"{filename_stem}_3d.html", build_3d_html(containers, title))
        bundle.writestr(f"{filename_stem}.pdf", build_loading_pdf(containers, title))
    return archive.getvalue()


CARGO_TYPE_OPTIONS = ["Box", "Big Bags", "Sacks", "Barrels", "Roll", "Pipes", "Bulk"]


@st.dialog("Add Product", width="large")
def show_add_product_dialog():
    st.caption("1. SELECT CARGO TYPE")
    cargo_type = st.radio(
        "Cargo type",
        CARGO_TYPE_OPTIONS,
        horizontal=True,
        label_visibility="collapsed",
        key="new_product_cargo_type",
    )

    st.caption("2. SELECT CARGO DIMENSIONS")
    product_cols = st.columns(3)
    new_name = product_cols[0].text_input("Product Name", value="New Product", key="new_product_name")
    new_color = product_cols[1].color_picker("Color", value="#8e43d6", key="new_product_color")
    new_qty = product_cols[2].number_input("Quantity", min_value=1, value=1, step=1, key="new_product_qty")

    if cargo_type in ("Barrels", "Roll", "Pipes"):
        dimension_cols = st.columns(3)
        diameter = dimension_cols[0].number_input("Diameter (mm)", min_value=0.0, value=0.0, step=10.0, key="new_product_diameter")
        cylinder_height = dimension_cols[1].number_input("Height (mm)", min_value=0.0, value=0.0, step=10.0, key="new_product_cylinder_height")
        new_length, new_width, new_height = cylinder_height, diameter, diameter
    else:
        dimension_cols = st.columns(3)
        new_length = dimension_cols[0].number_input("Length (mm)", min_value=0.0, value=0.0, step=10.0, key="new_product_length")
        new_width = dimension_cols[1].number_input("Width (mm)", min_value=0.0, value=0.0, step=10.0, key="new_product_width")
        new_height = dimension_cols[2].number_input("Height (mm)", min_value=0.0, value=0.0, step=10.0, key="new_product_height")
    new_weight = st.number_input("Weight (kg)", min_value=0.0, value=0.0, step=1.0, key="new_product_weight")

    settings_cols = st.columns(2)
    with settings_cols[0]:
        st.caption("3. SPACING SETTINGS")
        tilt_to_length = st.checkbox("Tilt to Length", key="new_product_tilt_length")
        tilt_to_width = st.checkbox("Tilt to Width", key="new_product_tilt_width")
    with settings_cols[1]:
        st.caption("4. STUFFING SETTINGS")
        enable_layers = st.checkbox("Layers Count", key="new_product_enable_layers")
        max_layers = st.number_input("Maximum layers", min_value=1, max_value=100, value=1, step=1, disabled=not enable_layers, key="new_product_max_layers")
        enable_mass = st.checkbox("Mass", key="new_product_enable_mass")
        max_stack_mass = st.number_input("Maximum load on this product (kg)", min_value=0.0, value=0.0, step=1.0, disabled=not enable_mass, key="new_product_max_mass")
        enable_height = st.checkbox("Height", key="new_product_enable_height")
        max_stack_height = st.number_input("Maximum stack height (mm)", min_value=1.0, value=1000.0, step=10.0, disabled=not enable_height, key="new_product_max_height")
        disable_stacking = st.checkbox("Disable stacking", key="new_product_disable_stacking")

    action_cols = st.columns(2)
    if action_cols[0].button("Cancel", use_container_width=True, key="new_product_cancel"):
        st.rerun()
    if action_cols[1].button("Add", type="primary", use_container_width=True, key="new_product_add"):
        st.session_state.product_list.append({
            "name": new_name or "New Product",
            "l": new_length,
            "w": new_width,
            "h": new_height,
            "wt": new_weight,
            "qty": new_qty,
            "color": new_color,
            "cargo_type": cargo_type,
            "shape": "cylinder" if cargo_type in ("Barrels", "Roll", "Pipes") else "box",
            "loading_order": None,
            "tilt_to_length": tilt_to_length,
            "tilt_to_width": tilt_to_width,
            "max_layers": int(max_layers) if enable_layers else None,
            "max_stack_mass_kg": max_stack_mass if enable_mass else None,
            "max_stack_height_mm": max_stack_height if enable_height else None,
            "disable_stacking": disable_stacking,
        })
        st.session_state.product_list_version += 1
        st.rerun()

# --- CLICKABLE TAB NAVIGATION ---
nav_cols = st.columns(3)
with nav_cols[0]:
    if st.button("PRODUCTS", use_container_width=True, type="primary" if st.session_state.current_tab == "PRODUCTS" else "secondary"):
        st.session_state.current_tab = "PRODUCTS"
        st.rerun()
with nav_cols[1]:
    if st.button("CONTAINERS & TRUCKS", use_container_width=True, type="primary" if st.session_state.current_tab == "CONTAINERS & TRUCKS" else "secondary"):
        st.session_state.current_tab = "CONTAINERS & TRUCKS"
        st.rerun()
with nav_cols[2]:
    if st.button("STUFFING RESULT", use_container_width=True, type="primary" if st.session_state.current_tab == "STUFFING RESULT" else "secondary"):
        st.session_state.current_tab = "STUFFING RESULT"
        st.rerun()

st.write("")

# ==========================================
# SCREEN 1: PRODUCTS (DATA ENTRY & IMPORT)
# ==========================================
if st.session_state.current_tab == "PRODUCTS":
    st.subheader("Step 1: Manage Product List")
    
    col_top1, col_top2, col_top3 = st.columns([2, 5, 3])


    with col_top1:
        st.button(
            "Add Group",
            type="primary"
        )

    with col_top3:
        uploaded_file = st.file_uploader(
            "Import Data File (Excel/CSV)",
            type=["csv", "xlsx", "xls"],
            label_visibility="collapsed"
        )

    def normalize_column_name(value):
        if pd.isna(value):
            return ""
        return " ".join(str(value).strip().upper().replace("\n", " ").split())

    COLUMN_ALIASES = {
        "Product Name": ["PRODUCT NAME", "DESCRIPTION", "DESC", "ITEM NAME"],
        "Quantity": ["QUANTITY", "QTY", "TOTAL CARTON"],
        "Length": ["LENGTH (MM)", "LENGTH", "LEN", "L (MM)"],
        "Width": ["WIDTH (MM)", "WIDTH", "WID", "W (MM)"],
        "Height": ["HEIGHT (MM)", "HEIGHT", "HEI", "H (MM)"],
        "Weight": ["WEIGHT (KG)", "GROSS WEIGHT", "G.W", "GW", "NET WEIGHT", "N.W", "NW", "WEIGHT", "WT"]
    }

    def find_column(columns, possible_names):
        normalized_columns = {col: normalize_column_name(col) for col in columns}
        normalized_names = [normalize_column_name(name) for name in possible_names]

        for expected in normalized_names:
            for original, normalized in normalized_columns.items():
                if normalized == expected:
                    return original

        for expected in normalized_names:
            for original, normalized in normalized_columns.items():
                if expected and expected in normalized:
                    return original

        return None

    def score_header_values(values):
        score = 0
        normalized_values = [normalize_column_name(value) for value in values]
        for aliases in COLUMN_ALIASES.values():
            if find_column(normalized_values, aliases) is not None:
                score += 1
        return score

    def make_unique_columns(values):
        columns = []
        counts = {}
        for index, value in enumerate(values):
            name = str(value).strip() if not pd.isna(value) else ""
            if not name or name.upper() == "NAN":
                name = f"Unnamed {index}"
            if name in counts:
                counts[name] += 1
                name = f"{name}.{counts[name]}"
            else:
                counts[name] = 0
            columns.append(name)
        return columns

    def dataframe_from_detected_header(raw_df):
        raw_df = raw_df.dropna(how="all").dropna(axis=1, how="all")
        if raw_df.empty:
            return raw_df

        best_row = 0
        best_score = -1
        for row_index in range(min(len(raw_df), 30)):
            score = score_header_values(raw_df.iloc[row_index].tolist())
            if score > best_score:
                best_score = score
                best_row = row_index

        if best_score < 3:
            return raw_df

        df = raw_df.iloc[best_row + 1:].copy()
        df.columns = make_unique_columns(raw_df.iloc[best_row].tolist())
        return df.dropna(how="all")

    def score_product_table(df):
        if df.empty:
            return -1

        product_col = find_column(df.columns, COLUMN_ALIASES["Product Name"])
        length_col = find_column(df.columns, COLUMN_ALIASES["Length"])
        width_col = find_column(df.columns, COLUMN_ALIASES["Width"])
        height_col = find_column(df.columns, COLUMN_ALIASES["Height"])
        weight_col = find_column(df.columns, COLUMN_ALIASES["Weight"])
        quantity_col = find_column(df.columns, COLUMN_ALIASES["Quantity"])
        header_score = sum(col is not None for col in [product_col, length_col, width_col, height_col, weight_col, quantity_col])

        if product_col is None:
            return header_score

        product_values = [str(value).strip() for value in df[product_col].dropna().head(20)]
        avg_name_length = sum(len(value) for value in product_values) / max(len(product_values), 1)
        row_score = min(len(product_values), 20)
        return header_score * 100 + avg_name_length + row_score

    def load_product_file(file):
        if file.name.lower().endswith(".csv"):
            raw_df = pd.read_csv(file, header=None)
            return dataframe_from_detected_header(raw_df)

        sheets = pd.read_excel(file, header=None, sheet_name=None)
        best_df = None
        best_score = -1
        for raw_df in sheets.values():
            candidate = dataframe_from_detected_header(raw_df)
            score = score_product_table(candidate)
            if score > best_score:
                best_score = score
                best_df = candidate

        return best_df if best_df is not None else pd.DataFrame()
    def clear_product_input_state():
        prefixes = ("name_", "type_", "l_", "w_", "h_", "wt_", "qty_", "color_", "order_", "del_")
        for key in list(st.session_state.keys()):
            if key.startswith(prefixes):
                del st.session_state[key]

    IMPORT_CODE_VERSION = "product-import-v10"

    if uploaded_file is not None:
        file_hash = hashlib.sha256(uploaded_file.getvalue()).hexdigest()
        import_key = f"{file_hash}:{IMPORT_CODE_VERSION}"

        if st.session_state.get("last_import_file_hash") != import_key:
            try:
                uploaded_file.seek(0)
                df = load_product_file(uploaded_file)
                df = df.dropna(how="all").dropna(axis=1, how="all")

                col_desc = find_column(df.columns, COLUMN_ALIASES["Product Name"])
                col_len = find_column(df.columns, COLUMN_ALIASES["Length"])
                col_wid = find_column(df.columns, COLUMN_ALIASES["Width"])
                col_hei = find_column(df.columns, COLUMN_ALIASES["Height"])
                col_wt = find_column(df.columns, COLUMN_ALIASES["Weight"])
                col_qty = find_column(df.columns, COLUMN_ALIASES["Quantity"])

                def average_text_length(column):
                    values = []
                    for value in df[column].dropna().head(20):
                        text = str(value).strip()
                        if text and not re.fullmatch(r"-?\d+(?:\.\d+)?", text.replace(",", "")):
                            values.append(text)
                    return sum(len(value) for value in values) / max(len(values), 1)

                if col_desc is not None:
                    current_name_score = average_text_length(col_desc)
                    best_name_col = col_desc
                    best_name_score = current_name_score
                    for candidate_col in df.columns:
                        candidate_score = average_text_length(candidate_col)
                        if candidate_score > best_name_score + 5:
                            best_name_score = candidate_score
                            best_name_col = candidate_col
                    col_desc = best_name_col

                if col_wt in df.columns:
                    weight_index = list(df.columns).index(col_wt)
                    if weight_index + 1 < len(df.columns):
                        next_col = df.columns[weight_index + 1]
                        if normalize_column_name(next_col) == "QUANTITY" or col_qty is None:
                            col_qty = next_col

                required = {
                    "Product Name": col_desc,
                    "Quantity": col_qty,
                    "Length": col_len,
                    "Width": col_wid,
                    "Height": col_hei,
                    "Weight": col_wt
                }
                missing = [key for key, value in required.items() if value is None]

                if missing:
                    raise ValueError(f"Missing required columns: {', '.join(missing)}")

                df = df.dropna(how="all")
                imported_products = []
                colors = [
                    "#2ecc71",
                    "#9b59b6",
                    "#3498db",
                    "#e74c3c",
                    "#f1c40f",
                    "#1abc9c"
                ]

                def is_blank(value):
                    return pd.isna(value) or str(value).strip() == ""

                def parse_number(value, default=1):
                    if pd.isna(value):
                        return default
                    if isinstance(value, (int, float)):
                        return int(value) if value > 0 else default

                    text = str(value).strip().replace(",", "")
                    match = re.search(r"-?\d+(?:\.\d+)?", text)
                    if not match:
                        return default

                    number = float(match.group(0))
                    return int(number) if number > 0 else default

                data_columns = [col_desc, col_len, col_wid, col_hei, col_wt, col_qty]

                for idx, row in df.iterrows():
                    if all(is_blank(row[column]) for column in data_columns):
                        continue

                    raw_name = row[col_desc]
                    product_name = "" if is_blank(raw_name) else str(raw_name).strip()
                    if not product_name:
                        product_name = f"Imported row {len(imported_products) + 1}"

                    imported_products.append({
                        "name": product_name,
                        "l": parse_number(row[col_len]),
                        "w": parse_number(row[col_wid]),
                        "h": parse_number(row[col_hei]),
                        "wt": parse_number(row[col_wt]),
                        "qty": parse_number(row[col_qty]),
                        "color": colors[len(imported_products) % len(colors)],
                        "cargo_type": "General Cargo"
                    })
                if not imported_products:
                    st.warning("No valid product rows were found in the uploaded file.")
                else:
                    clear_product_input_state()
                    st.session_state.product_list = imported_products
                    st.session_state.product_list_version += 1
                    st.session_state.last_import_file_hash = import_key
                    st.session_state.import_success_message = "Created"
                    st.rerun()

            except Exception as e:
                st.warning(f"Could not import this file: {e}")

    if st.session_state.get("import_success_message"):
        st.success(st.session_state.import_success_message)
    col_h1, col_h_type, col_h2, col_h3, col_h4, col_h5, col_h6, col_h7, col_h8, col_h9 = st.columns([2.3, 1.35, 1.05, 1.05, 1.05, 1.05, 1, 0.85, 0.7, 0.5])
    with col_h1: st.markdown("**Product Name**")
    with col_h_type: st.markdown("**Cargo Type**")
    with col_h2: st.markdown("**Length**")
    with col_h3: st.markdown("**Width**")
    with col_h4: st.markdown("**Height**")
    with col_h5: st.markdown("**Weight (kg)**")
    with col_h6: st.markdown("**Quantity**")
    with col_h7: st.markdown("**Color**")
    with col_h8: st.markdown("**Order (Inside → Out)**")
    with col_h9: st.markdown("**Del**")

    # Update product rows while keeping Streamlit state stable
    temp_list = []
    to_delete = None
    invalid_order_rows = []
    
    product_key_version = st.session_state.product_list_version
    for i, prod in enumerate(st.session_state.product_list):
        cols = st.columns([2.3, 1.35, 1.05, 1.05, 1.05, 1.05, 1, 0.85, 0.7, 0.5])
        name = cols[0].text_input("", value=prod["name"], key=f"name_{product_key_version}_{i}", label_visibility="collapsed")
        cargo_type_options = ["General Cargo", "Lumber Bundle", *CARGO_TYPE_OPTIONS]
        current_cargo_type = prod.get("cargo_type", "General Cargo")
        cargo_type = cols[1].selectbox("", cargo_type_options, index=cargo_type_options.index(current_cargo_type) if current_cargo_type in cargo_type_options else 0, key=f"type_{product_key_version}_{i}", label_visibility="collapsed")
        dim_step = 1 if cargo_type == "Lumber Bundle" else 10
        l = cols[2].number_input("", value=float(prod["l"]), step=float(dim_step), key=f"l_{product_key_version}_{i}", label_visibility="collapsed")
        w = cols[3].number_input("", value=float(prod["w"]), step=float(dim_step), key=f"w_{product_key_version}_{i}", label_visibility="collapsed")
        h = cols[4].number_input("", value=float(prod["h"]), step=float(dim_step), key=f"h_{product_key_version}_{i}", label_visibility="collapsed")
        wt = cols[5].number_input("", value=float(prod["wt"]), step=1.0, key=f"wt_{product_key_version}_{i}", label_visibility="collapsed")
        qty = cols[6].number_input("", value=int(prod["qty"]), step=1, key=f"qty_{product_key_version}_{i}", label_visibility="collapsed")
        color = cols[7].color_picker("", value=prod["color"], key=f"color_{product_key_version}_{i}", label_visibility="collapsed")
        order_text = cols[8].text_input(
            "",
            value="" if prod.get("loading_order") is None else str(prod["loading_order"]),
            max_chars=3,
            placeholder="1–100",
            key=f"order_{product_key_version}_{i}",
            label_visibility="collapsed",
        )
        loading_order = int(order_text) if order_text.isdigit() and 1 <= int(order_text) <= 100 else None
        if order_text and loading_order is None:
            invalid_order_rows.append(i + 1)
        
        if cols[9].button("🗑️", key=f"del_{product_key_version}_{i}"):
            to_delete = i
            
        temp_list.append({
            "name": name,
            "l": l,
            "w": w,
            "h": h,
            "wt": wt,
            "qty": qty,
            "color": color,
            "cargo_type": cargo_type,
            "shape": prod.get("shape", "box"),
            "loading_order": loading_order,
            "tilt_to_length": prod.get("tilt_to_length", False),
            "tilt_to_width": prod.get("tilt_to_width", False),
            "max_layers": prod.get("max_layers"),
            "max_stack_mass_kg": prod.get("max_stack_mass_kg"),
            "max_stack_height_mm": prod.get("max_stack_height_mm"),
            "disable_stacking": prod.get("disable_stacking", False),
        })

    if to_delete is not None:
        temp_list.pop(to_delete)
        clear_product_input_state()
        st.session_state.product_list = temp_list
        st.session_state.product_list_version += 1
        st.rerun()
    else:
        st.session_state.product_list = temp_list

    if invalid_order_rows:
        st.warning("Order must be a whole number from 1 to 100. Invalid values are ignored.")

    st.write("")
    add_cols = st.columns([1.2, 1.5, 1.5, 6])
    with add_cols[0]:
        if st.button("Add Product"):
            show_add_product_dialog()
    with add_cols[1]:
        if st.button("Add Lumber Bundle"):
            st.session_state.product_list.append({"name": "Lumber Bundle", "l": 96, "w": 12, "h": 12, "wt": 35, "qty": 10, "color": "#8e5a2a", "cargo_type": "Lumber Bundle"})
            st.session_state.product_list_version += 1
            st.rerun()
    with add_cols[2]:
        if st.button("Reset All"):
            # Reset product list to defaults
            st.session_state.product_list = [
                {"name": "Boxes 1", "l": 0, "w": 0, "h": 0, "wt": 0, "qty": 80, "color": "#2ecc71", "cargo_type": "General Cargo"},
                {"name": "Sacks", "l": 0, "w": 0, "h": 0, "wt": 0, "qty": 100, "color": "#9b59b6", "cargo_type": "General Cargo"},
                {"name": "Big bags", "l": 0, "w": 0, "h": 0, "wt": 0, "qty": 10, "color": "#3498db", "cargo_type": "General Cargo"},
            ]
            st.session_state.product_list_version += 1
            # Reset all calculation related state
            st.session_state.calculation_requested = False
            st.session_state.recalculate_loading = False
            st.session_state.selected_container_quantity = 1
            st.session_state.loading_plans = {}
            st.session_state.variant_idx = 0
            st.session_state.selected_strategies = []
            st.session_state.max_additional_containers = 10
            st.session_state.contact_compaction = True
            st.session_state.minimum_support_ratio = 0.65
            # Clear only the loading calculation cache; avoid the global cache confirmation.
            try:
                calculate_loading_cached.clear()
            except Exception:
                pass
            clear_product_input_state()
            st.rerun()

    st.caption("General Cargo dimensions use mm. Lumber Bundle dimensions use inch and are converted to mm automatically.")

    # Move from step 1 to step 2
    st.write("---")
    if st.button("Next", type="primary"):
        st.session_state.current_tab = "CONTAINERS & TRUCKS"
        st.rerun()


# ==========================================
# SCREEN 2: CONTAINERS & TRUCKS
# ==========================================
elif st.session_state.current_tab == "CONTAINERS & TRUCKS":
    st.subheader("Step 2: Select Container / Truck")
    
    container_options = list(CONTAINER_DICT.keys()) + [c.get("name", f"Custom {i+1}") for i, c in enumerate(st.session_state.custom_containers)]
    custom_by_name = {item.get("name"): item for item in st.session_state.custom_containers}

    def is_truck_option(name):
        custom = custom_by_name.get(name, {})
        return "truck" in name.lower() or "truck" in str(custom.get("kind", "")).lower()

    selected_before = st.session_state.get("selected_containers", [st.session_state.selected_container])
    current_is_truck = is_truck_option(selected_before[0]) if selected_before else is_truck_option(st.session_state.selected_container)
    vehicle_type = st.radio(
        "Vehicle type",
        ["Containers", "Trucks"],
        index=1 if current_is_truck else 0,
        horizontal=True,
    )
    selecting_trucks = vehicle_type == "Trucks"
    vehicle_options = [name for name in container_options if is_truck_option(name) == selecting_trucks]
    defaults = [name for name in selected_before if name in vehicle_options]
    if not defaults and st.session_state.selected_container in vehicle_options:
        defaults = [st.session_state.selected_container]
    selected_vehicles = st.multiselect(
        f"Select one or more {'trucks' if selecting_trucks else 'containers'} for this calculation:",
        vehicle_options,
        default=defaults,
        help="Select two or more vehicles to calculate one combined loading plan.",
    )
    if not selected_vehicles:
        st.info("Select at least one container or truck to configure the loading plan.")
        st.stop()
    st.session_state.selected_container = selected_vehicles[0]
    st.session_state.selected_containers = selected_vehicles

    # Upload custom containers configuration (JSON)
    uploaded_containers_file = st.file_uploader(
        "Import custom trucks / containers (JSON)",
        type=["json"],
        label_visibility="collapsed"
    )
    if uploaded_containers_file is not None:
        try:
            uploaded_bytes = uploaded_containers_file.read()
            # Decode as UTF-8 and parse JSON
            custom_containers_data = json.loads(uploaded_bytes.decode("utf-8"))
            if isinstance(custom_containers_data, list):
                # Validate required keys in each container definition
                required_keys = {"name", "l", "w", "h", "m"}
                valid = all(required_keys.issubset(set(cont.keys())) for cont in custom_containers_data)
                if valid:
                    st.session_state.custom_containers = custom_containers_data
                    # Persist to file
                    with open('custom_containers.json', 'w') as f:
                        json.dump(st.session_state.custom_containers, f, indent=2)
                    st.success("Custom containers imported successfully.")
                    # Reset selection to first imported container
                    if st.session_state.custom_containers:
                        st.session_state.selected_container = st.session_state.custom_containers[0]["name"]
                    else:
                        st.session_state.selected_container = list(CONTAINER_DICT.keys())[0]
                    st.rerun()
                else:
                    st.warning("Invalid container format. Each item must include name, l, w, h, m.")
            else:
                st.warning("Uploaded JSON must be a list of container objects.")
        except Exception as e:
            st.warning(f"Failed to import custom containers: {e}")
    
    # If a custom truck or container is selected, show its editable fields.
    if st.session_state.selected_container not in CONTAINER_DICT:
        # Find the custom container entry
        custom_idx = next((i for i, c in enumerate(st.session_state.custom_containers) 
                         if c.get("name") == st.session_state.selected_container), None)
        if custom_idx is not None:
            cont = st.session_state.custom_containers[custom_idx]
            edit_cols = st.columns([1.5, 1.2, 1.2, 1.2, 1.2, 0.5])
            edit_name = edit_cols[0].text_input(
                "Name", value=cont.get("name", f"Custom {custom_idx+1}"),
                key=f"edit_name_{custom_idx}"
            )
            edit_l = edit_cols[1].number_input(
                "Length (mm)", value=cont.get("l", 6000), step=10,
                key=f"edit_l_{custom_idx}"
            )
            edit_w = edit_cols[2].number_input(
                "Width (mm)", value=cont.get("w", 2400), step=10,
                key=f"edit_w_{custom_idx}"
            )
            edit_h = edit_cols[3].number_input(
                "Height (mm)", value=cont.get("h", 2400), step=10,
                key=f"edit_h_{custom_idx}"
            )
            edit_m = edit_cols[4].number_input(
                "Max payload (kg)", value=cont.get("m", 25000), step=10,
                key=f"edit_m_{custom_idx}"
            )
            # Delete button
            if edit_cols[5].button("🗑️", key=f"del_{custom_idx}"):
                st.session_state.custom_containers.pop(custom_idx)
                with open('custom_containers.json', 'w') as f:
                    json.dump(st.session_state.custom_containers, f, indent=2)
                st.success("Custom truck / container deleted.")
                # Reset selection to first option
                if st.session_state.custom_containers:
                    st.session_state.selected_container = st.session_state.custom_containers[0]["name"]
                else:
                    st.session_state.selected_container = list(CONTAINER_DICT.keys())[0]
                st.rerun()
            # Save button
            if st.button("Save Truck / Container", type="primary"):
                st.session_state.custom_containers[custom_idx] = {
                    "name": edit_name,
                    "l": edit_l,
                    "w": edit_w,
                    "h": edit_h,
                    "m": edit_m,
                    "kind": "Truck / Custom"
                }
                with open('custom_containers.json', 'w') as f:
                    json.dump(st.session_state.custom_containers, f, indent=2)
                st.session_state.selected_container = edit_name
                st.session_state.selected_containers = [edit_name]
                calculate_loading_cached.clear()
                st.success("Custom truck / container saved and added to calculations.")
                st.rerun()
    # Add a draft vehicle. It is persisted only after the user clicks Save.
    if st.button("Add Truck / Custom Vehicle"):
        new_index = len(st.session_state.custom_containers) + 1
        existing_names = {container.get("name") for container in st.session_state.custom_containers}
        new_name = f"Custom Truck {new_index}"
        while new_name in existing_names:
            new_index += 1
            new_name = f"Custom Truck {new_index}"
        st.session_state.custom_containers.append({
            "name": new_name,
            "l": 6000,
            "w": 2400,
            "h": 2400,
            "m": 25000,
            "kind": "Truck / Custom"
        })
        # Automatically select the draft so the user can enter dimensions, then save.
        st.session_state.selected_container = new_name
        st.session_state.selected_containers = [new_name]
        st.rerun()



    # Determine custom dimensions based on selected container
    if st.session_state.selected_container in CONTAINER_DICT:
        st.session_state.custom_dims = {}
    else:
        # Look for a matching custom container by name
        match = next((c for c in st.session_state.custom_containers if c.get('name') == st.session_state.selected_container), None)
        if match:
            st.session_state.custom_dims = match
        else:
            st.session_state.custom_dims = {"l": 6000, "w": 2400, "h": 2400, "m": 25000}

    st.session_state.custom_dimensions = {
        item["name"]: item
        for item in st.session_state.custom_containers
        if item.get("name") in st.session_state.selected_containers
    }


    st.write("---")
    st.subheader("Loading Configuration")
    config_cols = st.columns(6)
    with config_cols[0]:
        direction_labels = {
            "inside_out": "Inside to door",
            "door_to_inside": "Door to inside"
        }
        direction_options = list(direction_labels.keys())
        st.session_state.load_direction = st.selectbox(
            "Loading direction",
            direction_options,
            index=direction_options.index(st.session_state.load_direction),
            format_func=lambda value: direction_labels[value],
            help="Inside to door means cargo is positioned from the front/deep end toward the container door."
        )
    with config_cols[1]:
        priority_labels = {
            "heavy_bottom": "Heavy bottom first",
            "large_first": "Large first"
        }
        priority_options = list(priority_labels.keys())
        st.session_state.heavy_priority = st.selectbox(
            "Weight logic",
            priority_options,
            index=priority_options.index(st.session_state.heavy_priority),
            format_func=lambda value: priority_labels[value],
        )

        # Placement strategy selector
        with config_cols[2]:
            strategy_labels = {
                "stable_floor_first": "Length first",
                "fill_width_before_length": "Width first",
            }
            strategy_options = list(strategy_labels.keys())
            st.session_state.placement_strategy = st.selectbox(
                "Fill strategy",
                strategy_options,
                index=strategy_options.index(st.session_state.placement_strategy),
                format_func=lambda value: strategy_labels[value],
            )
            # Multi‑strategy selector (compute alternative packing variants)
            default_strats = [st.session_state.placement_strategy] if st.session_state.placement_strategy in strategy_options else []
            st.session_state.selected_strategies = st.multiselect(
                "Compute alternative packing variants",
                options=strategy_options,
                default=default_strats,
                help="Select one or more placement strategies to evaluate. The app will run the optimizer for each."
            )
    
    with config_cols[3]:
        selected_vehicle_is_truck = st.session_state.selected_container.startswith("Truck ")
        auto_add_label = "Max auto trucks" if selected_vehicle_is_truck else "Max auto containers"
        st.session_state.max_additional_containers = st.number_input(
            auto_add_label,
            min_value=0,
            max_value=50,
            value=int(st.session_state.max_additional_containers),
            step=1
        )

    with config_cols[4]:
        st.session_state.contact_compaction = st.checkbox(
            "Contact compaction",
            value=bool(st.session_state.contact_compaction),
            help="Force loaded cargo to settle downward and close gaps along the loading direction."
        )
    with config_cols[5]:
        st.session_state.minimum_support_ratio = st.slider(
            "Support ratio",
            min_value=0.1,
            max_value=1.0,
            value=float(st.session_state.minimum_support_ratio),
            step=0.05,
            help="Minimum footprint overlap required before one package is considered supported by another."
        )

    # Step 2 navigation controls
    st.write("---")
    col_nav1, col_nav2, _ = st.columns([1.5, 2, 8.5])
    with col_nav1:
        if st.button("Back"):
            st.session_state.current_tab = "PRODUCTS"
            st.rerun()
    with col_nav2:
        if st.button("Calculate Loading", type="primary"):
            st.session_state.calculation_requested = True
            st.session_state.recalculate_loading = True
            st.session_state.current_tab = "STUFFING RESULT"
            st.rerun()


# ==========================================
# SCREEN 3: STUFFING RESULT (3D SIMULATION)
# ==========================================
elif st.session_state.current_tab == "STUFFING RESULT":
    st.subheader("Step 3: Loading Result")

    if not st.session_state.calculation_requested:
        st.info("Choose container settings, then click Calculate Loading to run the optimization.")
        if st.button("Back to Containers"):
            st.session_state.current_tab = "CONTAINERS & TRUCKS"
            st.rerun()
        st.stop()

    custom_dimensions = st.session_state.get("custom_dimensions", {})
# Deprecated: loading_config is now generated per selected strategy below
    try:
        with st.spinner("Calculating optimized loading plan..."):
            # Determine which strategies to compute
            strategy_labels = {
                "stable_floor_first": "Length first",
                "fill_width_before_length": "Width first"
            }
            selected_strategies = st.session_state.get('selected_strategies', [st.session_state.placement_strategy])
            if not selected_strategies:
                selected_strategies = [st.session_state.placement_strategy]
            # Determine containers to compute (list)
            containers_to_compute = st.session_state.get('selected_containers', [st.session_state.selected_container])
            if (
                'loading_plans' in st.session_state
                and not st.session_state.recalculate_loading
            ):
                selected_strategies = st.session_state.loading_plans.get('strategies', selected_strategies)
                containers_to_compute = st.session_state.loading_plans.get('containers', containers_to_compute)
            calculation_input = {
                "products": st.session_state.product_list,
                "containers": containers_to_compute,
                "dimensions": custom_dimensions,
                "quantity": int(st.session_state.selected_container_quantity),
                "strategies": selected_strategies,
                "load_direction": st.session_state.load_direction,
                "heavy_priority": st.session_state.heavy_priority,
                "max_additional_containers": int(st.session_state.max_additional_containers),
                "minimum_support_ratio": float(st.session_state.minimum_support_ratio),
                "contact_compaction": bool(st.session_state.contact_compaction),
            }
            calculation_signature = hashlib.sha256(
                json.dumps(calculation_input, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest()
            # Keep the last calculated result while moving between screens. A new
            # calculation only starts after the user presses Calculate Loading.
            if ('loading_plans' not in st.session_state) or st.session_state.recalculate_loading:
                st.session_state.loading_plans = {
                    'strategies': selected_strategies,
                    'containers': containers_to_compute,
                    'signature': calculation_signature,
                }
                plan_id = "||".join(containers_to_compute)
                st.session_state.loading_plans['plan_id'] = plan_id
                for strat in selected_strategies:
                    config = make_loading_config(
                        load_direction=st.session_state.load_direction,
                        heavy_priority=st.session_state.heavy_priority,
                        placement_strategy=strat,
                        max_additional_containers=int(st.session_state.max_additional_containers),
                        minimum_support_ratio=float(st.session_state.minimum_support_ratio),
                        contact_compaction=bool(st.session_state.contact_compaction),
                    )
                    try:
                        plan = calculate_loading_cached(
                            st.session_state.product_list,
                            tuple(containers_to_compute),
                            custom_dimensions,
                            int(st.session_state.selected_container_quantity),
                            config,
                        )
                    except Exception as exc:
                        st.warning(f"Failed to compute loading for selected vehicles, strategy '{strat}': {exc}")
                        plan = None
                    st.session_state.loading_plans[f"{plan_id}|{strat}"] = plan
                st.session_state.recalculate_loading = False
            # Initialize variant navigation index
            if "variant_idx" not in st.session_state or st.session_state.variant_idx >= len(selected_strategies):
                st.session_state.variant_idx = 0

            # Navigation controls
            # Variant navigation UI moved to after 3D models section
            selected_variant = selected_strategies[st.session_state.variant_idx]
            # Determine which containers are available (from stored state)
            containers_list = st.session_state.loading_plans.get('containers', [st.session_state.selected_container])
        # Composite key to retrieve the correct loading plan
        selected_container_view = containers_list[0]
        plan_id = st.session_state.loading_plans.get('plan_id', "||".join(containers_list))
        plan_key = f"{plan_id}|{selected_variant}"
        loading_plan = st.session_state.loading_plans.get(plan_key)
        if loading_plan is None:
            raise ValueError("Loading plan not found for the selected configuration.")

        st.download_button(
            label="Download total",
            data=build_download_bundle(
                loading_plan.containers,
                "Total Loading Result",
                "total_loading_result",
            ),
            file_name="total_loading_result.zip",
            mime="application/zip",
        )
    except Exception as exc:
        st.warning("Could not complete the optimization with the current data.")
        st.info("Check cargo dimensions, quantities, and weights. You can also reduce the lot size or try a larger container.")
        st.caption(f"Technical detail: {exc}")
        loading_plan = None

    if loading_plan is None:
        action_cols = st.columns([1.2, 1.4, 6])
        with action_cols[0]:
            if st.button("Back", use_container_width=True):
                st.session_state.current_tab = "CONTAINERS & TRUCKS"
                st.rerun()
        with action_cols[1]:
            if st.button("Reset All", use_container_width=True):
                st.session_state.calculation_requested = False
                st.session_state.recalculate_loading = False
                st.session_state.loading_plans = {}
                calculate_loading_cached.clear()
                st.session_state.current_tab = "PRODUCTS"
                st.rerun()
        st.stop()

    summary_df = container_summary_df(loading_plan)
    detail_df = detail_plan_df(loading_plan)

    metric_cols = st.columns(5)
    selected_vehicle_is_truck = selected_container_view.startswith("Truck ")
    used_vehicle_label = "Trucks Used" if selected_vehicle_is_truck else "Containers Used"
    metric_cols[0].metric(used_vehicle_label, len(loading_plan.containers))
    metric_cols[1].metric("Loaded / Total", f"{loading_plan.loaded_count}/{loading_plan.requested_count}")
    metric_cols[2].metric("Unloaded", loading_plan.leftover_count)
    metric_cols[3].metric("Total Volume", f"{loading_plan.total_volume_m3:.2f} m3")
    metric_cols[4].metric("Total Weight", f"{loading_plan.total_weight_kg:.2f} kg")

    for message in loading_plan.suggestions:
        st.success(message)
    for message in loading_plan.warnings:
        st.warning(message)
    st.caption(
        "Loading logic: heavier cargo is inserted first to occupy lower/floor positions; "
        "the 3D view is normalized to show the selected loading direction."
    )

    st.write("---")
    st.subheader("Container Summary")
    if summary_df.empty:
        st.warning("No cargo could be loaded. Try a larger container or review oversized cargo.")
    else:
        st.dataframe(summary_df, use_container_width=True, hide_index=True)

    st.write("---")
    st.subheader("3D Loading Models")
    # Display each container in the loading plan
    for index, container in enumerate(loading_plan.containers, start=1):
        title = (
            f"Container {index}: {container.spec.name} - {container.package_count} packages, "
            f"{container.volume_pct:.1f}% volume, {container.weight_pct:.1f}% weight"
        )
        with st.expander(title, expanded=index == 1):
            fig = build_container_figure(container)
            st.plotly_chart(fig, use_container_width=True)
            st.download_button(
                label="Download",
                data=build_download_bundle(
                    [container],
                    f"Loading Result - {container.spec.name} {index}",
                    f"{safe_download_name(container.spec.name)}_{index}",
                ),
                file_name=f"{safe_download_name(container.spec.name)}_{index}_loading_result.zip",
                mime="application/zip",
            )
            rows = summarize_container(container)
            render_color_summary_table(rows)

    if loading_plan.leftover_items:
        st.write("---")
        st.subheader("Leftover Cargo")
        leftover_rows = {}
        for item in loading_plan.leftover_items:
            row = leftover_rows.setdefault(item.name, {"Name": item.name, "Packages": 0, "Volume (m3)": 0.0, "Weight (kg)": 0.0})
            row["Packages"] += 1
            row["Volume (m3)"] += item.volume_mm3 / 1e9
            row["Weight (kg)"] += item.weight_kg
        st.dataframe(
            pd.DataFrame([
                {
                    "Name": row["Name"],
                    "Packages": row["Packages"],
                    "Volume (m3)": round(row["Volume (m3)"], 3),
                    "Weight (kg)": round(row["Weight (kg)"], 2),
                }
                for row in leftover_rows.values()
            ]),
            use_container_width=True,
            hide_index=True
        )
        st.info("Suggested handling: split oversized cargo, add a specialized truck/container, or update the container catalog.")

    st.write("")
    action_cols = st.columns([1, 1.35, 1.2, 5])
    with action_cols[0]:
        if st.button("Back", use_container_width=True):
            st.session_state.current_tab = "CONTAINERS & TRUCKS"
            st.rerun()
    with action_cols[1]:
        if not detail_df.empty:
            st.download_button(
                "Download CSV",
                data=detail_df.to_csv(index=False).encode("utf-8-sig"),
                file_name="container_load_plan.csv",
                mime="text/csv",
                use_container_width=True
            )
        else:
            st.button("Download CSV", use_container_width=True, disabled=True)
    with action_cols[2]:
        if st.button("Reset All", use_container_width=True):
            st.session_state.calculation_requested = False
            st.session_state.recalculate_loading = False
            st.session_state.loading_plans = {}
            calculate_loading_cached.clear()
            st.session_state.current_tab = "PRODUCTS"
            st.rerun()







