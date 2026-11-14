from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.serializers.base import DeserializationError
from django.db import IntegrityError
from django.db.models import Q
from django.db.models.deletion import ProtectedError
from django.http import HttpResponseForbidden, HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from .models import (
    AcademicPeriod,
    AssessmentPlan,
    CustomUser,
    RecycleBinEntry,
    Student,
    StudentResult,
    Teacher,
)
from .recycle_bin import (
    RestoreConflictError,
    permanently_delete_entry,
    restore_entry,
    trash_queryset,
)
from .scoping import visible_results, visible_students


_BULK_DELETE_CONFIG = {
    'learners': {
        'route': 'manage_student',
        'title': 'learner',
        'filters': ('teacher', 'school_class', 'search'),
    },
    'teachers': {
        'route': 'manage_teachers',
        'title': 'teacher',
        'filters': ('search',),
    },
    'academic-periods': {
        'route': 'manage_academic_periods',
        'title': 'academic period',
        'filters': ('search',),
    },
    'results': {
        'route': 'manage_student_results',
        'title': 'result',
        'filters': ('school_class', 'subject', 'academic_period', 'search'),
    },
}


def _search_learners(queryset, search):
    if search:
        queryset = queryset.filter(
            Q(admin__first_name__icontains=search)
            | Q(admin__last_name__icontains=search)
            | Q(admin__username__icontains=search)
        )
    return CustomUser.objects.filter(student__in=queryset).distinct()


def _bulk_queryset(request, data_type):
    if data_type == 'learners':
        if request.user.user_type not in ('1', '3'):
            return None
        queryset = visible_students(request.user)
        if request.user.user_type == '1':
            teacher_id = request.POST.get('teacher', '')
            class_id = request.POST.get('school_class', '')
            if teacher_id.isdigit():
                queryset = queryset.filter(
                    teacher_requests__teacher_id=teacher_id,
                    teacher_requests__status='accepted',
                ).distinct()
            if class_id.isdigit():
                queryset = queryset.filter(school_class_id=class_id)
        return _search_learners(queryset, request.POST.get('search', '').strip())

    if data_type == 'teachers':
        if request.user.user_type != '1':
            return None
        queryset = Teacher.objects.select_related('teacher')
        search = request.POST.get('search', '').strip()
        if search:
            queryset = queryset.filter(
                Q(teacher__first_name__icontains=search)
                | Q(teacher__last_name__icontains=search)
                | Q(teacher__username__icontains=search)
            )
        return queryset

    if data_type == 'academic-periods':
        if request.user.user_type != '1':
            return None
        queryset = AcademicPeriod.objects.all()
        search = request.POST.get('search', '').strip()
        digits = [int(value) for value in search.split() if value.isdigit()]
        if digits:
            queryset = queryset.filter(
                Q(year__in=digits)
                | Q(term__in=digits)
                | Q(start_year__year__in=digits)
                | Q(end_year__year__in=digits)
            ).distinct()
        elif search and search.lower() == 'current':
            queryset = queryset.filter(is_current=True)
        elif search:
            queryset = queryset.none()
        return queryset

    if data_type == 'results':
        if request.user.user_type not in ('1', '3'):
            return None
        queryset = visible_results(request.user)
        for key, field in (
            ('school_class', 'subject__school_class_id'),
            ('subject', 'subject_id'),
            ('academic_period', 'academic_period_id'),
        ):
            value = request.POST.get(key, '')
            if value.isdigit():
                queryset = queryset.filter(**{field: value})
        search = request.POST.get('search', '').strip()
        if search:
            queryset = queryset.filter(
                Q(student__admin__first_name__icontains=search)
                | Q(student__admin__last_name__icontains=search)
                | Q(student__admin__username__icontains=search)
                | Q(subject__name__icontains=search)
            )
        return queryset.distinct()
    return None


@login_required
def recycle_bin(request):
    entries = RecycleBinEntry.objects.filter(owner=request.user)
    record_type = request.GET.get('type', '').strip()
    search = request.GET.get('search', '').strip()
    if record_type:
        entries = entries.filter(record_type=record_type)
    if search:
        entries = entries.filter(
            Q(record_label__icontains=search)
            | Q(record_type__icontains=search)
        )
    types = RecycleBinEntry.objects.filter(
        owner=request.user).values_list('record_type', flat=True).distinct()
    return render(request, 'main_app/recycle_bin.html', {
        'entries': entries,
        'record_types': [(value, value.replace('-', ' ').title())
                         for value in types],
        'selected_type': record_type,
        'search_query': search,
        'page_title': 'Recycle Bin',
    })


@login_required
@require_POST
def bulk_delete(request, data_type):
    config = _BULK_DELETE_CONFIG.get(data_type)
    if config is None:
        return HttpResponseForbidden('Unsupported bulk-delete type.')
    queryset = _bulk_queryset(request, data_type)
    if queryset is None:
        return HttpResponseForbidden(
            'You do not have permission to delete these records.')

    record_ids = list(queryset.values_list('pk', flat=True))
    if data_type == 'academic-periods':
        protected_periods = AcademicPeriod.objects.filter(
            pk__in=record_ids,
        ).filter(
            Q(results__isnull=False)
            | Q(assessment_plans__isnull=False)
            | Q(student__studentresult__isnull=False)
        ).values_list('pk', flat=True).distinct()
        protected_ids = set(protected_periods)
        record_ids = [pk for pk in record_ids if pk not in protected_ids]
        if protected_ids:
            messages.warning(
                request,
                f'{len(protected_ids)} academic period(s) with results or '
                'assessment settings were not deleted.',
            )

    queryset = queryset.filter(pk__in=record_ids)
    count = queryset.count()
    if not count:
        messages.warning(request, 'No matching records were available to delete.')
    else:
        try:
            entry = trash_queryset(
                queryset,
                request.user,
                data_type,
                f'{count} {config["title"]} record(s)',
            )
        except ProtectedError:
            messages.error(
                request,
                'These records cannot be deleted while protected related '
                'records still depend on them.',
            )
        else:
            if entry:
                messages.success(
                    request,
                    f'{count} {config["title"]} record(s) moved to the recycle bin.',
                )

    params = {
        key: request.POST[key]
        for key in config['filters']
        if request.POST.get(key)
    }
    url = reverse(config['route'])
    if params:
        url = f'{url}?{urlencode(params)}'
    return redirect(url)


@login_required
@require_POST
def restore_recycle_bin_entry(request, entry_id):
    entry = get_object_or_404(
        RecycleBinEntry, pk=entry_id, owner=request.user)
    try:
        restore_entry(entry)
    except RestoreConflictError as error:
        messages.error(request, f'Could not restore this item: {error}')
    except (DeserializationError, IntegrityError, LookupError):
        messages.error(
            request,
            'Could not restore this item because a related record conflicts '
            'with the current data. No records were changed.',
        )
    else:
        messages.success(request, 'Deleted records restored successfully.')
    return redirect(reverse('recycle_bin'))


@login_required
@require_POST
def permanently_delete_recycle_bin_entry(request, entry_id):
    entry = get_object_or_404(
        RecycleBinEntry, pk=entry_id, owner=request.user)
    permanently_delete_entry(entry)
    messages.success(request, 'Recycle-bin item permanently deleted.')
    return redirect(reverse('recycle_bin'))


@login_required
def delete_student_records(request, student_id):
    if request.user.user_type not in ('1', '3'):
        return HttpResponseForbidden(
            'You do not have permission to delete learner accounts.')
    student = get_object_or_404(
        visible_students(request.user), pk=student_id)
    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])
    try:
        trash_queryset(
            CustomUser.objects.filter(pk=student.admin_id),
            request.user,
            'learners',
            f'{student.admin.last_name}, {student.admin.first_name}',
        )
    except ProtectedError:
        messages.error(
            request,
            'This learner cannot be deleted while protected records depend '
            'on the account.',
        )
    else:
        messages.success(request, 'Learner moved to the recycle bin.')
    return redirect(reverse('manage_student'))


@login_required
@require_POST
def delete_teacher_records(request, teacher_id):
    if request.user.user_type != '1':
        return HttpResponseForbidden(
            'You do not have permission to delete teacher accounts.')
    teacher = get_object_or_404(Teacher, pk=teacher_id)
    try:
        trash_queryset(
            CustomUser.objects.filter(pk=teacher.teacher_id),
            request.user,
            'teachers',
            str(teacher),
        )
    except ProtectedError:
        messages.error(
            request,
            'This teacher cannot be deleted while protected records depend '
            'on the account.',
        )
    else:
        messages.success(request, 'Teacher account moved to the recycle bin.')
    return redirect(reverse('manage_teachers'))


@login_required
@require_POST
def delete_academic_period_record(request, period_id):
    if request.user.user_type != '1':
        return HttpResponseForbidden(
            'You do not have permission to delete academic periods.')
    period = get_object_or_404(AcademicPeriod, pk=period_id)
    has_results = Student.objects.filter(
        academic_period=period,
        studentresult__isnull=False,
    ).exists() or StudentResult.objects.filter(
        academic_period=period).exists()
    has_assessments = AssessmentPlan.objects.filter(
        academic_period=period).exists()
    if has_results or has_assessments:
        messages.error(
            request,
            'This academic period cannot be deleted because it has results '
            'or assessment settings.',
        )
    else:
        trash_queryset(
            AcademicPeriod.objects.filter(pk=period.pk),
            request.user,
            'academic-periods',
            str(period),
        )
        messages.success(request, 'Academic period moved to the recycle bin.')
    return redirect(reverse('manage_academic_periods'))


@login_required
def delete_student_result_record(request, result_id):
    if request.user.user_type not in ('1', '3'):
        return HttpResponseForbidden(
            'You do not have permission to delete learner results.')
    result = get_object_or_404(visible_results(request.user), pk=result_id)
    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])
    trash_queryset(
        StudentResult.objects.filter(pk=result.pk),
        request.user,
        'results',
        str(result),
    )
    messages.success(request, 'Learner result moved to the recycle bin.')
    return redirect(reverse('manage_student_results'))
