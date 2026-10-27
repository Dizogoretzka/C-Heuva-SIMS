from django.conf import settings
from django.core.management.base import BaseCommand

from main_app.models import StudentResult


class Command(BaseCommand):
    help = 'List existing results outside the configured score limits.'

    def handle(self, *args, **options):
        maximum = settings.SIMS_MAX_TOTAL_SCORE
        results = StudentResult.objects.select_related(
            'student__admin', 'subject').order_by('pk')
        invalid_count = 0

        for result in results.iterator():
            total = result.test + result.exam
            problems = []
            if result.test < 0 or result.exam < 0 or total < 0:
                problems.append('below zero')
            if total > maximum:
                problems.append(f'above maximum {maximum}')
            if not problems:
                continue

            invalid_count += 1
            learner = (
                result.student.admin.get_full_name()
                or result.student.admin.username
            )
            self.stdout.write(
                f'Result {result.pk}: learner={learner}; '
                f'subject={result.subject.name}; test={result.test:g}; '
                f'exam={result.exam:g}; total={total:g}; '
                f'issue={", ".join(problems)}'
            )

        if invalid_count:
            self.stdout.write(
                self.style.WARNING(
                    f'Found {invalid_count} result(s) outside 0..{maximum}. '
                    'No records were changed.'
                )
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f'All existing result totals are within 0..{maximum}.'
                )
            )
