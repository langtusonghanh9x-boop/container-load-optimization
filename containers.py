from .models import ContainerSpec


STANDARD_CONTAINERS = {
    "20' Standard": ContainerSpec("20' Standard", 5898, 2352, 2393, 28000),
    "40' Standard": ContainerSpec("40' Standard", 12032, 2352, 2393, 28000),
    "40' High-Cube (40HQ)": ContainerSpec("40' High-Cube (40HQ)", 12032, 2352, 2698, 28000),
    "45' High-Cube": ContainerSpec("45' High-Cube", 13556, 2352, 2698, 29000),
}


def get_container_spec(name, custom_dims=None):
    if name == "Custom":
        custom_dims = custom_dims or {"l": 6000, "w": 2400, "h": 2400, "m": 25000}
        return ContainerSpec("Custom", custom_dims["l"], custom_dims["w"], custom_dims["h"], custom_dims["m"])
    return STANDARD_CONTAINERS[name]


def catalog_smallest_first():
    return sorted(STANDARD_CONTAINERS.values(), key=lambda spec: spec.volume_m3)

