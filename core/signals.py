from django.contrib.auth import get_user_model
from django.db.models.signals import post_save
from django.dispatch import receiver

from core.models import UserSiteAccess

User = get_user_model()


@receiver(post_save, sender=User)
def ensure_user_site_access(sender, instance, created, **kwargs):
    if created:
        UserSiteAccess.objects.get_or_create(user=instance)

