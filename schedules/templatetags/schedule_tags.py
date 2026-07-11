from django import template
from decimal import Decimal, InvalidOperation

register = template.Library()


@register.filter
def form_field(form, field_name):
    return form[field_name]


@register.filter
def attr(instance, name):
    return getattr(instance, name)


@register.filter
def hours_int(value):
    try:
        decimal_value = Decimal(str(value)).normalize()
        rendered = format(decimal_value, "f")
        if "." in rendered:
            rendered = rendered.rstrip("0").rstrip(".")
        return rendered or "0"
    except (InvalidOperation, TypeError, ValueError):
        return value


@register.filter
def non_negative_hours_int(value):
    try:
        decimal_value = max(Decimal(str(value)), Decimal("0.00")).normalize()
        rendered = format(decimal_value, "f")
        if "." in rendered:
            rendered = rendered.rstrip("0").rstrip(".")
        return rendered or "0"
    except (InvalidOperation, TypeError, ValueError):
        return value
