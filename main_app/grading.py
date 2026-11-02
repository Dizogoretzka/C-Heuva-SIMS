from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.conf import settings

from .models import AssessmentWeight, GradeBand


NOT_GRADED = 'Not graded'


def get_assessment_weights(subject, academic_period):
    if not subject or not academic_period:
        return None
    weights = AssessmentWeight.objects.filter(
        subject=subject,
        academic_period=academic_period,
    ).first()
    if not weights:
        return None
    tests = get_test_weights(weights)
    if not tests:
        return None
    values = [weight for _, weight in tests]
    values.append(weights.exam_weight)
    if (
        any(value is None or value < 0 or value > 100 for value in values)
        or sum(values) != Decimal('100')
    ):
        return None
    return weights


def get_test_weights(weights):
    if weights is None:
        return []
    configured = weights.test_weights
    if not configured:
        configured = [
            {
                'name': f'Test {number}',
                'weight': str(getattr(weights, f'test_{number}_weight')),
            }
            for number in range(1, 6)
        ]
    try:
        return [
            (item['name'], Decimal(str(item['weight'])))
            for item in configured
        ]
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return []


def get_test_scores(result, weights=None):
    configured = get_test_weights(weights) if weights else []
    count = len(configured) or 5
    saved_scores = result.test_scores or {}
    return [
        saved_scores.get(
            name,
            getattr(result, f'test_{number}', 0),
        )
        for number, (name, _) in enumerate(configured[:count], start=1)
    ] if configured else [
        saved_scores.get(
            f'Test {number}',
            getattr(result, f'test_{number}', 0),
        )
        for number in range(1, count + 1)
    ]


def calculate_final_mark(result, weights=None):
    period = result.academic_period or result.student.academic_period
    weights = weights or get_assessment_weights(result.subject, period)
    if weights is None:
        return None

    test_weights = get_test_weights(weights)
    scores = [
        Decimal(str(score))
        for score in get_test_scores(result, weights)
    ]
    scores.append(Decimal(str(result.exam)))
    if any(score < 0 or score > settings.SIMS_MAX_TOTAL_SCORE for score in scores):
        return None

    weighted_scores = [
        score * weight / Decimal('100')
        for score, (_, weight) in zip(scores, test_weights)
    ]
    weighted_scores.append(
        Decimal(str(result.exam)) * weights.exam_weight / Decimal('100'))
    return sum(weighted_scores)


def get_grade_band(total_score, bands=None):
    if total_score is None:
        return NOT_GRADED
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
