from collections import defaultdict
from math import sqrt

import pandas as pd


def _overlap_1d(a, sa, b, sb):
    return max(0, min(a + sa, b + sb) - max(a, b))


def _contact_area_for_item(item, items):
    x, y, z = item.position
    dx, dy, dz = item.size
    contact = 0.0
    for other in items:
        if other is item:
            continue
        ox, oy, oz = other.position
        odx, ody, odz = other.size
        if abs(oz + odz - z) < 1e-6:
            contact += _overlap_1d(x, dx, ox, odx) * _overlap_1d(y, dy, oy, ody)
    return contact


def container_metrics(container):
    if not container.items:
        return {
            "utilization_pct": 0.0,
            "weight_utilization_pct": 0.0,
            "empty_space_m3": container.spec.volume_m3,
            "layers": 0,
            "support_ratio": 0.0,
            "center_of_gravity": (0.0, 0.0, 0.0),
            "stability_score": 0.0,
            "packing_score": getattr(container, "optimization_score", 0.0),
        }
    used = sum(item.size[0] * item.size[1] * item.size[2] for item in container.items)
    capacity = container.spec.length_mm * container.spec.width_mm * container.spec.height_mm
    utilization = used / capacity
    empty = (1.0 - utilization) * container.spec.volume_m3
    total_weight = max(sum(item.cargo.weight_kg for item in container.items), 1e-9)
    cx = sum((item.position[0] + item.size[0] / 2) * item.cargo.weight_kg for item in container.items) / total_weight
    cy = sum((item.position[1] + item.size[1] / 2) * item.cargo.weight_kg for item in container.items) / total_weight
    cz = sum((item.position[2] + item.size[2] / 2) * item.cargo.weight_kg for item in container.items) / total_weight
    balance = max(0.0, 1.0 - sqrt(((cx - container.spec.length_mm / 2) / max(container.spec.length_mm / 2, 1)) ** 2 + ((cy - container.spec.width_mm / 2) / max(container.spec.width_mm / 2, 1)) ** 2))
    low_cog = max(0.0, 1.0 - cz / max(container.spec.height_mm, 1))
    support_ratios = []
    for item in container.items:
        if item.position[2] <= 1e-6:
            support_ratios.append(1.0)
        else:
            support_ratios.append(min(1.0, _contact_area_for_item(item, container.items) / max(item.size[0] * item.size[1], 1.0)))
    support = sum(support_ratios) / len(support_ratios)
    layers = len({round(item.position[2], 3) for item in container.items})
    stability = (support + balance + low_cog) / 3
    return {
        "utilization_pct": utilization * 100,
        "weight_utilization_pct": container.weight_pct,
        "empty_space_m3": empty,
        "layers": layers,
        "support_ratio": support,
        "center_of_gravity": (cx, cy, cz),
        "stability_score": stability,
        "packing_score": getattr(container, "optimization_score", getattr(container, "score", 0.0)),
    }


def summarize_container(container):
    grouped = defaultdict(lambda: {"Packages": 0, "Volume (m3)": 0.0, "Weight (kg)": 0.0, "Color": "#7f8c8d"})
    for item in container.items:
        entry = grouped[item.cargo.name]
        entry["Packages"] += 1
        entry["Volume (m3)"] += (item.size[0] * item.size[1] * item.size[2]) / 1e9
        entry["Weight (kg)"] += item.cargo.weight_kg
        entry["Color"] = item.cargo.color
    return [
        {
            "Name": name,
            "Packages": values["Packages"],
            "Volume (m3)": round(values["Volume (m3)"], 3),
            "Weight (kg)": round(values["Weight (kg)"], 2),
            "Color": values["Color"],
        }
        for name, values in grouped.items()
    ]


def container_summary_df(plan):
    rows = []
    for index, container in enumerate(plan.containers, start=1):
        metrics = container_metrics(container)
        rows.append({
            "Container": index,
            "Type": container.spec.name,
            "Packages": container.package_count,
            "Volume Used": f"{container.cargo_volume_m3:.2f} m3 ({container.volume_pct:.1f}%)",
            "Weight Used": f"{container.cargo_weight_kg:.2f} kg ({container.weight_pct:.1f}%)",
            "Empty Space (m3)": round(metrics["empty_space_m3"], 2),
            "Layers": metrics["layers"],
            "Support Ratio": round(metrics["support_ratio"], 3),
            "CoG (x,y,z)": f"({metrics['center_of_gravity'][0]:.0f}, {metrics['center_of_gravity'][1]:.0f}, {metrics['center_of_gravity'][2]:.0f})",
            "Stability": round(metrics["stability_score"], 3),
            "Packing Score": round(metrics["packing_score"], 3),
        })
    return pd.DataFrame(rows)


def loading_metrics(plan):
    total_empty = sum(container_metrics(c)["empty_space_m3"] for c in plan.containers)
    return {
        "container_count": len(plan.containers),
        "loaded_count": plan.loaded_count,
        "leftover_count": plan.leftover_count,
        "total_volume_m3": plan.total_volume_m3,
        "total_weight_kg": plan.total_weight_kg,
        "total_empty_space_m3": total_empty,
        "diagnostics": plan.diagnostics,
    }


def detail_plan_df(plan):
    rows = []
    for index, container in enumerate(plan.containers, start=1):
        for row in summarize_container(container):
            rows.append({
                "Container": index,
                "Container Type": container.spec.name,
                "Color": row["Color"],
                "Name": row["Name"],
                "Packages": row["Packages"],
                "Volume (m3)": row["Volume (m3)"],
                "Weight (kg)": row["Weight (kg)"],
            })
    return pd.DataFrame(rows)
