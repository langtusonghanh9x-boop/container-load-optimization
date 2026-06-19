import plotly.graph_objects as go


def hex_to_rgba(hex_color, opacity):
    clean = hex_color.lstrip("#")
    if len(clean) != 6:
        return f"rgba(127,140,141,{opacity})"
    r, g, b = tuple(int(clean[i:i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r},{g},{b},{opacity})"


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


def build_container_figure(packed_container):
    spec = packed_container.spec
    fig = go.Figure()
    draw_cube(fig, 0, 0, 0, spec.length_mm, spec.width_mm, 18, "rgba(230,160,30,0.8)", "Floor", opacity=0.82, edge_color="rgba(220,150,20,0.8)")
    add_box_edges(fig, 0, 0, 0, spec.length_mm, spec.width_mm, spec.height_mm, "rgba(110,124,140,0.45)", width=5)

    for item in packed_container.items:
        x, y, z = item.position
        dx, dy, dz = item.size
        draw_cube(
            fig,
            x, y, z, dx, dy, dz,
            item.cargo.color,
            item.cargo.id,
            opacity=1.0,
            edge_color=hex_to_rgba(item.cargo.color, 0.55),
        )

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

