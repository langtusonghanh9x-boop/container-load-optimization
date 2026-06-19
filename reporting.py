from collections import defaultdict

import pandas as pd


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
    return pd.DataFrame([
        {
            "Container": index,
            "Type": container.spec.name,
            "Packages": container.package_count,
            "Volume Used": f"{container.cargo_volume_m3:.2f} m3 ({container.volume_pct:.1f}%)",
            "Weight Used": f"{container.cargo_weight_kg:.2f} kg ({container.weight_pct:.1f}%)",
        }
        for index, container in enumerate(plan.containers, start=1)
    ])


def detail_plan_df(plan):
    rows = []
    for index, container in enumerate(plan.containers, start=1):
        for row in summarize_container(container):
            rows.append({
                "Container": index,
                "Container Type": container.spec.name,
                "Name": row["Name"],
                "Packages": row["Packages"],
                "Volume (m3)": row["Volume (m3)"],
                "Weight (kg)": row["Weight (kg)"],
            })
    return pd.DataFrame(rows)

