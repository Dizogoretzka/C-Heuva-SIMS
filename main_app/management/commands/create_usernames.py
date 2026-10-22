import csv
import re
import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


class Command(BaseCommand):
    help = (
        'Create usernames for accounts without one and write a one-time CSV '
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
            value.lower()
            for value in user_model.objects.exclude(username__isnull=True)
            .values_list('username', flat=True)
            if value
        }
        updates = []
        rows = []

        for user in users:
            username = self._available_username(user.email, reserved)
            reserved.add(username.lower())
            user.username = username
            updates.append(user)
            rows.append([
                user.get_full_name().strip(),
                user.get_user_type_display(),
                username,
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
                writer.writerow(['name', 'role', 'username'])
                writer.writerows(rows)

            with transaction.atomic():
                user_model.objects.bulk_update(updates, ['username'])
            database_updated = True
            temporary_path.replace(output_path)
        except OSError as error:
            if database_updated:
                raise CommandError(
                    'Usernames were assigned, but the CSV could not be moved. '
                    f'Recover the output from {temporary_path}: {error}'
                ) from error
            raise CommandError(
                f'Could not write the username CSV: {error}') from error
        finally:
            if (
                not database_updated
                and temporary_path
                and temporary_path.exists()
            ):
                temporary_path.unlink()

        self.stdout.write(self.style.SUCCESS(
            f'Created {len(rows)} usernames and wrote {output_path}. '
            'Hand each username to its account holder.'
        ))

    @staticmethod
    def _available_username(email, reserved):
        local_part = (email or '').split('@', 1)[0].lower()
        base = re.sub(r'[^a-z0-9._-]', '', local_part)
        base = re.sub(r'^[^a-z0-9]+', '', base)
        if not base:
            base = 'user'
        base = base[:20].ljust(4, '0')

        candidate = base
        suffix_number = 1
        while candidate.lower() in reserved:
            suffix = str(suffix_number)
            candidate = f'{base[:20 - len(suffix)]}{suffix}'
            suffix_number += 1
        return candidate
