from .containers import catalog_smallest_first, is_truck_spec
from .packing import can_fit_item


class ContainerManager:
    def __init__(self, selected_spec, selected_quantity=1, allow_auto_add=True):
        raw_specs = list(selected_spec) if isinstance(selected_spec, (list, tuple)) else [selected_spec]
        # Accept both a single specification and nested lists produced by
        # callers that combine multiple selected vehicles.
        self.selected_specs = []
        for spec in raw_specs:
            if isinstance(spec, (list, tuple)):
                self.selected_specs.extend(spec)
            else:
                self.selected_specs.append(spec)
        if not self.selected_specs:
            raise ValueError("At least one vehicle must be selected.")
        if not all(hasattr(spec, "name") for spec in self.selected_specs):
            raise TypeError("Selected vehicles must be container specifications.")
        self.selected_spec = self.selected_specs[0]
        self.selected_quantity = max(int(selected_quantity), 1)
        self.allow_auto_add = allow_auto_add

    def initial_specs(self):
        if len(self.selected_specs) > 1:
            return self.selected_specs
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
