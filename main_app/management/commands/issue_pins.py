import csv
import tempfile
from pathlib import Path

from django.contrib.auth.hashers import make_password
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from main_app.credentials import generate_pin


class Command(BaseCommand):
    help = (
        'Issue staff PINs, disable staff passwords, and write a one-time CSV '
        'for secure distribution.'
    )

    def add_arguments(self, parser):
        parser.add_argument('csv_path', help='Path to the one-time PIN CSV.')

    def handle(self, *args, **options):
        user_model = get_user_model()
        output_path = Path(options['csv_path']).expanduser().resolve()
        if not output_path.parent.is_dir():
            raise CommandError(
                f'Output directory does not exist: {output_path.parent}')
        if output_path.exists():
            raise CommandError(
                f'Output file already exists; choose a new path: {output_path}')

        users = list(
            user_model.objects.filter(
                user_type__in=('1', '3'),
                is_superuser=False,
            ).order_by('pk')
        )
        learners_with_pins = list(
            user_model.objects.filter(user_type='2')
            .exclude(pin_hash='')
            .order_by('pk')
        )
        if any(not user.username for user in users):
            raise CommandError(
                'Run create_usernames first so every account has a username.')

        issued_at = timezone.now()
        updates = []
        rows = []
        for user in users:
            pin = generate_pin()
            user.set_unusable_password()
            user.pin_hash = make_password(pin)
            user.pin_issued_at = issued_at
            user.failed_login_attempts = 0
            user.locked_until = None
            updates.append(user)
            rows.append([
                user.get_full_name().strip(),
                user.get_user_type_display(),
                user.username,
                pin,
            ])
        for learner in learners_with_pins:
            learner.pin_hash = ''
            learner.pin_issued_at = None

        temporary_path = None
        database_updated = False
        try:
            with tempfile.NamedTemporaryFile(
                mode='w',
                newline='',
                encoding='utf-8',
                dir=output_path.parent,
                delete=False,
            ) as output_file:
                temporary_path = Path(output_file.name)
                writer = csv.writer(output_file)
                writer.writerow(['name', 'role', 'username', 'PIN'])
                writer.writerows(rows)

            with transaction.atomic():
                user_model.objects.bulk_update(
                    updates,
                    [
                        'pin_hash',
                        'pin_issued_at',
                        'password',
                        'failed_login_attempts',
                        'locked_until',
                    ],
                )
                user_model.objects.bulk_update(
                    learners_with_pins,
                    ['pin_hash', 'pin_issued_at'],
                )
            database_updated = True
            temporary_path.replace(output_path)
        except OSError as error:
            if database_updated:
                raise CommandError(
                    'PINs were issued, but the CSV could not be moved. '
                    f'Recover the output from {temporary_path}: {error}'
                ) from error
            raise CommandError(
                f'Could not write the PIN CSV: {error}') from error
        finally:
            if (
                not database_updated
                and temporary_path
                and temporary_path.exists()
            ):
                temporary_path.unlink()

        self.stdout.write(self.style.SUCCESS(
            f'Issued {len(rows)} staff PINs and disabled staff passwords; '
            f'cleared {len(learners_with_pins)} legacy learner PINs; '
            f'wrote {output_path}. '
            'Hand the PINs securely to their account holders, then delete '
            'the CSV and any copies.'
        ))
