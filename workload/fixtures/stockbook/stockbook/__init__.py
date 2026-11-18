"""Functions over inventory items: dicts with the keys sku, qty and price."""


def total_units(items):
    return sum(item["qty"] for item in items)


def stock_value(items):
    return sum(item["qty"] * item["price"] for item in items)


def average_price(items):
    return sum(item["price"] for item in items) / len(items)
