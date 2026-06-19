from itertools import permutations

from py3dbp import Bin, Item, Packer

from .models import CargoItem, ContainerSpec, PackedContainer, PackedItem


def can_fit_item(item: CargoItem, container: ContainerSpec) -> bool:
    cargo_dims = [item.length_mm, item.width_mm, item.height_mm]
    limit_dims = [container.length_mm, container.width_mm, container.height_mm]
    dimension_fit = any(all(rotated[i] <= limit_dims[i] for i in range(3)) for rotated in permutations(cargo_dims))
    return dimension_fit and item.weight_kg <= container.max_weight_kg


def sort_items_for_loading(items):
    return sorted(items, key=lambda item: (item.weight_kg, item.volume_mm3), reverse=True)


def pack_container(container: ContainerSpec, items, role="Selected"):
    active_bin = Bin(container.name, container.length_mm, container.width_mm, container.height_mm, container.max_weight_kg)
    active_bin.format_numbers(3)
    packer = Packer()
    packer.add_bin(active_bin)

    py_items = []
    cargo_by_id = {}
    for cargo in sort_items_for_loading(items):
        py_item = Item(cargo.id, cargo.length_mm, cargo.width_mm, cargo.height_mm, cargo.weight_kg)
        py_item.format_numbers(3)
        py_items.append(py_item)
        cargo_by_id[cargo.id] = cargo

    for py_item in py_items:
        packer.pack_to_bin(active_bin, py_item)

    packed = []
    for py_item in active_bin.items:
        cargo = cargo_by_id[py_item.name]
        packed.append(PackedItem(
            cargo=cargo,
            position=tuple(float(value) for value in py_item.position),
            size=tuple(float(value) for value in py_item.get_dimension()),
        ))

    packed_ids = {item.cargo.id for item in packed}
    leftovers = [item for item in items if item.id not in packed_ids]
    return PackedContainer(spec=container, items=packed, role=role), leftovers

