from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from schedules.models import EmployeeInitialBalance
from schedules.services import (
    initial_balance_rebuild_is_suppressed,
    rebuild_balances_for_employee_from_earliest_schedule,
)


@receiver(post_save, sender=EmployeeInitialBalance)
def rebuild_schedule_balances_after_initial_balance_save(sender, instance, raw=False, **kwargs):
    if raw or initial_balance_rebuild_is_suppressed():
        return
    rebuild_balances_for_employee_from_earliest_schedule(instance.employee_identifier)


@receiver(post_delete, sender=EmployeeInitialBalance)
def rebuild_schedule_balances_after_initial_balance_delete(sender, instance, **kwargs):
    if initial_balance_rebuild_is_suppressed():
        return
    rebuild_balances_for_employee_from_earliest_schedule(instance.employee_identifier)
