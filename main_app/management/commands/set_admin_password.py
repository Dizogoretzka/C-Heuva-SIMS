import getpass

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


class Command(BaseCommand):
    help = 'Set a Django superuser password for Django Admin access.'

    def add_arguments(self, parser):
        parser.add_argument('username')

    def handle(self, *args, **options):
        user_model = get_user_model()
        try:
            admin = user_model.objects.get(
                username__iexact=options['username'],
                user_type='1',
                is_superuser=True,
            )
        except user_model.DoesNotExist as error:
            raise CommandError('Django superuser account was not found.') from error

        password = getpass.getpass('New administrator password: ')
        confirmation = getpass.getpass('Confirm new administrator password: ')
        if password != confirmation:
            raise CommandError('Passwords do not match.')
        try:
            validate_password(password, user=admin)
        except ValidationError as error:
            raise CommandError('; '.join(error.messages)) from error

        with transaction.atomic():
            admin.set_password(password)
            admin.failed_login_attempts = 0
            admin.locked_until = None
            admin.save(update_fields=[
                'password',
                'failed_login_attempts',
                'locked_until',
            ])
        self.stdout.write(self.style.SUCCESS(
            f'Django superuser password updated for {admin.username}.'
        ))
