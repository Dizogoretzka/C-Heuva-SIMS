from .models import GradeBand


NOT_GRADED = 'Not graded'


def get_grade_band(total_score, bands=None):
    if bands is None:
        bands = GradeBand.objects.order_by('minimum_score', 'maximum_score')

    for band in bands:
        if band.minimum_score <= total_score <= band.maximum_score:
            return band
    return NOT_GRADED
