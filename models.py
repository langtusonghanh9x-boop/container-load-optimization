from dataclasses import dataclass, field
from math import pi
from typing import Dict, List, Optional, Tuple


@dataclass(frozen=True)
class CargoItem:
    id: str
    name: str
    length_mm: float
    width_mm: float
    height_mm: float
    weight_kg: float
    color: str = "#7f8c8d"
    cargo_type: str = "General Cargo"
    loading_order: Optional[int] = None
    tilt_to_length: bool = False
    tilt_to_width: bool = False
    max_layers: Optional[int] = None
    max_stack_mass_kg: Optional[float] = None
    max_stack_height_mm: Optional[float] = None
    disable_stacking: bool = False
    shape: str = "box"

    @property
    def volume_mm3(self) -> float:
        if self.shape == "cylinder":
            diameter = min(self.width_mm, self.height_mm)
            return pi * (diameter / 2) ** 2 * self.length_mm
        return self.length_mm * self.width_mm * self.height_mm


@dataclass(frozen=True)
class ContainerSpec:
    name: str
    length_mm: float
    width_mm: float
    height_mm: float
    max_weight_kg: float

    @property
    def volume_m3(self) -> float:
        return (self.length_mm * self.width_mm * self.height_mm) / 1e9


@dataclass
class PackedItem:
    cargo: CargoItem
    position: Tuple[float, float, float]
    size: Tuple[float, float, float]


@dataclass
class PackedContainer:
    spec: ContainerSpec
    items: List[PackedItem] = field(default_factory=list)
    role: str = "Selected"

    @property
    def package_count(self) -> int:
        return len(self.items)

    @property
    def cargo_volume_m3(self) -> float:
        return sum(item.size[0] * item.size[1] * item.size[2] for item in self.items) / 1e9

    @property
    def cargo_weight_kg(self) -> float:
        return sum(item.cargo.weight_kg for item in self.items)

    @property
    def volume_pct(self) -> float:
        return (self.cargo_volume_m3 / self.spec.volume_m3 * 100) if self.spec.volume_m3 else 0

    @property
    def weight_pct(self) -> float:
        return (self.cargo_weight_kg / self.spec.max_weight_kg * 100) if self.spec.max_weight_kg else 0


@dataclass
class LoadingPlan:
    containers: List[PackedContainer]
    requested_count: int
    leftover_items: List[CargoItem]
    warnings: List[str] = field(default_factory=list)
    suggestions: List[str] = field(default_factory=list)
    diagnostics: Dict[str, float] = field(default_factory=dict)

    @property
    def loaded_count(self) -> int:
        return sum(container.package_count for container in self.containers)

    @property
    def leftover_count(self) -> int:
        return len(self.leftover_items)

    @property
    def total_volume_m3(self) -> float:
        return sum(container.cargo_volume_m3 for container in self.containers)

    @property
    def total_weight_kg(self) -> float:
        return sum(container.cargo_weight_kg for container in self.containers)


@dataclass(frozen=True)
class LoadingConfig:
    load_direction: str = "inside_out"
    heavy_priority: str = "heavy_bottom"
    placement_strategy: str = "stable_floor_first"
    max_additional_containers: int = 10
    minimum_support_ratio: float = 0.65
    contact_compaction: bool = True
    # Internal optimizer controls.  They are intentionally kept out of the
    # basic UI so normal users get the complete global search by default.
    packing_sequence: str = "largest_volume_first"
    search_limit: int = 35
    beam_width: int = 12
    beam_carton_limit: int = 20
    optimization_profile: str = "balanced"
    time_budget_seconds: float = 60.0
    parallel_search: bool = True
