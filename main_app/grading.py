from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.db.models import Q

from .models import AssessmentPlan


GRADING_SCALE = (
    {
        'symbol': 'A*',
        'minimum': Decimal('90.00'),
        'maximum': Decimal('100.00'),
        'description': 'Excellent',
        'us_grade': 'US A+',
    },
    {
        'symbol': 'A',
        'minimum': Decimal('80.00'),
        'maximum': Decimal('89.99'),
        'description': 'Excellent',
        'us_grade': 'US A',
    },
    {
        'symbol': 'B',
        'minimum': Decimal('70.00'),
        'maximum': Decimal('79.99'),
        'description': 'Very good',
        'us_grade': 'US A-',
    },
    {
        'symbol': 'C',
        'minimum': Decimal('60.00'),
        'maximum': Decimal('69.99'),
        'description': 'Good',
        'us_grade': 'US B',
    },
    {
        'symbol': 'D',
        'minimum': Decimal('50.00'),
        'maximum': Decimal('59.99'),
        'description': 'Sufficient',
        'us_grade': 'US C+',
    },
    {
        'symbol': 'E',
        'minimum': Decimal('40.00'),
        'maximum': Decimal('49.99'),
        'description': 'Mediocre',
        'us_grade': 'US C',
    },
    {
        'symbol': 'F',
        'minimum': Decimal('30.00'),
        'maximum': Decimal('39.99'),
        'description': 'Poor',
        'us_grade': 'US D+',
    },
    {
        'symbol': 'G',
        'minimum': Decimal('20.00'),
        'maximum': Decimal('29.99'),
        'description': 'Very poor',
        'us_grade': 'US D',
    },
    {
        'symbol': 'U',
        'minimum': Decimal('0.00'),
        'maximum': Decimal('19.99'),
        'description': 'Ungraded',
        'us_grade': 'US F',
    },
)


def assessment_plan_for(subject, school_class, academic_period):
    if not subject or not school_class or not academic_period:
        return None
    return AssessmentPlan.objects.filter(
        subject=subject,
        school_class=school_class,
        academic_period=academic_period,
    ).first()


def active_assessments(plan):
    if plan is None:
        return []
    return list(
        plan.assessments.filter(is_previous_record=False).order_by(
            'position', 'pk')
    )


def assessment_score(result, assessment):
    value = (result.assessment_scores or {}).get(str(assessment.pk))
    if value in (None, ''):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def assessment_weight_total(assessments):
    return sum(
        (Decimal(str(item.weight)) for item in assessments),
        Decimal('0'),
    )


def calculate_final_mark(result, assessments=None):
    if assessments is None:
        plan = assessment_plan_for(
            result.subject,
            result.subject.school_class or result.student.school_class,
            result.academic_period or result.student.academic_period,
        )
        assessments = active_assessments(plan)
    if not assessments or assessment_weight_total(assessments) != Decimal('100'):
        return None

    weighted_scores = []
    for assessment in assessments:
        score = assessment_score(result, assessment)
        maximum = Decimal(str(assessment.maximum_mark))
        if (
            score is None
            or maximum <= 0
            or score < 0
            or score > maximum
        ):
            return None
        weighted_scores.append(
            score / maximum * Decimal(str(assessment.weight)))

    return sum(weighted_scores, Decimal('0')).quantize(
        Decimal('0.01'), rounding=ROUND_HALF_UP)


def prepare_result(result):
    plan = assessment_plan_for(
        result.subject,
        result.subject.school_class or result.student.school_class,
        result.academic_period or result.student.academic_period,
    )
    assessments = active_assessments(plan)
    previous_assessment = None
    if plan:
        previous_assessment = plan.assessments.filter(
            is_previous_record=True).first()
    display_assessments = (
        [previous_assessment]
        if result.is_previous_record and previous_assessment
        else assessments
    )
    result.assessment_plan = plan
    result.display_assessments = display_assessments
    result.assessment_values = [
        (item, assessment_score(result, item))
        for item in display_assessments
    ]
    result.weight_total = assessment_weight_total(assessments)
    result.total_score = calculate_final_mark(
        result,
        [previous_assessment]
        if result.is_previous_record and previous_assessment
        else assessments,
    )
    result.grade = get_grade(result.total_score)
    return result


def get_grade(mark):
    if mark is None:
        return None
    try:
        value = Decimal(str(mark)).quantize(
            Decimal('0.01'), rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError):
        return None
    if value < Decimal('0.00') or value > Decimal('100.00'):
        return None
    return next(
        (
            grade for grade in GRADING_SCALE
            if grade['minimum'] <= value <= grade['maximum']
        ),
        None,
    )


def previous_period_plan(subject, school_class, academic_period):
    if not subject or not school_class or not academic_period:
        return None
    periods = academic_period.__class__.objects.exclude(
        pk=academic_period.pk)
    if academic_period.year is not None and academic_period.term is not None:
        periods = periods.filter(
            year__isnull=False,
            term__isnull=False,
        ).filter(
            Q(year__lt=academic_period.year)
            | Q(year=academic_period.year, term__lt=academic_period.term)
        )
        previous_period = periods.order_by('-year', '-term').first()
    elif academic_period.start_year is not None:
        previous_period = periods.filter(
            start_year__lt=academic_period.start_year,
        ).order_by('-start_year', '-end_year').first()
    else:
        return None
    if previous_period is None:
        return None
    return AssessmentPlan.objects.filter(
        school_class=school_class,
        subject=subject,
        academic_period=previous_period,
    ).first()
