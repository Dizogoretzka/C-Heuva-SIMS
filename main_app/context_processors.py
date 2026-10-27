from .models import AcademicPeriod


def current_academic_period(request):
    return {
        'current_academic_period': AcademicPeriod.objects.filter(
            is_current=True).first(),
    }
