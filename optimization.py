from dataclasses import replace
from math import sqrt

from .manager import ContainerManager
from .models import LoadingConfig, LoadingPlan
from .packing import can_fit_item, pack_container


SEQUENCES = (
    "largest_volume_first", "largest_base_first", "heaviest_first",
    "longest_first", "highest_density_first",
)
PLACEMENT_HEURISTICS = (
    "length_first", "width_first", "bottom_left_fill", "best_contact_area",
    "best_volume_fit", "lowest_z", "best_free_space_reduction",
)


def _candidate_score(packed):
    """Volume is primary; the Beam weighted score breaks volume ties."""
    return (packed.volume_pct, getattr(packed, "optimization_score", 0.0))


def _layout_score(packed):
    """Score a complete vehicle layout with volume utilization dominant."""
    if not packed.items:
        return (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    spec = packed.spec
    cargo_volume = sum(item.size[0] * item.size[1] * item.size[2] for item in packed.items)
    max_x = max(item.position[0] + item.size[0] for item in packed.items)
    max_y = max(item.position[1] + item.size[1] for item in packed.items)
    max_z = max(item.position[2] + item.size[2] for item in packed.items)
    compactness = cargo_volume / max(max_x * max_y * max_z, 1.0)
    support_values = []
    for item in packed.items:
        if item.position[2] <= 1e-6:
            support_values.append(1.0)
            continue
        base = max(item.size[0] * item.size[1], 1.0)
        supported = 0.0
        for other in packed.items:
            if other is item or abs(other.position[2] + other.size[2] - item.position[2]) > 1e-6:
                continue
            supported += max(0.0, min(item.position[0] + item.size[0], other.position[0] + other.size[0]) - max(item.position[0], other.position[0])) * max(0.0, min(item.position[1] + item.size[1], other.position[1] + other.size[1]) - max(item.position[1], other.position[1]))
        support_values.append(min(1.0, supported / base))
    support = sum(support_values) / len(support_values)
    total_weight = max(packed.cargo_weight_kg, 1e-9)
    cx = sum((item.position[0] + item.size[0] / 2) * item.cargo.weight_kg for item in packed.items) / total_weight
    cy = sum((item.position[1] + item.size[1] / 2) * item.cargo.weight_kg for item in packed.items) / total_weight
    cz = sum((item.position[2] + item.size[2] / 2) * item.cargo.weight_kg for item in packed.items) / total_weight
    distance = sqrt(((cx - spec.length_mm / 2) / max(spec.length_mm / 2, 1)) ** 2 + ((cy - spec.width_mm / 2) / max(spec.width_mm / 2, 1)) ** 2)
    balance = max(0.0, 1.0 - distance)
    low_cog = max(0.0, 1.0 - cz / max(spec.height_mm, 1))
    planes = len({round(item.position[0], 4) for item in packed.items}) + len({round(item.position[1], 4) for item in packed.items}) + len({round(item.position[2], 4) for item in packed.items})
    return (packed.volume_pct, compactness, support, balance, low_cog, 1.0 / planes)


def _candidate_configs(items, config):
    """Build every requested sequence/heuristic candidate within search limit."""
    sequences = ("loading_order",) if any(item.loading_order is not None for item in items) else SEQUENCES
    candidates = [replace(config, packing_sequence=sequence, placement_strategy=heuristic)
                  for sequence in sequences for heuristic in PLACEMENT_HEURISTICS]
    return candidates[:max(1, int(getattr(config, "search_limit", len(candidates))))]


def _incremental_repacking(spec, items, role, packed, remaining, config):
    """Retry a near-full layout with a wider Beam before committing it.

    This is a safe rollback-style repack: the original layout remains the
    fallback and is replaced only when the widened free-space search improves
    the complete plan score.  Ordered loads keep their original order.
    """
    if packed.volume_pct >= 98.0:
        return packed, remaining
    repack_updates = {"placement_strategy": "best_free_space_reduction"}
    # Keep cloud deployments compatible with a previously cached/packaged
    # LoadingConfig that does not yet expose the optional Beam setting.
    if "beam_width" in getattr(config, "__dataclass_fields__", {}):
        repack_updates["beam_width"] = max(int(getattr(config, "beam_width", 12)), 24)
    repack_config = replace(config, **repack_updates)
    repacked, repacked_remaining = pack_container(spec, items, role=role, config=repack_config)
    if _candidate_score(repacked) > _candidate_score(packed):
        return repacked, repacked_remaining
    return packed, remaining


def optimize_single_container(spec, cartons, role="Selected", config=None):
    """Find and commit the best plan for exactly one container/truck.

    No carton is assigned to another vehicle while this function evaluates its
    full sequence × orientation × placement-heuristic search space.
    """
    config = config or LoadingConfig()
    candidate_plans = []
    for candidate_config in _candidate_configs(cartons, config):
        packed, remaining = pack_container(spec, cartons, role=role, config=candidate_config)
        score = _candidate_score(packed)
        candidate_plans.append((score, packed, remaining, candidate_config))
    _, best_packed, best_remaining, best_config = max(candidate_plans, key=lambda candidate: candidate[0])
    return _incremental_repacking(spec, cartons, role, best_packed, best_remaining, best_config)


def _pack_fullest_vehicle(spec, items, role, config):
    """Compatibility wrapper used by the multi-container engine."""
    return optimize_single_container(spec, items, role, config)


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
