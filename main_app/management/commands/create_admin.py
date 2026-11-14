from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from main_app.credentials import issue_pin
from main_app.models import CustomUser


class Command(BaseCommand):
    help = 'Create an administrator account and display its one-time PIN.'

    def handle(self, *args, **options):
        first_name = input('First name: ').strip()
        last_name = input('Last name: ').strip()
        if not first_name or not last_name:
            raise CommandError('First and last names are required.')

        gender = input('Gender (M/F): ').strip().upper()
        if gender not in {'M', 'F'}:
            raise CommandError('Gender must be M or F.')

        with transaction.atomic():
            admin = CustomUser.objects.create_user(
                password=None,
                email='',
                user_type='1',
                first_name=first_name,
                last_name=last_name,
                gender=gender,
                address='',
            )
            pin = issue_pin(admin)

        self.stdout.write(self.style.SUCCESS(
            f'Administrator {admin.username} created with an unusable password. '
            f'One-time PIN: {pin}'
        ))
