from dataclasses import replace

from .manager import ContainerManager
from .models import LoadingConfig, LoadingPlan
from .packing import can_fit_item, pack_container


def _utilization_score(packed):
    """Prefer the arrangement that fills the current vehicle most completely."""
    return (packed.volume_pct, packed.weight_pct, packed.package_count)


def _alternative_configs(items, config):
    """Offer valid fallback layouts without overriding an explicit load order."""
    alternatives = []
    alternate_strategy = (
        "fill_width_before_length"
        if config.placement_strategy == "stable_floor_first"
        else "stable_floor_first"
    )
    alternatives.append(replace(config, placement_strategy=alternate_strategy))
    if not any(item.loading_order is not None for item in items):
        alternate_priority = "large_first" if config.heavy_priority == "heavy_bottom" else "heavy_bottom"
        alternatives.append(replace(config, heavy_priority=alternate_priority))
        alternatives.append(replace(config, placement_strategy=alternate_strategy, heavy_priority=alternate_priority))
    return alternatives


def _pack_fullest_vehicle(spec, items, role, config):
    """Fill one vehicle as much as possible before moving to the next one."""
    best_packed, best_remaining = pack_container(spec, items, role=role, config=config)
    # A near-full volume or payload does not need slower alternative searches.
    if not best_remaining or max(best_packed.volume_pct, best_packed.weight_pct) >= 92:
        return best_packed, best_remaining

    for alternative in _alternative_configs(items, config):
        packed, remaining = pack_container(spec, items, role=role, config=alternative)
        if _utilization_score(packed) > _utilization_score(best_packed):
            best_packed, best_remaining = packed, remaining
    return best_packed, best_remaining


def _pack_fixed_specs(specs, items, config):
    containers = []
    remaining = list(items)
    for index, spec in enumerate(specs, start=1):
        packed, remaining = _pack_fullest_vehicle(
            spec,
            remaining,
            "Selected" if index == 1 else f"Selected {index}",
            config,
        )
        containers.append(packed)
        if not remaining:
            break
    return containers, remaining


def _pack_repeating_spec(spec, items, config):
    containers = []
    remaining = list(items)
    max_additional = getattr(config, "max_additional_containers", 10)
    while remaining and len(containers) < max_additional:
        packed, next_remaining = _pack_fullest_vehicle(
            spec,
            remaining,
            f"Additional {len(containers) + 1}",
            config,
        )
        if not packed.items or len(next_remaining) == len(remaining):
            return None, remaining
        containers.append(packed)
        remaining = next_remaining
    return containers, remaining


def optimize_loading(items, selected_spec, selected_quantity=1, allow_auto_add=True, config=None):
    config = config or LoadingConfig()
    warnings = []
    suggestions = []
    manager = ContainerManager(selected_spec, selected_quantity, allow_auto_add)
    vehicle_label = "xe tai" if "truck" in manager.selected_spec.name.lower() else "container"

    impossible_for_selected = [
        item for item in items
        if not any(can_fit_item(item, spec) for spec in manager.initial_specs())
    ]
    if impossible_for_selected:
        warnings.append(
            f"{len(impossible_for_selected)} kien khong phu hop container da chon do vuot kich thuoc hoac tai trong."
        )

    containers, remaining = _pack_fixed_specs(manager.initial_specs(), items, config)

    if not remaining:
        suggestions.append("Hoan thanh: toan bo hang da duoc xep trong so container da chon.")
        return LoadingPlan(containers, len(items), [], warnings, suggestions)

    impossible_for_catalog = [
        item for item in remaining
        if not any(can_fit_item(item, spec) for spec in manager.candidate_extra_specs([item]))
    ]
    if impossible_for_catalog:
        warnings.append(
            f"{len(impossible_for_catalog)} kien du khong phu hop bat ky {vehicle_label} tieu chuan nao trong catalog."
        )

    packable_remaining = [item for item in remaining if item not in impossible_for_catalog]
    if not allow_auto_add or not packable_remaining:
        suggestions.append("Can tach hang qua kho, doi phuong tien chuyen dung hoac cap nhat catalog container.")
        return LoadingPlan(containers, len(items), remaining, warnings, suggestions)

    feasible_options = []
    for spec in manager.candidate_extra_specs(packable_remaining):
        extra_containers, extra_leftover = _pack_repeating_spec(spec, packable_remaining, config)
        if extra_containers is not None and not extra_leftover:
            feasible_options.append((len(extra_containers), spec.volume_m3, spec, extra_containers))

    if not feasible_options:
        warnings.append("Khong tim duoc phuong an phuong tien bo sung du chua phan hang du.")
        suggestions.append(f"Thu tang so luong {vehicle_label} da chon, dung {vehicle_label} lon hon hoac chia lo hang.")
        return LoadingPlan(containers, len(items), remaining, warnings, suggestions)

    _, _, chosen_spec, extra_containers = sorted(feasible_options, key=lambda option: (option[0], option[1]))[0]
    containers.extend(extra_containers)
    final_leftover = impossible_for_catalog
    suggestions.append(
        f"Tu bo sung {len(extra_containers)} {vehicle_label} {chosen_spec.name} cho phan hang du, uu tien it phuong tien nhat va kich thuoc nho nhat du chua."
    )
    return LoadingPlan(containers, len(items), final_leftover, warnings, suggestions)
