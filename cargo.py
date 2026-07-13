INCH_TO_MM = 25.4


def product_rows_to_cargo_items(products):
    items = []
    for product in products:
        cargo_type = product.get("cargo_type", "General Cargo")
        length = float(product.get("l", 0))
        width = float(product.get("w", 0))
        height = float(product.get("h", 0))
        if cargo_type == "Lumber Bundle":
            length *= INCH_TO_MM
            width *= INCH_TO_MM
            height *= INCH_TO_MM

        for index in range(int(product.get("qty", 0))):
            from .models import CargoItem

            items.append(CargoItem(
                id=f"{product.get('name', 'Item')} #{index + 1}",
                name=str(product.get("name", "Item")),
                length_mm=length,
                width_mm=width,
                height_mm=height,
                weight_kg=float(product.get("wt", 0)),
                color=product.get("color", "#7f8c8d"),
                cargo_type=cargo_type,
                loading_order=product.get("loading_order"),
                tilt_to_length=bool(product.get("tilt_to_length", False)),
                tilt_to_width=bool(product.get("tilt_to_width", False)),
                max_layers=product.get("max_layers") or None,
                max_stack_mass_kg=product.get("max_stack_mass_kg") or None,
                max_stack_height_mm=product.get("max_stack_height_mm") or None,
                disable_stacking=bool(product.get("disable_stacking", False)),
            ))
    return items
