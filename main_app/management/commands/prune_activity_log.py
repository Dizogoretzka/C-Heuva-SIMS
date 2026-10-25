from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from main_app.models import ActivityLog


class Command(BaseCommand):
    help = 'Delete activity-log entries older than the configured retention.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--days',
            type=int,
            help='Override SIMS_ACTIVITY_LOG_RETENTION_DAYS for this run.',
        )

    def handle(self, *args, **options):
        retention_days = options['days']
        if retention_days is None:
            retention_days = settings.SIMS_ACTIVITY_LOG_RETENTION_DAYS
        if retention_days < 1:
            raise CommandError('Activity-log retention must be at least one day.')

        cutoff = timezone.now() - timedelta(days=retention_days)
        old_entries = ActivityLog.objects.filter(created_at__lt=cutoff)
        deleted_count = old_entries.count()
        old_entries.delete()
        self.stdout.write(self.style.SUCCESS(
            f'Deleted {deleted_count} activity-log entries older than '
            f'{retention_days} days.'))
