import hashlib
import re
from decimal import Decimal
from itertools import permutations
import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from py3dbp import Packer, Bin, Item

# --- PAGE CONFIG ---
st.set_page_config(page_title="Container Load 3D Optimization", layout="wide")

# 1. Manage current tab state
if 'current_tab' not in st.session_state:
    st.session_state.current_tab = "PRODUCTS"

# 2. Initialize default product list
if 'product_list' not in st.session_state:
    st.session_state.product_list = [
        {"name": "Boxes 1", "l": 500, "w": 400, "h": 300, "wt": 10, "qty": 80, "color": "#2ecc71"},
        {"name": "Sacks", "l": 1000, "w": 450, "h": 300, "wt": 45, "qty": 100, "color": "#9b59b6"},
        {"name": "Big bags", "l": 1000, "w": 1000, "h": 1000, "wt": 900, "qty": 10, "color": "#3498db"}
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
        prefixes = ("name_", "l_", "w_", "h_", "wt_", "qty_", "color_", "del_")
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
                    st.error("Error")
                    st.stop()

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
                        "color": colors[len(imported_products) % len(colors)]
                    })
                if not imported_products:
                    st.error("Error")
                else:
                    clear_product_input_state()
                    st.session_state.product_list = imported_products
                    st.session_state.product_list_version += 1
                    st.session_state.last_import_file_hash = import_key
                    st.session_state.import_success_message = "Created"
                    st.rerun()

            except Exception as e:
                st.error("Error")

    if st.session_state.get("import_success_message"):
        st.success(st.session_state.import_success_message)
    col_h1, col_h2, col_h3, col_h4, col_h5, col_h6, col_h7, col_h8 = st.columns([2.5, 1.2, 1.2, 1.2, 1.2, 1.2, 1, 0.5])
    with col_h1: st.markdown("**Product Name**")
    with col_h2: st.markdown("**Length (mm)**")
    with col_h3: st.markdown("**Width (mm)**")
    with col_h4: st.markdown("**Height (mm)**")
    with col_h5: st.markdown("**Weight (kg)**")
    with col_h6: st.markdown("**Quantity**")
    with col_h7: st.markdown("**Color**")
    with col_h8: st.markdown("**Del**")

    # Update product rows while keeping Streamlit state stable
    temp_list = []
    to_delete = None
    
    product_key_version = st.session_state.product_list_version
    for i, prod in enumerate(st.session_state.product_list):
        cols = st.columns([2.5, 1.2, 1.2, 1.2, 1.2, 1.2, 1, 0.5])
        name = cols[0].text_input("", value=prod["name"], key=f"name_{product_key_version}_{i}", label_visibility="collapsed")
        l = cols[1].number_input("", value=prod["l"], step=10, key=f"l_{product_key_version}_{i}", label_visibility="collapsed")
        w = cols[2].number_input("", value=prod["w"], step=10, key=f"w_{product_key_version}_{i}", label_visibility="collapsed")
        h = cols[3].number_input("", value=prod["h"], step=10, key=f"h_{product_key_version}_{i}", label_visibility="collapsed")
        wt = cols[4].number_input("", value=prod["wt"], step=1, key=f"wt_{product_key_version}_{i}", label_visibility="collapsed")
        qty = cols[5].number_input("", value=prod["qty"], step=1, key=f"qty_{product_key_version}_{i}", label_visibility="collapsed")
        color = cols[6].color_picker("", value=prod["color"], key=f"color_{product_key_version}_{i}", label_visibility="collapsed")
        
        if cols[7].button("Delete", key=f"del_{product_key_version}_{i}"):
            to_delete = i
            
        temp_list.append({"name": name, "l": l, "w": w, "h": h, "wt": wt, "qty": qty, "color": color})

    if to_delete is not None:
        temp_list.pop(to_delete)
        clear_product_input_state()
        st.session_state.product_list = temp_list
        st.session_state.product_list_version += 1
        st.rerun()
    else:
        st.session_state.product_list = temp_list

    st.write("")
    if st.button("Add Product"):
        st.session_state.product_list.append({"name": "New Item", "l": 500, "w": 400, "h": 300, "wt": 10, "qty": 1, "color": "#e74c3c"})
        st.session_state.product_list_version += 1
        st.rerun()

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

    # Step 2 navigation controls
    st.write("---")
    col_nav1, col_nav2, _ = st.columns([1.5, 2, 8.5])
    with col_nav1:
        if st.button("Back"):
            st.session_state.current_tab = "PRODUCTS"
            st.rerun()
    with col_nav2:
        if st.button("Next to Result", type="primary"):
            st.session_state.current_tab = "STUFFING RESULT"
            st.rerun()


# ==========================================
# SCREEN 3: STUFFING RESULT (3D SIMULATION)
# ==========================================
elif st.session_state.current_tab == "STUFFING RESULT":
    
    # Use the container settings selected in step 2
    c_type = st.session_state.selected_container
    if c_type != "Custom":
        c_l, c_w, c_h, c_m = CONTAINER_DICT[c_type]["l"], CONTAINER_DICT[c_type]["w"], CONTAINER_DICT[c_type]["h"], CONTAINER_DICT[c_type]["m"]
    else:
        c_l, c_w, c_h, c_m = st.session_state.custom_dims["l"], st.session_state.custom_dims["w"], st.session_state.custom_dims["h"], st.session_state.custom_dims["m"]

    def to_float(value):
        return float(value) if isinstance(value, Decimal) else float(value)

    def container_volume(dims):
        return (float(dims["l"]) * float(dims["w"]) * float(dims["h"])) / 1e9

    def item_volume(item):
        return to_float(item.width) * to_float(item.height) * to_float(item.depth)

    def clone_item(item):
        return Item(item.name, item.width, item.height, item.depth, item.weight)

    def item_base_name(item):
        return item.name.split(" #")[0]

    def can_fit_dimensions(item, dims):
        item_dims = [to_float(item.width), to_float(item.height), to_float(item.depth)]
        limit_dims = [float(dims["l"]), float(dims["w"]), float(dims["h"])]
        return any(all(rotated[index] <= limit_dims[index] for index in range(3)) for rotated in permutations(item_dims))

    def oversize_reason(item, dims):
        reasons = []
        if not can_fit_dimensions(item, dims):
            reasons.append("vượt kích thước")
        if to_float(item.weight) > float(dims["m"]):
            reasons.append("vượt tải trọng")
        return ", ".join(reasons)

    def sort_pack_items(items):
        return sorted(
            [clone_item(item) for item in items],
            key=lambda item: (to_float(item.weight), item_volume(item)),
            reverse=True
        )

    def pack_once(container_name, dims, items):
        packer = Packer()
        active = Bin(container_name, dims["l"], dims["w"], dims["h"], dims["m"])
        active.format_numbers(3)
        packer.add_bin(active)
        for item in sort_pack_items(items):
            item.format_numbers(3)
            packer.pack_to_bin(active, item)
        return active

    def pack_across_containers(container_name, dims, items):
        remaining = [clone_item(item) for item in items]
        bins = []
        while remaining:
            packed_bin = pack_once(container_name, dims, remaining)
            if not packed_bin.items:
                return None, remaining
            bins.append(packed_bin)
            fitted_names = set(item.name for item in packed_bin.items)
            remaining = [item for item in remaining if item.name not in fitted_names]
        return bins, []

    def bin_usage(packed_bin, dims):
        used_volume = sum(item_volume(item) for item in packed_bin.items) / 1e9
        used_weight = sum(to_float(item.weight) for item in packed_bin.items)
        max_volume = container_volume(dims)
        max_weight = float(dims["m"])
        return {
            "volume": used_volume,
            "weight": used_weight,
            "volume_pct": (used_volume / max_volume * 100) if max_volume else 0,
            "weight_pct": (used_weight / max_weight * 100) if max_weight else 0
        }

    def summarize_items(items):
        rows = []
        for p in st.session_state.product_list:
            fitted_count = sum(1 for idx in items if item_base_name(idx) == p["name"])
            row_volume = (float(p["l"]) * float(p["w"]) * float(p["h"]) * fitted_count) / 1e9
            row_weight = float(p["wt"]) * fitted_count
            rows.append({
                "Name": p["name"],
                "Packages": fitted_count,
                "Volume": row_volume,
                "Weight": row_weight,
                "Color": p["color"]
            })
        return rows

    selected_dims = {"l": c_l, "w": c_w, "h": c_h, "m": c_m}
    all_items = []
    for item in st.session_state.product_list:
        for q in range(int(item["qty"])):
            all_items.append(Item(f"{item['name']} #{q+1}", item["l"], item["w"], item["h"], item["wt"]))

    active_bin = pack_once(c_type, selected_dims, all_items)
    fitted_items = active_bin.items
    unfitted_items = [clone_item(item) for item in active_bin.unfitted_items]
    total_requested_packages = len(all_items)
    loaded_packages = len(fitted_items)

    primary_usage = bin_usage(active_bin, selected_dims)
    total_container_vol = container_volume(selected_dims)
    total_cargo_vol = primary_usage["volume"]
    total_cargo_weight = primary_usage["weight"]
    vol_efficiency = primary_usage["volume_pct"]
    weight_efficiency = primary_usage["weight_pct"]

    oversized_selected = [
        {"name": item.name, "reason": oversize_reason(item, selected_dims)}
        for item in unfitted_items
        if oversize_reason(item, selected_dims)
    ]

    feasible_extra_options = []
    impossible_extra_items = []
    if unfitted_items:
        for item in unfitted_items:
            fits_any_catalog = any(
                can_fit_dimensions(item, dims) and to_float(item.weight) <= float(dims["m"])
                for dims in CONTAINER_DICT.values()
            )
            if not fits_any_catalog:
                impossible_extra_items.append(item)

        if not impossible_extra_items:
            for name, dims in CONTAINER_DICT.items():
                extra_bins, extra_leftover = pack_across_containers(name, dims, unfitted_items)
                if extra_bins and not extra_leftover:
                    feasible_extra_options.append({
                        "name": name,
                        "dims": dims,
                        "bins": extra_bins,
                        "count": len(extra_bins),
                        "capacity": container_volume(dims)
                    })

    best_extra_option = None
    if feasible_extra_options:
        best_extra_option = sorted(feasible_extra_options, key=lambda option: (option["count"], option["capacity"]))[0]

    final_container_plans = [{"name": c_type, "dims": selected_dims, "bin": active_bin, "role": "Selected"}]
    if best_extra_option:
        for index, packed_bin in enumerate(best_extra_option["bins"], start=1):
            final_container_plans.append({
                "name": best_extra_option["name"],
                "dims": best_extra_option["dims"],
                "bin": packed_bin,
                "role": f"Additional {index}"
            })

    final_loaded_items = []
    for plan in final_container_plans:
        final_loaded_items.extend(plan["bin"].items)
    final_loaded_packages = len(final_loaded_items)
    final_leftover_count = total_requested_packages - final_loaded_packages

    summary_rows = summarize_items(fitted_items)
    volume_pct = vol_efficiency
    weight_pct = weight_efficiency
    loaded_rows = [row for row in summary_rows if row["Packages"] > 0]

    st.markdown(f"""
    <div style="display:flex; align-items:center; justify-content:space-between; border:1px solid #e5ebf3; border-radius:8px 8px 0 0; padding:18px 22px; background:#fff;">
        <div style="font-size:18px; font-weight:700; color:#2f3742;">{c_type.upper()}</div>
        <div style="font-size:17px; font-weight:600; color:#9aacc1;">Cargo volume: {total_cargo_vol:.2f} m3</div>
        <div style="font-size:17px; font-weight:600; color:#9aacc1;">Cargo weight: {total_cargo_weight:.2f} kg</div>
    </div>
    """, unsafe_allow_html=True)

    fig_3d = go.Figure()

    def hex_to_rgba(hex_color, opacity):
        clean = hex_color.lstrip("#")
        r, g, b = tuple(int(clean[i:i+2], 16) for i in (0, 2, 4))
        return f"rgba({r},{g},{b},{opacity})"

    def add_box_edges(fig, x, y, z, dx, dy, dz, color, width=2):
        points = [
            (x, y, z), (x+dx, y, z), (x+dx, y+dy, z), (x, y+dy, z),
            (x, y, z+dz), (x+dx, y, z+dz), (x+dx, y+dy, z+dz), (x, y+dy, z+dz)
        ]
        edges = [(0,1), (1,2), (2,3), (3,0), (4,5), (5,6), (6,7), (7,4), (0,4), (1,5), (2,6), (3,7)]
        xs, ys, zs = [], [], []
        for a, b in edges:
            xs += [points[a][0], points[b][0], None]
            ys += [points[a][1], points[b][1], None]
            zs += [points[a][2], points[b][2], None]
        fig.add_trace(go.Scatter3d(x=xs, y=ys, z=zs, mode="lines", line=dict(color=color, width=width), showlegend=False, hoverinfo="skip"))

    def draw_cube(fig, x, y, z, dx, dy, dz, color, name, opacity=1.0, edge_color="rgba(255,255,255,0.45)"):
        fig.add_trace(go.Mesh3d(
            x=[x, x+dx, x+dx, x, x, x+dx, x+dx, x],
            y=[y, y, y+dy, y+dy, y, y, y+dy, y+dy],
            z=[z, z, z, z, z+dz, z+dz, z+dz, z+dz],
            i=[7, 0, 0, 0, 4, 4, 6, 6, 4, 0, 3, 2],
            j=[3, 4, 1, 2, 5, 6, 5, 2, 0, 1, 6, 7],
            k=[0, 7, 2, 3, 6, 7, 1, 1, 5, 5, 2, 6],
            color=color,
            opacity=opacity,
            name=name,
            showlegend=False,
            lighting=dict(ambient=0.48, diffuse=0.8, specular=0.25, roughness=0.45),
            lightposition=dict(x=0, y=-4000, z=5000)
        ))
        add_box_edges(fig, x, y, z, dx, dy, dz, edge_color, width=2)

    # Container frame and floor
    draw_cube(fig_3d, 0, 0, 0, c_l, c_w, 18, "rgba(230,160,30,0.8)", "Floor", opacity=0.82, edge_color="rgba(220,150,20,0.8)")
    add_box_edges(fig_3d, 0, 0, 0, c_l, c_w, c_h, "rgba(110,124,140,0.45)", width=5)

    for fitted_item in fitted_items:
        base_name = item_base_name(fitted_item)
        item_color = next((p["color"] for p in st.session_state.product_list if p["name"] == base_name), "#7f8c8d")
        x, y, z = [float(coord) for coord in fitted_item.position]
        dx, dy, dz = [float(dim) for dim in fitted_item.get_dimension()]
        draw_cube(fig_3d, x, y, z, dx, dy, dz, item_color, fitted_item.name, opacity=1.0, edge_color=hex_to_rgba(item_color, 0.55))

    fig_3d.update_layout(
        scene=dict(
            xaxis=dict(visible=False),
            yaxis=dict(visible=False),
            zaxis=dict(visible=False),
            camera=dict(eye=dict(x=1.7, y=-2.2, z=1.25)),
            aspectmode="manual",
            aspectratio=dict(x=3.2, y=1.25, z=1)
        ),
        paper_bgcolor="white",
        plot_bgcolor="white",
        margin=dict(l=0, r=0, b=0, t=0),
        height=420,
        showlegend=False
    )
    st.plotly_chart(fig_3d, use_container_width=True)

    metric_cols = st.columns(5)
    metric_cols[0].metric("Loaded / Total", f"{final_loaded_packages}/{total_requested_packages}")
    metric_cols[1].metric("Primary Loaded", f"{loaded_packages}/{total_requested_packages}")
    metric_cols[2].metric("Volume Used", f"{volume_pct:.1f}%")
    metric_cols[3].metric("Weight Used", f"{weight_pct:.1f}%")
    metric_cols[4].metric("Unloaded", f"{final_leftover_count}")

    legend_cols = st.columns(max(len(loaded_rows), 1))
    for index, row in enumerate(loaded_rows):
        with legend_cols[index % len(legend_cols)]:
            volume_share = (row["Volume"] / total_cargo_vol * 100) if total_cargo_vol else 0
            st.markdown(
                f"<span style='color:{row['Color']}; font-size:20px;'>&#9679;</span> "
                f"<b>{row['Name']}</b><br><span style='color:#9aacc1;'>x{row['Packages']} ({volume_share:.0f}% of volume)</span>",
                unsafe_allow_html=True
            )

    st.write("---")
    result_left, result_right = st.columns([3, 7])
    with result_left:
        if loaded_rows:
            fig_summary = go.Figure(data=[go.Pie(
                labels=[row["Name"] for row in loaded_rows],
                values=[row["Volume"] for row in loaded_rows],
                hole=.62,
                marker=dict(colors=[row["Color"] for row in loaded_rows]),
                textinfo="none"
            )])
            fig_summary.update_layout(margin=dict(l=0, r=0, b=0, t=0), height=190, showlegend=False)
            st.plotly_chart(fig_summary, use_container_width=True)
        else:
            st.info("No cargo fitted in the selected container.")

    with result_right:
        st.markdown("| Name | Packages | Volume | Weight |")
        st.markdown("|---|---:|---:|---:|")
        for row in loaded_rows:
            st.markdown(
                f"| <span style='color:{row['Color']}; font-size:18px;'>&#9679;</span> **{row['Name']}** "
                f"| {row['Packages']} | {row['Volume']:.2f} m3 | {row['Weight']:.2f} kg |",
                unsafe_allow_html=True
            )

    st.write("---")
    st.subheader("Final Load Plan")

    if not unfitted_items:
        st.success("Completed: all cargo fits in the selected container.")
    elif best_extra_option:
        remaining_volume = sum(item_volume(item) for item in unfitted_items) / 1e9
        remaining_weight = sum(to_float(item.weight) for item in unfitted_items)
        free_volume = max(total_container_vol - total_cargo_vol, 0)
        free_weight = max(float(c_m) - total_cargo_weight, 0)
        shortage_reasons = []
        if remaining_volume > free_volume:
            shortage_reasons.append("thiếu thể tích")
        if remaining_weight > free_weight:
            shortage_reasons.append("quá tải trọng")
        if oversized_selected:
            shortage_reasons.append("có kiện quá khổ so với container đã chọn")
        reason_text = ", ".join(shortage_reasons) if shortage_reasons else "không còn vị trí phù hợp theo thuật toán xếp"
        st.warning(
            f"Selected container is not enough ({reason_text}). "
            f"Suggested additional plan: {best_extra_option['count']} x {best_extra_option['name']}."
        )
    elif impossible_extra_items:
        st.error("No suitable standard container option was found for at least one leftover item.")
    else:
        st.error("No feasible additional-container plan was found. Review dimensions, weight limits, or split oversized cargo.")

    container_rows = []
    detail_rows = []
    for index, plan in enumerate(final_container_plans, start=1):
        usage = bin_usage(plan["bin"], plan["dims"])
        container_rows.append({
            "Container": index,
            "Type": plan["name"],
            "Packages": len(plan["bin"].items),
            "Volume Used": f"{usage['volume']:.2f} m3 ({usage['volume_pct']:.1f}%)",
            "Weight Used": f"{usage['weight']:.2f} kg ({usage['weight_pct']:.1f}%)"
        })
        for row in [row for row in summarize_items(plan["bin"].items) if row["Packages"] > 0]:
            detail_rows.append({
                "Container": index,
                "Container Type": plan["name"],
                "Name": row["Name"],
                "Packages": row["Packages"],
                "Volume (m3)": round(row["Volume"], 2),
                "Weight (kg)": round(row["Weight"], 2)
            })
    container_plan_df = pd.DataFrame(container_rows)
    detail_plan_df = pd.DataFrame(detail_rows)
    st.dataframe(container_plan_df, use_container_width=True, hide_index=True)

    for index, plan in enumerate(final_container_plans, start=1):
        usage = bin_usage(plan["bin"], plan["dims"])
        with st.expander(
            f"Container {index}: {plan['name']} - {len(plan['bin'].items)} packages, "
            f"{usage['volume_pct']:.1f}% volume, {usage['weight_pct']:.1f}% weight",
            expanded=index == 1
        ):
            plan_rows = [row for row in summarize_items(plan["bin"].items) if row["Packages"] > 0]
            if plan_rows:
                st.dataframe(
                    pd.DataFrame([
                        {
                            "Name": row["Name"],
                            "Packages": row["Packages"],
                            "Volume (m3)": round(row["Volume"], 2),
                            "Weight (kg)": round(row["Weight"], 2)
                        }
                        for row in plan_rows
                    ]),
                    use_container_width=True,
                    hide_index=True
                )
            else:
                st.info("No cargo in this container.")

    if oversized_selected:
        st.warning("Oversized against selected container:")
        st.dataframe(pd.DataFrame(oversized_selected), use_container_width=True, hide_index=True)

    if final_leftover_count > 0:
        leftover_items = unfitted_items if not best_extra_option else []
        leftover_rows = [row for row in summarize_items(leftover_items) if row["Packages"] > 0]
        if leftover_rows:
            st.warning("Leftover cargo requiring manual handling:")
            st.dataframe(
                pd.DataFrame([
                    {"Name": row["Name"], "Packages": row["Packages"], "Volume (m3)": round(row["Volume"], 2), "Weight (kg)": round(row["Weight"], 2)}
                    for row in leftover_rows
                ]),
                use_container_width=True,
                hide_index=True
            )

    st.write("")
    action_cols = st.columns([1, 1.25, 1.25, 5])
    with action_cols[0]:
        if st.button("Back", use_container_width=True):
            st.session_state.current_tab = "CONTAINERS & TRUCKS"
            st.rerun()
    with action_cols[1]:
        st.download_button(
            "Download CSV",
            data=detail_plan_df.to_csv(index=False).encode("utf-8-sig"),
            file_name="container_load_plan.csv",
            mime="text/csv",
            use_container_width=True
        )
    with action_cols[2]:
        st.button("Copy request", use_container_width=True)







