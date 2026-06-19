from .manager import ContainerManager
from .models import LoadingPlan
from .packing import can_fit_item, pack_container


def _pack_fixed_specs(specs, items):
    containers = []
    remaining = list(items)
    for index, spec in enumerate(specs, start=1):
        packed, remaining = pack_container(spec, remaining, role="Selected" if index == 1 else f"Selected {index}")
        containers.append(packed)
        if not remaining:
            break
    return containers, remaining


def _pack_repeating_spec(spec, items):
    containers = []
    remaining = list(items)
    while remaining:
        packed, next_remaining = pack_container(spec, remaining, role=f"Additional {len(containers) + 1}")
        if not packed.items or len(next_remaining) == len(remaining):
            return None, remaining
        containers.append(packed)
        remaining = next_remaining
    return containers, []


def optimize_loading(items, selected_spec, selected_quantity=1, allow_auto_add=True):
    warnings = []
    suggestions = []
    manager = ContainerManager(selected_spec, selected_quantity, allow_auto_add)

    impossible_for_selected = [item for item in items if not can_fit_item(item, selected_spec)]
    if impossible_for_selected:
        warnings.append(
            f"{len(impossible_for_selected)} kiện không phù hợp container đã chọn do vượt kích thước hoặc tải trọng."
        )

    containers, remaining = _pack_fixed_specs(manager.initial_specs(), items)

    if not remaining:
        suggestions.append("Hoàn thành: toàn bộ hàng đã được xếp trong số container đã chọn.")
        return LoadingPlan(containers, len(items), [], warnings, suggestions)

    impossible_for_catalog = [
        item for item in remaining
        if not any(can_fit_item(item, spec) for spec in manager.candidate_extra_specs([item]))
    ]
    if impossible_for_catalog:
        warnings.append(
            f"{len(impossible_for_catalog)} kiện dư không phù hợp bất kỳ container tiêu chuẩn nào trong catalog."
        )

    packable_remaining = [item for item in remaining if item not in impossible_for_catalog]
    if not allow_auto_add or not packable_remaining:
        suggestions.append("Cần tách hàng quá khổ, đổi phương tiện chuyên dụng hoặc cập nhật catalog container.")
        return LoadingPlan(containers, len(items), remaining, warnings, suggestions)

    feasible_options = []
    for spec in manager.candidate_extra_specs(packable_remaining):
        extra_containers, extra_leftover = _pack_repeating_spec(spec, packable_remaining)
        if extra_containers is not None and not extra_leftover:
            feasible_options.append((len(extra_containers), spec.volume_m3, spec, extra_containers))

    if not feasible_options:
        warnings.append("Không tìm được phương án container bổ sung đủ chứa phần hàng dư.")
        suggestions.append("Thử tăng số lượng container đã chọn, dùng container lớn hơn hoặc chia lô hàng.")
        return LoadingPlan(containers, len(items), remaining, warnings, suggestions)

    _, _, chosen_spec, extra_containers = sorted(feasible_options, key=lambda option: (option[0], option[1]))[0]
    containers.extend(extra_containers)
    final_leftover = impossible_for_catalog
    suggestions.append(
        f"Tự bổ sung {len(extra_containers)} container {chosen_spec.name} cho phần hàng dư, ưu tiên ít container nhất và container nhỏ nhất đủ chứa."
    )
    return LoadingPlan(containers, len(items), final_leftover, warnings, suggestions)

