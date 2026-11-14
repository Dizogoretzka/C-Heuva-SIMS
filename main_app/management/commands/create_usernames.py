import csv
import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from main_app.account_ids import generate_account_id

class Command(BaseCommand):
    help = (
        'Create account IDs for accounts without one and write a one-time CSV '
        'for distribution.'
    )

    def add_arguments(self, parser):
        parser.add_argument('csv_path', help='Path to the one-time CSV output.')

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
                username__isnull=True,
            ).order_by('pk')
        )
        reserved = {
            value.casefold()
            for value in user_model.objects.exclude(username__isnull=True)
            .values_list('username', flat=True)
            if value
        }
        updates = []
        rows = []

        for user in users:
            try:
                account_id = generate_account_id(
                    user.first_name, user.last_name, reserved)
            except ValueError as error:
                raise CommandError(
                    f'Cannot generate an account ID for account {user.pk}: '
                    f'{error}') from error
            reserved.add(account_id.casefold())
            user.username = account_id
            updates.append(user)
            rows.append([
                user.get_full_name().strip(),
                user.get_user_type_display(),
                account_id,
            ])

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
                writer.writerow(['name', 'role', 'account_id'])
                writer.writerows(rows)

            with transaction.atomic():
                user_model.objects.bulk_update(updates, ['username'])
            database_updated = True
            temporary_path.replace(output_path)
        except OSError as error:
            if database_updated:
                raise CommandError(
                    'Account IDs were assigned, but the CSV could not be moved. '
                    f'Recover the output from {temporary_path}: {error}'
                ) from error
            raise CommandError(
                f'Could not write the account ID CSV: {error}') from error
        finally:
            if (
                not database_updated
                and temporary_path
                and temporary_path.exists()
            ):
                temporary_path.unlink()

        self.stdout.write(self.style.SUCCESS(
            f'Created {len(rows)} account IDs and wrote {output_path}. '
            'Hand each account ID to its account holder.'
        ))
