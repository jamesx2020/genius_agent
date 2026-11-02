"""Deterministic incremental cost; amounts are integer cents, never LLM arithmetic."""
from decimal import Decimal, ROUND_HALF_UP

def quote_cost(quote:dict,order:dict)->dict:
    mode=quote.get('pricing_mode')
    structured_legacy=quote.get('quantity') and quote.get('available_date')
    if mode=='inclusive_delta' or (mode!='unit_price_plus_fees' and not structured_legacy):
        # Original seed quotes explicitly contain a pre-calculated inclusive delta.
        return {'additional_cents':int(quote['additional_cents']),'pricing_mode':'inclusive_delta'}
    quantity=int(quote['quantity'])
    if quantity<1 or quantity>int(order['quantity']):
        raise ValueError('报价数量超出订单范围')
    original=Decimal(order['amount_cents'])*Decimal(quantity)/Decimal(order['quantity'])
    difference=int((Decimal(quote['unit_price_cents'])*quantity-original).quantize(Decimal('1'),rounding=ROUND_HALF_UP))
    fee=int(quote.get('service_fee_cents',quote['additional_cents']))
    return {'pricing_mode':'unit_price_plus_fees','price_difference_cents':difference,'service_fee_cents':fee,'additional_cents':difference+fee}
