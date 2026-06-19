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

import streamlit as st
import pandas as pd
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
        {"name": "Boxes 1", "l": 500, "w": 400, "h": 300, "wt": 10, "qty": 80, "color": "#2ecc71", "cargo_type": "General Cargo"},
        {"name": "Sacks", "l": 1000, "w": 450, "h": 300, "wt": 45, "qty": 100, "color": "#9b59b6", "cargo_type": "General Cargo"},
        {"name": "Big bags", "l": 1000, "w": 1000, "h": 1000, "wt": 900, "qty": 10, "color": "#3498db", "cargo_type": "General Cargo"}
    ]
# Force product row widgets to refresh when imported data changes
if 'product_list_version' not in st.session_state:
    st.session_state.product_list_version = 0

# Standard container dimension catalog for calculations
CONTAINER_DICT = {
    "20' Standard": {"l": 5898, "w": 2352, "h": 2393, "m": 28000},
    "40' Standard": {"l": 12032, "w": 2352, "h": 2393, "m": 28000},
    "40' High-Cube (40HQ)": {"l": 12032, "w": 2352, "h": 2698, "m": 28000},
    "45' High-Cube": {"l": 13556, "w": 2352, "h": 2698, "m": 29000}
}

# Keep the selected container across screens
if 'selected_container' not in st.session_state:
    st.session_state.selected_container = "40' High-Cube (40HQ)"
if 'selected_container_quantity' not in st.session_state:
    st.session_state.selected_container_quantity = 1
if 'calculation_requested' not in st.session_state:
    st.session_state.calculation_requested = False
if 'load_direction' not in st.session_state:
    st.session_state.load_direction = "inside_out"
if 'heavy_priority' not in st.session_state:
    st.session_state.heavy_priority = "heavy_bottom"
if 'placement_strategy' not in st.session_state:
    st.session_state.placement_strategy = "stable_floor_first"
if 'max_additional_containers' not in st.session_state:
    st.session_state.max_additional_containers = 10
if 'contact_compaction' not in st.session_state:
    st.session_state.contact_compaction = True
if 'minimum_support_ratio' not in st.session_state:
    st.session_state.minimum_support_ratio = 0.65


@st.cache_data(show_spinner=False)
def calculate_loading_cached(products, selected_container, custom_dims, selected_quantity, loading_config):
    items = product_rows_to_cargo_items(products)
    spec = get_container_spec(selected_container, custom_dims)
    try:
        return optimize_loading(
            items,
            spec,
            selected_quantity=selected_quantity,
            allow_auto_add=True,
            config=loading_config,
        )
    except TypeError as exc:
        if "config" not in str(exc):
            raise
        return optimize_loading(items, spec, selected_quantity=selected_quantity, allow_auto_add=True)


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
        html.append(
            "<tr style='border-bottom:1px solid #f1f5f9;'>"
            f"<td style='padding:8px;'><span style='display:inline-block;width:18px;height:18px;border-radius:4px;background:{color};border:1px solid #94a3b8;'></span></td>"
            f"<td style='padding:8px; font-weight:600;'>{row['Name']}</td>"
            f"<td style='padding:8px; text-align:right;'>{row['Packages']}</td>"
            f"<td style='padding:8px; text-align:right;'>{row['Volume (m3)']:.3f}</td>"
            f"<td style='padding:8px; text-align:right;'>{row['Weight (kg)']:.2f}</td>"
            "</tr>"
        )
    html.append("</tbody></table>")
    st.markdown("".join(html), unsafe_allow_html=True)


def make_loading_config(**kwargs):
    fields = getattr(LoadingConfig, "__dataclass_fields__", {})
    if fields:
        kwargs = {key: value for key, value in kwargs.items() if key in fields}
    return LoadingConfig(**kwargs)

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
        prefixes = ("name_", "type_", "l_", "w_", "h_", "wt_", "qty_", "color_", "del_")
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
    col_h1, col_h_type, col_h2, col_h3, col_h4, col_h5, col_h6, col_h7, col_h8 = st.columns([2.3, 1.35, 1.05, 1.05, 1.05, 1.05, 1, 0.85, 0.5])
    with col_h1: st.markdown("**Product Name**")
    with col_h_type: st.markdown("**Cargo Type**")
    with col_h2: st.markdown("**Length**")
    with col_h3: st.markdown("**Width**")
    with col_h4: st.markdown("**Height**")
    with col_h5: st.markdown("**Weight (kg)**")
    with col_h6: st.markdown("**Quantity**")
    with col_h7: st.markdown("**Color**")
    with col_h8: st.markdown("**Del**")

    # Update product rows while keeping Streamlit state stable
    temp_list = []
    to_delete = None
    
    product_key_version = st.session_state.product_list_version
    for i, prod in enumerate(st.session_state.product_list):
        cols = st.columns([2.3, 1.35, 1.05, 1.05, 1.05, 1.05, 1, 0.85, 0.5])
        name = cols[0].text_input("", value=prod["name"], key=f"name_{product_key_version}_{i}", label_visibility="collapsed")
        cargo_type_options = ["General Cargo", "Lumber Bundle"]
        current_cargo_type = prod.get("cargo_type", "General Cargo")
        cargo_type = cols[1].selectbox("", cargo_type_options, index=cargo_type_options.index(current_cargo_type) if current_cargo_type in cargo_type_options else 0, key=f"type_{product_key_version}_{i}", label_visibility="collapsed")
        dim_step = 1 if cargo_type == "Lumber Bundle" else 10
        l = cols[2].number_input("", value=prod["l"], step=dim_step, key=f"l_{product_key_version}_{i}", label_visibility="collapsed")
        w = cols[3].number_input("", value=prod["w"], step=dim_step, key=f"w_{product_key_version}_{i}", label_visibility="collapsed")
        h = cols[4].number_input("", value=prod["h"], step=dim_step, key=f"h_{product_key_version}_{i}", label_visibility="collapsed")
        wt = cols[5].number_input("", value=prod["wt"], step=1, key=f"wt_{product_key_version}_{i}", label_visibility="collapsed")
        qty = cols[6].number_input("", value=prod["qty"], step=1, key=f"qty_{product_key_version}_{i}", label_visibility="collapsed")
        color = cols[7].color_picker("", value=prod["color"], key=f"color_{product_key_version}_{i}", label_visibility="collapsed")
        
        if cols[8].button("Delete", key=f"del_{product_key_version}_{i}"):
            to_delete = i
            
        temp_list.append({"name": name, "l": l, "w": w, "h": h, "wt": wt, "qty": qty, "color": color, "cargo_type": cargo_type})

    if to_delete is not None:
        temp_list.pop(to_delete)
        clear_product_input_state()
        st.session_state.product_list = temp_list
        st.session_state.product_list_version += 1
        st.rerun()
    else:
        st.session_state.product_list = temp_list

    st.write("")
    add_cols = st.columns([1.2, 1.5, 1.5, 6])
    with add_cols[0]:
        if st.button("Add Product"):
            st.session_state.product_list.append({"name": "New Item", "l": 500, "w": 400, "h": 300, "wt": 10, "qty": 1, "color": "#e74c3c", "cargo_type": "General Cargo"})
            st.session_state.product_list_version += 1
            st.rerun()
    with add_cols[1]:
        if st.button("Add Lumber Bundle"):
            st.session_state.product_list.append({"name": "Lumber Bundle", "l": 96, "w": 12, "h": 12, "wt": 35, "qty": 10, "color": "#8e5a2a", "cargo_type": "Lumber Bundle"})
            st.session_state.product_list_version += 1
            st.rerun()
    with add_cols[2]:
        if st.button("Reset All"):
            st.session_state.product_list = [
                {"name": "Boxes 1", "l": 500, "w": 400, "h": 300, "wt": 10, "qty": 80, "color": "#2ecc71", "cargo_type": "General Cargo"},
                {"name": "Sacks", "l": 1000, "w": 450, "h": 300, "wt": 45, "qty": 100, "color": "#9b59b6", "cargo_type": "General Cargo"},
                {"name": "Big bags", "l": 1000, "w": 1000, "h": 1000, "wt": 900, "qty": 10, "color": "#3498db", "cargo_type": "General Cargo"}
            ]
            st.session_state.product_list_version += 1
            st.session_state.calculation_requested = False
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
    
    container_options = list(CONTAINER_DICT.keys()) + ["Custom"]
    selected_container_index = container_options.index(st.session_state.selected_container) if st.session_state.selected_container in container_options else 0
    st.session_state.selected_container = st.selectbox(
        "Select the target container type:",
        container_options,
        index=selected_container_index
    )
    
    if st.session_state.selected_container == "Custom":
        if 'custom_dims' not in st.session_state:
            st.session_state.custom_dims = {"l": 6000, "w": 2400, "h": 2400, "m": 25000}
        st.session_state.custom_dims["l"] = st.number_input("Custom length (mm)", value=st.session_state.custom_dims["l"])
        st.session_state.custom_dims["w"] = st.number_input("Custom width (mm)", value=st.session_state.custom_dims["w"])
        st.session_state.custom_dims["h"] = st.number_input("Custom height (mm)", value=st.session_state.custom_dims["h"])
        st.session_state.custom_dims["m"] = st.number_input("Custom max payload (kg)", value=st.session_state.custom_dims["m"])

    st.session_state.selected_container_quantity = st.number_input(
        "Number of selected containers:",
        min_value=1,
        max_value=20,
        value=int(st.session_state.selected_container_quantity),
        step=1
    )

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
    with config_cols[2]:
        strategy_labels = {
            "stable_floor_first": "Length first",
            "fill_width_before_length": "Width first"
        }
        strategy_options = list(strategy_labels.keys())
        st.session_state.placement_strategy = st.selectbox(
            "Fill strategy",
            strategy_options,
            index=strategy_options.index(st.session_state.placement_strategy),
            format_func=lambda value: strategy_labels[value],
        )
    with config_cols[3]:
        st.session_state.max_additional_containers = st.number_input(
            "Max auto containers",
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

    custom_dims = st.session_state.get("custom_dims", {"l": 6000, "w": 2400, "h": 2400, "m": 25000})
    loading_config = make_loading_config(
        load_direction=st.session_state.load_direction,
        heavy_priority=st.session_state.heavy_priority,
        placement_strategy=st.session_state.placement_strategy,
        max_additional_containers=int(st.session_state.max_additional_containers),
        minimum_support_ratio=float(st.session_state.minimum_support_ratio),
        contact_compaction=bool(st.session_state.contact_compaction),
    )

    try:
        with st.spinner("Calculating optimized loading plan..."):
            loading_plan = calculate_loading_cached(
                st.session_state.product_list,
                st.session_state.selected_container,
                custom_dims,
                int(st.session_state.selected_container_quantity),
                loading_config
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
                st.cache_data.clear()
                st.session_state.current_tab = "PRODUCTS"
                st.rerun()
        st.stop()

    summary_df = container_summary_df(loading_plan)
    detail_df = detail_plan_df(loading_plan)

    metric_cols = st.columns(5)
    metric_cols[0].metric("Containers Used", len(loading_plan.containers))
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
    for index, container in enumerate(loading_plan.containers, start=1):
        title = (
            f"Container {index}: {container.spec.name} - {container.package_count} packages, "
            f"{container.volume_pct:.1f}% volume, {container.weight_pct:.1f}% weight"
        )
        with st.expander(title, expanded=index == 1):
            st.plotly_chart(build_container_figure(container), use_container_width=True)
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
            st.cache_data.clear()
            st.session_state.current_tab = "PRODUCTS"
            st.rerun()







