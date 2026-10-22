from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from main_app.credentials import issue_pin


class Command(BaseCommand):
    help = 'Generate a new PIN for an administrator.'

    def add_arguments(self, parser):
        parser.add_argument('username')

    def handle(self, *args, **options):
        user_model = get_user_model()
        try:
            admin = user_model.objects.get(
                username__iexact=options['username'],
                user_type='1',
            )
        except user_model.DoesNotExist as error:
            raise CommandError('Administrator account was not found.') from error

        with transaction.atomic():
            pin = issue_pin(admin)
        self.stdout.write(self.style.SUCCESS(
            f'One-time PIN for {admin.username}: {pin}'
        ))
