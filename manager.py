from .containers import catalog_smallest_first, is_truck_spec
from .packing import can_fit_item


class ContainerManager:
    def __init__(self, selected_spec, selected_quantity=1, allow_auto_add=True):
        self.selected_spec = selected_spec
        self.selected_quantity = max(int(selected_quantity), 1)
        self.allow_auto_add = allow_auto_add

    def initial_specs(self):
        return [self.selected_spec for _ in range(self.selected_quantity)]

    def candidate_extra_specs(self, items):
        if not self.allow_auto_add:
            return []
        candidates = []
        vehicle_type = "truck" if is_truck_spec(self.selected_spec) else "container"
        for spec in catalog_smallest_first(vehicle_type=vehicle_type):
            if all(can_fit_item(item, spec) for item in items):
                candidates.append(spec)
        return candidates
