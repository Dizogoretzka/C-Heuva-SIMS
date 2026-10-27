from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings

from .models import GradeBand


NOT_GRADED = 'Not graded'


def get_grade_band(total_score, bands=None):
    score = Decimal(str(total_score))
    if score < 0 or score > settings.SIMS_MAX_TOTAL_SCORE:
        return NOT_GRADED
    # Round halves up before selecting a whole-number grade band.
    score = score.quantize(Decimal('1'), rounding=ROUND_HALF_UP)
    if bands is None:
        bands = GradeBand.objects.order_by('minimum_score', 'maximum_score')

    for band in bands:
        if band.minimum_score <= score <= band.maximum_score:
            return band
    return NOT_GRADED
