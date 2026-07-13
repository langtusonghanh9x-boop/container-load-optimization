from .models import ContainerSpec


STANDARD_CONTAINERS = {
    "20' Standard": ContainerSpec("20' Standard", 5898, 2352, 2393, 28000),
    "40' Standard": ContainerSpec("40' Standard", 12032, 2352, 2393, 28000),
    "40' High-Cube (40HQ)": ContainerSpec("40' High-Cube (40HQ)", 12032, 2352, 2698, 28000),
    "45' High-Cube": ContainerSpec("45' High-Cube", 13556, 2352, 2698, 29000),
    "Truck 1.5T": ContainerSpec("Truck 1.5T", 3100, 1600, 1700, 1500),
    "Truck 2T": ContainerSpec("Truck 2T", 3800, 1800, 1850, 2000),
    "Truck 2.5T": ContainerSpec("Truck 2.5T", 4300, 1900, 1950, 2500),
    "Truck 3.5T": ContainerSpec("Truck 3.5T", 4300, 2000, 2000, 3500),
    "Truck 5T": ContainerSpec("Truck 5T", 6200, 2300, 2300, 5000),
    "Truck 8T": ContainerSpec("Truck 8T", 8000, 2350, 2400, 8000),
    "Truck 10T": ContainerSpec("Truck 10T", 9500, 2400, 2500, 10000),
    "Truck 15T": ContainerSpec("Truck 15T", 12000, 2400, 2600, 15000),
}


def get_container_spec(name, custom_dims=None):
    if name in STANDARD_CONTAINERS:
        return STANDARD_CONTAINERS[name]
    # For custom containers, use provided dimensions (fallback defaults)
    custom = custom_dims or {"l": 6000, "w": 2400, "h": 2400, "m": 25000}
    return ContainerSpec(name, custom["l"], custom["w"], custom["h"], custom["m"])


def is_truck_spec(spec):
    """Return whether a catalog specification is a truck."""
    return spec.name.startswith("Truck ")


def catalog_smallest_first(vehicle_type=None):
    """Return catalog entries ordered by volume, optionally by vehicle type.

    ``vehicle_type`` may be ``"container"`` or ``"truck"``.  Keeping the
    automatic additions within the selected type prevents a container loading
    plan from unexpectedly adding trucks, and vice versa.
    """
    specs = STANDARD_CONTAINERS.values()
    if vehicle_type == "truck":
        specs = (spec for spec in specs if is_truck_spec(spec))
    elif vehicle_type == "container":
        specs = (spec for spec in specs if not is_truck_spec(spec))
    return sorted(specs, key=lambda spec: spec.volume_m3)
