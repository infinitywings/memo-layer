import stockbook

ITEMS = [
    {"sku": "100-3", "qty": 3, "price": 2.0},
    {"sku": "207-1", "qty": 4, "price": 0.5},
]


def test_total_units_adds_every_quantity():
    assert stockbook.total_units(ITEMS) == 7


def test_stock_value_weights_price_by_quantity():
    assert stockbook.stock_value(ITEMS) == 8.0


def test_average_price_is_the_plain_mean():
    assert stockbook.average_price(ITEMS) == 1.25
