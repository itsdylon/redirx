"""October catalog: integer USD cents, separate trial, pooled monthly URLs."""
from dataclasses import dataclass

VERSION = 'redirx_2026_10'
STRIPE_API_VERSION = '2024-12-18.acacia'


@dataclass(frozen=True)
class Plan:
    name: str
    amount_cents: int
    included_old_urls: int
    live_price_id: str
    live_product_id: str


PLANS = {
    'payg': Plan('Pay as you go', 100, 0, 'price_1UQtSDReyTnSoYHgl5XT6yeK', 'prod_VRn28fHeo27BPb'),
    'studio': Plan('Studio', 7900, 5000, 'price_1UQtOEReyTnSoYHgA9L17plj', 'prod_VRmyQILrauV2ru'),
    'agency': Plan('Agency', 19900, 15000, 'price_1UQtP1ReyTnSoYHgBoleBYrl', 'prod_U8u0k94qbip2sY'),
    'additional_urls': Plan('Additional URLs', 100, 0, 'price_1UQtT5ReyTnSoYHg0ws7GKJr', 'prod_VRn3DAF4w5ccxm'),
}


def usage_cents(old_urls, *, minimum=False):
    if type(old_urls) is not int or old_urls < 0:
        raise ValueError('Old URL count must be a nonnegative integer.')
    return max(1000 if minimum else 0, ((old_urls + 49) // 50) * 100)


def validate_price(price, sku, price_id, livemode):
    """Reject old, archived, metered, tiered or wrong-environment prices."""
    plan = PLANS[sku]
    if (price.get('id') != price_id or price.get('livemode') is not livemode
            or price.get('active') is not True or price.get('currency') != 'usd'
            or type(price.get('unit_amount')) is not int or price['unit_amount'] != plan.amount_cents
            or price.get('billing_scheme') != 'per_unit' or price.get('tax_behavior') != 'exclusive'):
        raise ValueError('The configured Stripe price does not match the current catalog.')
    recurring = price.get('recurring')
    if sku in {'studio', 'agency'}:
        if (not isinstance(recurring, dict) or recurring.get('interval') != 'month'
                or recurring.get('interval_count') != 1 or recurring.get('usage_type') != 'licensed'
                or price.get('transform_quantity') is not None):
            raise ValueError('A fixed monthly subscription price is required.')
    elif recurring is not None or price.get('transform_quantity') != {'divide_by': 50, 'round': 'up'}:
        raise ValueError('Usage requires a one-time $1 package of 50 URLs, rounded up.')
    if livemode and price.get('product') != plan.live_product_id:
        raise ValueError('The price belongs to a different product.')
