from django.conf import settings
from django.core.management.base import BaseCommand

from main_app.models import StudentResult


class Command(BaseCommand):
    help = 'List existing assessment scores outside configured limits.'

    def handle(self, *args, **options):
        maximum = settings.SIMS_MAX_TOTAL_SCORE
        results = StudentResult.objects.select_related(
            'student__admin', 'subject').order_by('pk')
        invalid_count = 0

        for result in results.iterator():
            scores = result.test_scores or {
                f'Test {number}': getattr(result, f'test_{number}')
                for number in range(1, 6)
            }
            scores['Exam'] = result.exam
            problems = []
            if any(score < 0 for score in scores.values()):
                problems.append('below zero')
            out_of_range = [
                f'{name}={score:g}'
                for name, score in scores.items()
                if score > maximum
            ]
            if out_of_range:
                problems.append(
                    f'above maximum {maximum}: {", ".join(out_of_range)}')
            if not problems:
                continue

            invalid_count += 1
            learner = (
                result.student.admin.get_full_name()
                or result.student.admin.username
            )
            self.stdout.write(
                f'Result {result.pk}: learner={learner}; '
                f'subject={result.subject.name}; '
                f'assessment scores={scores}; '
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
                    f'All existing assessment scores are within 0..{maximum}.'
                )
            )
