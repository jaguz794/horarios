from django.conf import settings
from django.core.mail import EmailMessage

from schedules.models import WeeklySchedule
from schedules.reporting import build_hourly_coverage_report_attachment


def get_review_report_recipients(schedule: WeeklySchedule) -> list[str]:
    recipients = [email for email in getattr(settings, "SCHEDULE_REVIEW_REPORT_RECIPIENTS", []) if email]
    if recipients:
        return recipients

    updated_by = getattr(schedule, "updated_by", None)
    if updated_by and getattr(updated_by, "email", "").strip():
        return [updated_by.email.strip()]

    return []


def send_schedule_review_report(schedule: WeeklySchedule) -> tuple[bool, str]:
    recipients = get_review_report_recipients(schedule)
    if not recipients:
        return False, "No hay destinatarios configurados para enviar el informe de cobertura en revision."

    attachment_name, attachment_bytes = build_hourly_coverage_report_attachment(schedule)
    email = EmailMessage(
        subject=(
            f"Horario en revision - {schedule.site.name} - "
            f"{schedule.week_start_date:%d/%m/%Y} al {schedule.week_end_date:%d/%m/%Y}"
        ),
        body=(
            "Se adjunta el informe de cobertura por franjas horarias del horario que paso a estado En revision.\n\n"
            f"Sede: {schedule.site.name}\n"
            f"Semana: {schedule.week_start_date:%d/%m/%Y} al {schedule.week_end_date:%d/%m/%Y}\n"
            f"Estado actual: {schedule.get_status_display()}\n"
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=recipients,
    )
    email.attach(
        attachment_name,
        attachment_bytes,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    email.send(fail_silently=False)
    return True, f"Informe de cobertura enviado a {', '.join(recipients)}."
