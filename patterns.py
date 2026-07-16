"""Packing-pattern policies for the shared optimization engine.

Patterns contain policy only: they never select a different packing algorithm.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class PackingPattern:
    key: str
    initial_strategy: str
    sequences: tuple
    heuristics: tuple
    profile: str | None = None
    local_search: bool = True
    weights: tuple = (55, 15, 10, 8, 5, 3, 2, 2)


PATTERNS = {
    "balanced": PackingPattern("balanced", "bottom_left_fill", (), (), weights=(55, 15, 10, 8, 5, 3, 2, 2)),
    "fill_width_height": PackingPattern("fill_width_height", "fill_width_height", ("largest_base_first", "largest_volume_first"), ("fill_width_height", "best_volume_fit"), weights=(62, 18, 10, 8, 3, 2, 2, 1)),
    "fill_length_first": PackingPattern("fill_length_first", "fill_length_first", ("longest_first", "largest_volume_first"), ("fill_length_first", "best_volume_fit"), weights=(62, 18, 10, 7, 3, 2, 2, 1)),
    "wall_building": PackingPattern("wall_building", "wall_building", ("largest_base_first", "largest_volume_first"), ("wall_building", "best_contact_area"), weights=(58, 16, 15, 8, 3, 2, 3, 1)),
    "layer_by_layer": PackingPattern("layer_by_layer", "layer_by_layer", ("largest_base_first", "heaviest_first"), ("layer_by_layer", "lowest_z"), weights=(58, 16, 10, 14, 3, 5, 2, 1)),
    "maximum_utilization": PackingPattern("maximum_utilization", "best_volume_fit", (), (), profile="maximum", weights=(78, 25, 8, 5, 2, 1, 4, 1)),
    "weight_balanced": PackingPattern("weight_balanced", "weight_balanced", ("heaviest_first", "largest_base_first"), ("weight_balanced", "lowest_z", "best_contact_area"), weights=(42, 10, 12, 12, 20, 16, 2, 1)),
    "fast_loading": PackingPattern("fast_loading", "bottom_left_fill", ("largest_volume_first",), ("bottom_left_fill",), profile="fast", local_search=False, weights=(55, 15, 8, 7, 5, 3, 2, 1)),
}


def get_pattern(key):
    return PATTERNS.get(key, PATTERNS["balanced"])
