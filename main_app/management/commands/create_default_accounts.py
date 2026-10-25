import getpass
import secrets
import string

from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from main_app.credentials import issue_pin, validate_pin
from main_app.models import CustomUser


DEFAULT_ACCOUNTS = (
    ('mbangulap-admin', '1', 'SIMS', 'Administrator'),
    ('mbangulap-teacher', '3', 'SIMS', 'Teacher'),
    ('mbangulap', '2', 'SIMS', 'Learner'),
)


def _generate_learner_password():
    alphabet = string.ascii_letters + string.digits + '!@#$%&*+-_'
    while True:
        password = ''.join(
            secrets.choice(alphabet) for _ in range(24))
        try:
            validate_password(password)
        except ValidationError:
            continue
        return password


class Command(BaseCommand):
    help = (
        'Create role-separated starter accounts with unique credentials. '
        'Credentials are displayed once.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--reuse-credentials',
            action='store_true',
            help=(
                'Prompt for credentials to reproduce the same accounts in '
                'another environment.'
            ),
        )

    def handle(self, *args, **options):
        usernames = [entry[0] for entry in DEFAULT_ACCOUNTS]
        conflicts = list(
            CustomUser.objects.filter(username__in=usernames)
            .values_list('username', flat=True)
        )
        if conflicts:
            raise CommandError(
                'Starter accounts already exist or usernames are in use: '
                + ', '.join(conflicts)
                + '. No accounts were changed.'
            )

        learner_password = _generate_learner_password()
        admin_pin = None
        teacher_pin = None
        if options['reuse_credentials']:
            learner_password = getpass.getpass(
                'Learner password (hidden): ')
            try:
                validate_password(learner_password)
            except ValidationError as error:
                raise CommandError('; '.join(error.messages)) from error
            admin_pin = getpass.getpass('Administrator PIN (hidden): ')
            teacher_pin = getpass.getpass('Teacher PIN (hidden): ')
            try:
                validate_pin(admin_pin)
                validate_pin(teacher_pin)
            except ValueError as error:
                raise CommandError(str(error)) from error

        credentials = {}
        with transaction.atomic():
            for username, role, first_name, last_name in DEFAULT_ACCOUNTS:
                account = CustomUser.objects.create_user(
                    username=username,
                    password=(
                        learner_password if role == '2'
                        else secrets.token_urlsafe(32)
                    ),
                    email='',
                    user_type=role,
                    first_name=first_name,
                    last_name=last_name,
                    gender='M',
                    address='',
                )
                if role == '1':
                    credentials['Administrator'] = issue_pin(
                        account, pin=admin_pin)
                elif role == '3':
                    credentials['Teacher'] = issue_pin(
                        account, pin=teacher_pin)
                else:
                    credentials['Learner'] = learner_password

        self.stdout.write(self.style.SUCCESS(
            'Starter accounts created. Save these credentials securely; '
            'they will not be shown again.'
        ))
        role_labels = {
            '1': 'Administrator',
            '2': 'Learner',
            '3': 'Teacher',
        }
        for username, role, _first_name, _last_name in DEFAULT_ACCOUNTS:
            role_label = role_labels[role]
            self.stdout.write(
                f'{role_label}: username={username}, '
                f'{"PIN" if role in ("1", "3") else "password"}='
                f'{credentials[role_label]}'
            )
        if not options['reuse_credentials']:
            self.stdout.write(
                'Run this command with --reuse-credentials in another '
                'environment and enter the same values to use matching '
                'credentials there.'
            )
