import plotly.graph_objects as go


def hex_to_rgba(hex_color, opacity):
    clean = hex_color.lstrip("#")
    if len(clean) != 6:
        return f"rgba(127,140,141,{opacity})"
    r, g, b = tuple(int(clean[i:i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r},{g},{b},{opacity})"


def _intensity_color(weight, max_weight, base):
    if max_weight <= 0:
        return base
    t = weight / max_weight
    r, g, b = tuple(int(base.lstrip("#")[i:i+2], 16) for i in (0, 2, 4))
    nr = int(r + (255 - r) * (1 - t))
    ng = int(g + (255 - g) * (1 - t))
    nb = int(b + (255 - b) * (1 - t))
    return f"rgb({nr},{ng},{nb})"


def add_box_edges(fig, x, y, z, dx, dy, dz, color, width=2):
    points = [
        (x, y, z), (x + dx, y, z), (x + dx, y + dy, z), (x, y + dy, z),
        (x, y, z + dz), (x + dx, y, z + dz), (x + dx, y + dy, z + dz), (x, y + dy, z + dz)
    ]
    edges = [(0, 1), (1, 2), (2, 3), (3, 0), (4, 5), (5, 6), (6, 7), (7, 4), (0, 4), (1, 5), (2, 6), (3, 7)]
    xs, ys, zs = [], [], []
    for a, b in edges:
        xs += [points[a][0], points[b][0], None]
        ys += [points[a][1], points[b][1], None]
        zs += [points[a][2], points[b][2], None]
    fig.add_trace(go.Scatter3d(x=xs, y=ys, z=zs, mode="lines", line=dict(color=color, width=width), showlegend=False, hoverinfo="skip"))


def draw_cube(fig, x, y, z, dx, dy, dz, color, name, opacity=1.0, edge_color="rgba(255,255,255,0.45)"):
    fig.add_trace(go.Mesh3d(
        x=[x, x + dx, x + dx, x, x, x + dx, x + dx, x],
        y=[y, y, y + dy, y + dy, y, y, y + dy, y + dy],
        z=[z, z, z, z, z + dz, z + dz, z + dz, z + dz],
        i=[7, 0, 0, 0, 4, 4, 6, 6, 4, 0, 3, 2],
        j=[3, 4, 1, 2, 5, 6, 5, 2, 0, 1, 6, 7],
        k=[0, 7, 2, 3, 6, 7, 1, 1, 5, 5, 2, 6],
        color=color,
        opacity=opacity,
        name=name,
        showlegend=False,
        lighting=dict(ambient=0.48, diffuse=0.8, specular=0.25, roughness=0.45),
        lightposition=dict(x=0, y=-4000, z=5000),
    ))
    add_box_edges(fig, x, y, z, dx, dy, dz, edge_color, width=2)


def _add_2d_box(fig, x, y, w, h, color, name, xaxis, yaxis):
    fig.add_trace(go.Scatter(
        x=[x, x + w, x + w, x, x],
        y=[y, y, y + h, y + h, y],
        mode="lines",
        fill="toself",
        fillcolor=hex_to_rgba(color, 0.5),
        line=dict(color=hex_to_rgba(color, 1.0), width=1),
        name=name,
        showlegend=False,
        xaxis=xaxis,
        yaxis=yaxis,
    ))


def build_container_figure(packed_container, view="3d", step=None, show_cog=False, weight_heatmap=False):
    spec = packed_container.spec
    items = packed_container.items
    if step is not None:
        items = items[:step]

    if view in ("3d", "cog"):
        fig = go.Figure()
        draw_cube(fig, 0, 0, 0, spec.length_mm, spec.width_mm, 18, "rgba(230,160,30,0.8)", "Floor", opacity=0.82, edge_color="rgba(220,150,20,0.8)")
        add_box_edges(fig, 0, 0, 0, spec.length_mm, spec.width_mm, spec.height_mm, "rgba(110,124,140,0.45)", width=5)

        max_weight = max((item.cargo.weight_kg for item in items), default=1.0)
        for item in items:
            x, y, z = item.position
            dx, dy, dz = item.size
            color = item.cargo.color
            if weight_heatmap:
                color = _intensity_color(item.cargo.weight_kg, max_weight, color)
            draw_cube(fig, x, y, z, dx, dy, dz, color, item.cargo.id, opacity=1.0, edge_color=hex_to_rgba(color, 0.55))

        if show_cog or view == "cog":
            total_weight = max(sum(item.cargo.weight_kg for item in items), 1e-9)
            cx = sum((item.position[0] + item.size[0] / 2) * item.cargo.weight_kg for item in items) / total_weight
            cy = sum((item.position[1] + item.size[1] / 2) * item.cargo.weight_kg for item in items) / total_weight
            cz = sum((item.position[2] + item.size[2] / 2) * item.cargo.weight_kg for item in items) / total_weight
            fig.add_trace(go.Scatter3d(x=[cx], y=[cy], z=[cz], mode="markers", marker=dict(size=8, color="red"), name="CoG"))

        fig.update_layout(
            scene=dict(
                xaxis=dict(visible=False),
                yaxis=dict(visible=False),
                zaxis=dict(visible=False),
                camera=dict(eye=dict(x=1.7, y=-2.2, z=1.25)),
                aspectmode="manual",
                aspectratio=dict(x=max(spec.length_mm / 3800, 1.5), y=max(spec.width_mm / 2000, 1), z=max(spec.height_mm / 2400, 1)),
            ),
            paper_bgcolor="white",
            plot_bgcolor="white",
            margin=dict(l=0, r=0, b=0, t=0),
            height=420,
            showlegend=False,
        )
        return fig

    fig = go.Figure()
    if view == "top":
        x_range = spec.length_mm
        y_range = spec.width_mm
        for item in items:
            _add_2d_box(fig, item.position[0], item.position[1], item.size[0], item.size[1], item.cargo.color, item.cargo.id, "x", "y")
        fig.update_layout(xaxis=dict(range=[0, x_range], visible=False), yaxis=dict(range=[0, y_range], visible=False, scaleanchor="x"), height=420)
    elif view == "front":
        for item in items:
            _add_2d_box(fig, item.position[0], item.position[2], item.size[0], item.size[2], item.cargo.color, item.cargo.id, "x", "y")
        fig.update_layout(xaxis=dict(range=[0, spec.length_mm], visible=False), yaxis=dict(range=[0, spec.height_mm], visible=False, scaleanchor="x"), height=420)
    elif view == "side":
        for item in items:
            _add_2d_box(fig, item.position[1], item.position[2], item.size[1], item.size[2], item.cargo.color, item.cargo.id, "x", "y")
        fig.update_layout(xaxis=dict(range=[0, spec.width_mm], visible=False), yaxis=dict(range=[0, spec.height_mm], visible=False, scaleanchor="x"), height=420)
    elif view == "layer":
        for item in items:
            _add_2d_box(fig, item.position[0], item.position[1], item.size[0], item.size[1], item.cargo.color, item.cargo.id, "x", "y")
        fig.update_layout(
            xaxis=dict(range=[0, spec.length_mm], visible=False),
            yaxis=dict(range=[0, spec.width_mm], visible=False, scaleanchor="x"),
            height=420,
            title="Layer View (color = product)",
        )
    elif view == "weight_heatmap":
        max_weight = max((item.cargo.weight_kg for item in items), default=1.0)
        for item in items:
            color = _intensity_color(item.cargo.weight_kg, max_weight, item.cargo.color)
            _add_2d_box(fig, item.position[0], item.position[1], item.size[0], item.size[1], color, item.cargo.id, "x", "y")
        fig.update_layout(xaxis=dict(range=[0, spec.length_mm], visible=False), yaxis=dict(range=[0, spec.width_mm], visible=False, scaleanchor="x"), height=420)
    elif view == "remaining_space":
        _add_2d_box(fig, 0, 0, spec.length_mm, spec.width_mm, "#eeeeee", "Container", "x", "y")
        for item in items:
            _add_2d_box(fig, item.position[0], item.position[1], item.size[0], item.size[1], item.cargo.color, item.cargo.id, "x", "y")
        fig.update_layout(xaxis=dict(range=[0, spec.length_mm], visible=False), yaxis=dict(range=[0, spec.width_mm], visible=False, scaleanchor="x"), height=420)
    else:
        raise ValueError(f"Unknown view: {view}")

    fig.update_layout(paper_bgcolor="white", plot_bgcolor="white", margin=dict(l=0, r=0, b=0, t=0), showlegend=False)
    return fig


def build_step_sequence(packed_container):
    """Generate step-by-step loading data for animation."""
    return [
        {"step": index, "cargo_id": item.cargo.id, "name": item.cargo.name,
         "position": item.position, "size": item.size, "color": item.cargo.color}
        for index, item in enumerate(packed_container.items, start=1)
    ]


def build_all_views(packed_container):
    return {
        "3d": build_container_figure(packed_container, view="3d"),
        "top": build_container_figure(packed_container, view="top"),
        "front": build_container_figure(packed_container, view="front"),
        "side": build_container_figure(packed_container, view="side"),
        "layer": build_container_figure(packed_container, view="layer"),
        "weight_heatmap": build_container_figure(packed_container, view="weight_heatmap"),
        "remaining_space": build_container_figure(packed_container, view="remaining_space"),
        "cog": build_container_figure(packed_container, view="cog", show_cog=True),
    }
