"""Validate delivery commitments against vendor records, independently of the LLM."""
from datetime import date

def delivery_option_error(option,quote,order,instruction):
    try:
        deliveries=option['deliveries']
        for item in deliveries:
            date.fromisoformat(item['date'])
        if sum(d['quantity'] for d in deliveries)!=order['quantity']:
            return '交付计划的总数量与订单不一致'
        if quote['valid_until']<date.today().isoformat():
            return '报价已过期'
        if not instruction:
            return None # Compatibility for original, pre-direction workflows.
        direction=instruction['direction']
        method=quote.get('fulfillment_type') or ('transfer' if quote['supplier']!=order['supplier'] else 'split')
        if direction in ('expedite','transfer'):
            if method!=direction:
                return '报价的履约方式不符合项目经理要求'
            if not quote.get('available_date'):
                return '供应商未明确承诺到货日期，请采购确认，不能由模型填写'
            date.fromisoformat(quote['available_date'])
            if not quote.get('quantity'):
                return '供应商未明确承诺供货数量'
            if quote['quantity']<instruction.get('required_quantity',0):
                return '报价数量不足以满足项目经理要求'
            if instruction.get('required_date') and quote['available_date']>instruction['required_date']:
                return '供应商承诺到货日期晚于现场要求'
            covered=sum(d['quantity'] for d in deliveries if d['date']>=quote['available_date'])
            if covered<quote['quantity']:
                return '计划把供应商的到货日期提前了，没有承诺依据'
        elif not quote.get('available_date') and not quote.get('delivery_commitments'):
            return '分批交货报价缺少明确的交付承诺'
        if instruction.get('required_date'):
            if sum(d['quantity'] for d in deliveries if d['date']<=instruction['required_date'])<instruction['required_quantity']:
                return '计划在现场要求日期前到货的数量不足'
        return None
    except (KeyError,TypeError,ValueError):
        return '报价或交付计划信息不完整'
