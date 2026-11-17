import ast
import json
import logging
import csv
import re
import secrets
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from xml.sax.saxutils import escape, unescape

import requests
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth import update_session_auth_hash
from django.core.files.storage import FileSystemStorage
from django.core.paginator import Paginator
from django.contrib.staticfiles import finders
from django.db import IntegrityError, OperationalError, transaction
from django.db.models import Count, ExpressionWrapper, F, FloatField, Prefetch, Q
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.templatetags.static import static
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import (
    require_GET,
    require_http_methods,
    require_POST,
)
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Image as ReportImage,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
import reportlab

from .forms import *
from .credentials import issue_pin
from .class_parser import ClassLabelError, parse_class_label
from .grading import (
    GRADING_SCALE,
    active_assessments,
    prepare_result,
    previous_period_plan,
)
from .learner_imports import (
    LEARNER_IMPORT_CHUNK_SIZE,
    LearnerImportError,
    parse_learner_upload,
    validate_learner_rows,
)
from .learner_ordering import learner_name_sort_key, sort_learners
from .models import *
from .scoping import (
    teacher_profile,
    visible_results,
    visible_students,
    visible_subjects,
)
from .views import render_pin_confirmation, update_profile_from_form
from .security_reset import security_question_profile_form
from .teacher_requests import (
    create_class_requests,
    create_requests_for_class,
    reset_class_requests,
)

logger = logging.getLogger(__name__)


def admin_home(request):
    total_students = Student.objects.all().count()
    recent_logins = LoginActivity.objects.select_related(
        'student__admin', 'student__school_class'
    ).order_by('-logged_in_at')[:5]
    context = {
        'page_title': "Administrative Dashboard",
        'security_question_missing': not bool(
            request.user.security_question),
        'total_students': total_students,
        'total_subject': Subject.objects.count(),
        'learners_logged_in_today': LoginActivity.objects.filter(
            logged_in_at__date=timezone.localdate()
        ).values('student_id').distinct().count(),
        'recent_logins': recent_logins,
    }
    return render(request, 'hod_template/home_content.html', context)


@login_required
def manage_academic_periods(request):
    if request.user.user_type != '1':
        return redirect(reverse('admin_home'))

    form = AcademicPeriodForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        period = form.save(commit=False)
        if not AcademicPeriod.objects.filter(is_current=True).exists():
            period.is_current = True
        period.save()
        messages.success(request, 'Academic period added successfully.')
        return redirect(reverse('manage_academic_periods'))

    search_query = request.GET.get('search', '').strip()
    periods = AcademicPeriod.objects.order_by(
        '-year', 'term', '-start_year', '-pk')
    if search_query:
        numbers = [int(value) for value in re.findall(r'\d+', search_query)]
        if numbers:
            periods = periods.filter(
                Q(year__in=numbers)
                | Q(term__in=numbers)
                | Q(start_year__year__in=numbers)
                | Q(end_year__year__in=numbers)
            ).distinct()
        elif search_query.casefold() == 'current':
            periods = periods.filter(is_current=True)
        else:
            periods = periods.none()
    return render(request, 'hod_template/manage_academic_periods.html', {
        'form': form,
        'periods': periods,
        'search_query': search_query,
        'page_title': 'Manage Academic Periods',
    })


@login_required
def school_profile(request):
    if request.user.user_type != '1':
        return redirect(reverse('admin_home'))

    profile, _ = SchoolProfile.objects.get_or_create(pk=1)
    form = SchoolProfileForm(
        request.POST or None,
        request.FILES or None,
        instance=profile,
    )
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, 'School profile updated successfully.')
        return redirect(reverse('school_profile'))
    return render(request, 'hod_template/school_profile.html', {
        'form': form,
        'profile': profile,
        'page_title': 'School Profile',
    })


@login_required
def edit_academic_period(request, period_id):
    if request.user.user_type != '1':
        return redirect(reverse('admin_home'))

    period = get_object_or_404(AcademicPeriod, pk=period_id)
    form = AcademicPeriodForm(request.POST or None, instance=period)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, 'Academic period updated successfully.')
        return redirect(reverse('manage_academic_periods'))
    return render(request, 'hod_template/edit_academic_period.html', {
        'form': form,
        'period': period,
        'page_title': 'Edit Academic Period',
    })


@login_required
def set_current_academic_period(request, period_id):
    if request.user.user_type != '1':
        return redirect(reverse('admin_home'))
    if request.method == 'POST':
        with transaction.atomic():
            current = AcademicPeriod.objects.filter(is_current=True).first()
            period = get_object_or_404(AcademicPeriod, pk=period_id)
            if current and current.pk != period.pk:
                current.is_current = False
                current.save(update_fields=['is_current'])
            if not period.is_current:
                period.is_current = True
                period.save(update_fields=['is_current'])
        messages.success(
            request, f'{period} is now the current academic period.')
    return redirect(reverse('manage_academic_periods'))


@login_required
def delete_academic_period(request, period_id):
    if request.user.user_type != '1':
        return redirect(reverse('admin_home'))
    period = get_object_or_404(AcademicPeriod, pk=period_id)
    if request.method == 'POST':
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
                'This academic period cannot be deleted because it has '
                'results or assessment settings.',
            )
        else:
            period.delete()
            messages.success(request, 'Academic period deleted successfully.')
    return redirect(reverse('manage_academic_periods'))


@login_required
def learner_login_activity(request):
    if request.GET:
        params = request.GET.copy()
        if params.get('learner'):
            params['search'] = params.pop('learner')
        if params.get('date'):
            params['start_date'] = params.pop('date')
            params['end_date'] = params['start_date']
        params.pop('school_class', None)
        params.pop('page', None)
        return redirect(f'{reverse("activity_log")}?{params.urlencode()}')
    return redirect(reverse('activity_log'))


def _activity_change_map(value):
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    for parser in (json.loads, ast.literal_eval):
        try:
            parsed = parser(value)
        except (SyntaxError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


def _activity_sentence(entry):
    actor = entry.actor_username or 'System'
    action_labels = {
        'create': 'created',
        'update': 'changed',
        'delete': 'deleted',
        'sign_in': 'signed in',
        'sign_out': 'signed out',
        'failed_sign_in': 'had a failed sign-in attempt',
        'export': 'exported',
        'import': 'imported',
    }
    action = action_labels.get(entry.action, entry.action.replace('_', ' '))
    record = (entry.object_type or 'record').replace('.', ' ')
    if entry.object_id:
        record = f'{record} #{entry.object_id}'
    timestamp = timezone.localtime(entry.created_at).strftime(
        '%d %B %Y at %H:%M')
    if entry.action == 'security_reset':
        return (
            f'{(entry.description or actor).rstrip(". ")} on {timestamp}.'
        )
    changes = _activity_change_map(entry.changes)
    if entry.object_type == 'main_app.studentresult':
        result = StudentResult.objects.select_related(
            'student__admin', 'student__school_class', 'subject'
        ).filter(pk=entry.object_id).first()
        if result:
            changed_scores = []
            score_change = _activity_change_map(
                changes.get('assessment_scores'))
            if isinstance(score_change, dict):
                old_scores = _activity_change_map(score_change.get('old'))
                new_scores = _activity_change_map(score_change.get('new'))
                old_scores = {
                    str(key): value for key, value in old_scores.items()
                }
                new_scores = {
                    str(key): value for key, value in new_scores.items()
                }
                keys = set(old_scores) | set(new_scores)
                assessment_map = {
                    str(item.pk): item.name
                    for item in Assessment.objects.filter(
                        plan__subject=result.subject)
                }
                for key in sorted(keys):
                    if old_scores.get(key) == new_scores.get(key):
                        continue
                    assessment_name = assessment_map.get(
                        str(key), f'Assessment {key}')
                    changed_scores.append(
                        f'{assessment_name} from '
                        f'{old_scores.get(key, "blank")} to '
                        f'{new_scores.get(key, "blank")}')
            learner_name = (
                result.student.admin.get_full_name().strip()
                or result.student.admin.username)
            class_name = result.student.school_class or 'unassigned class'
            subject_name = result.subject.name
            if changed_scores:
                verb = 'entered' if entry.action == 'create' else 'changed'
                return (
                    f'{actor} {verb} {"; ".join(changed_scores)} for '
                    f'{learner_name} in {class_name} {subject_name} '
                    f'on {timestamp}.'
                )
    if entry.object_type == 'main_app.assessment' and entry.action == 'update':
        assessment = Assessment.objects.select_related(
            'plan__school_class', 'plan__subject'
        ).filter(pk=entry.object_id).first()
        if assessment:
            for field in ('weight', 'maximum_mark', 'name'):
                change = _activity_change_map(changes.get(field))
                if (
                    isinstance(change, dict)
                    and change.get('old') != change.get('new')
                ):
                    detail = {
                        'weight': 'weight',
                        'maximum_mark': 'maximum mark',
                        'name': 'name',
                    }[field]
                    return (
                        f'{actor} changed the {detail} for {assessment.name} '
                        f'in {assessment.plan.school_class} '
                        f'{assessment.plan.subject.name} from '
                        f'{change.get("old")} to {change.get("new")} '
                        f'on {timestamp}.'
                    )
    description = entry.description or record
    details = []
    for field, raw_values in changes.items():
        normalized_field = field.lower()
        if any(
            sensitive_part in normalized_field
            for sensitive_part in (
                'password', 'pin', 'hash', 'secret', 'token')
        ):
            continue
        values = _activity_change_map(raw_values)
        if not isinstance(values, dict) or not {'old', 'new'} <= values.keys():
            continue
        old_value = values.get('old')
        new_value = values.get('new')
        if old_value == '[redacted]' or new_value == '[redacted]':
            continue
        details.append(
            f'{field.replace("_", " ")} from {old_value or "blank"} '
            f'to {new_value or "blank"}')
    if details and entry.action == 'update':
        description = '; '.join(details)
    if entry.action == 'update' and details:
        return f'{actor} {action} {description} on {record} at {timestamp}.'
    return f'{actor} {action} {description} ({record}) at {timestamp}.'


def _activity_role_label(user):
    return {
        '1': 'Administrator',
        '2': 'Learner',
        '3': 'Teacher',
    }.get(str(user.user_type), 'Unknown') if user else 'Unknown'


@login_required
def activity_log(request, user_id=None):
    target_user = None
    activities = ActivityLog.objects.select_related('actor', 'target_user')
    if user_id is not None:
        target_user = get_object_or_404(CustomUser, pk=user_id)
        activities = activities.filter(
            Q(target_user=target_user)
            | Q(target_user_id_snapshot=str(target_user.pk))
            | Q(actor=target_user))

    filter_form = ActivityLogFilterForm(request.GET)
    filter_valid = filter_form.is_valid()
    if filter_valid:
        search = filter_form.cleaned_data['search']
        role = filter_form.cleaned_data['role']
        action = filter_form.cleaned_data['action']
        start_date = filter_form.cleaned_data['start_date']
        end_date = filter_form.cleaned_data['end_date']
        if search:
            activities = activities.filter(
                Q(actor_username__icontains=search)
                | Q(description__icontains=search)
                | Q(object_type__icontains=search)
                | Q(target_username__icontains=search)
                | Q(target_user__username__icontains=search)
            )
        if role:
            activities = activities.filter(
                Q(actor__user_type=role)
                | Q(target_user__user_type=role)
            )
        if action:
            activities = activities.filter(action=action)
        if start_date:
            activities = activities.filter(created_at__date__gte=start_date)
        if end_date:
            activities = activities.filter(created_at__date__lte=end_date)
    activities = list(activities.order_by('-created_at', '-pk'))
    legacy_logins = LoginActivity.objects.select_related(
        'student__admin', 'student__school_class')
    if target_user:
        legacy_logins = legacy_logins.filter(student__admin=target_user)
    if filter_valid:
        search = filter_form.cleaned_data['search']
        role = filter_form.cleaned_data['role']
        action = filter_form.cleaned_data['action']
        start_date = filter_form.cleaned_data['start_date']
        end_date = filter_form.cleaned_data['end_date']
        if search:
            legacy_logins = legacy_logins.filter(
                Q(student__admin__first_name__icontains=search)
                | Q(student__admin__last_name__icontains=search)
                | Q(student__admin__username__icontains=search))
        if action and action != 'sign_in':
            legacy_logins = legacy_logins.none()
        if role and role != '2':
            legacy_logins = legacy_logins.none()
        if start_date:
            legacy_logins = legacy_logins.filter(
                logged_in_at__date__gte=start_date)
        if end_date:
            legacy_logins = legacy_logins.filter(
                logged_in_at__date__lte=end_date)
    for login in legacy_logins:
        user = login.student.admin
        if ActivityLog.objects.filter(
            action='sign_in',
            target_user=user,
            created_at__gte=login.logged_in_at - timedelta(seconds=5),
            created_at__lte=login.logged_in_at + timedelta(seconds=5),
        ).exists():
            continue
        activities.append(SimpleNamespace(
            pk=f'legacy-{login.pk}',
            actor=user,
            actor_id=user.pk,
            actor_username=user.get_username(),
            target_user=user,
            target_username=user.get_username(),
            action='sign_in',
            action_label='Sign in',
            object_type='Learner login',
            object_id=str(login.student_id),
            description=(
                f'{user.get_full_name() or user.get_username()} signed in'),
            display_description=(
                f'{user.get_full_name() or user.get_username()} signed in '
                f'on {timezone.localtime(login.logged_in_at):%d %B %Y at %H:%M}.'),
            changes={},
            created_at=login.logged_in_at,
        ))
    activities.sort(key=lambda item: (item.created_at, str(item.pk)), reverse=True)
    for entry in activities:
        entry.action_label = (
            entry.action_label if hasattr(entry, 'action_label')
            else entry.get_action_display())
        entry.display_description = (
            entry.display_description if hasattr(entry, 'display_description')
            else _activity_sentence(entry))
        entry.role_label = _activity_role_label(
            getattr(entry, 'actor', None)
            or getattr(entry, 'target_user', None))

    export_format = request.GET.get('export')
    if export_format in ('pdf', 'csv') and not filter_valid:
        return HttpResponse(
            'Invalid activity-log filters.', status=400,
            content_type='text/plain',
        )

    if export_format in ('pdf', 'csv'):
        headers = [
            'Timestamp', 'Actor', 'Role', 'Action', 'Record',
            'Target user', 'Description',
        ]
        rows = [[
                item.created_at.isoformat(),
                item.actor_username,
                item.role_label,
                item.action_label,
                f'{item.object_type} {item.object_id}'.strip(),
                item.target_username,
                item.display_description,
            ] for item in activities]
        if not rows:
            messages.warning(request, 'No activity found for these filters.')
        else:
            title = (
                f'Activity Timeline {target_user.get_username()}'
                if target_user else 'Activity Log')
            return _export_data(
                request,
                request.user,
                title,
                headers,
                rows,
                export_format,
                report_template=(
                    'activity_timeline' if target_user else 'activity_log'),
            )

    paginator = Paginator(activities, 50)
    page_obj = paginator.get_page(request.GET.get('page'))
    query_params = request.GET.copy()
    query_params.pop('page', None)
    query_params.pop('export', None)
    return render(request, 'hod_template/activity_log.html', {
        'page_title': (
            f'Activity Timeline: {target_user.get_username()}'
            if target_user else 'Activity Log'),
        'filter_form': filter_form,
        'page_obj': page_obj,
        'query_params': query_params.urlencode(),
        'target_user': target_user,
    })


def manage_teachers(request):
    teachers = Teacher.objects.select_related('teacher').order_by(
        'teacher__last_name', 'teacher__first_name')
    if request.user.user_type == '3':
        teachers = teachers.filter(teacher=request.user)
    search_query = request.GET.get('search', '').strip()
    if search_query:
        teachers = teachers.filter(
            Q(teacher__first_name__icontains=search_query)
            | Q(teacher__last_name__icontains=search_query)
            | Q(teacher__username__icontains=search_query)
        )
    export_format = request.GET.get('export')
    matching_count = teachers.count()
    if export_format in ('pdf', 'csv'):
        rows = [[
            profile.teacher.get_full_name()
            or profile.teacher.username,
            profile.teacher.username,
            profile.teacher.get_gender_display(),
        ] for profile in teachers]
        if not rows:
            messages.warning(request, 'No teachers are available to export.')
        else:
            return _export_data(
                request,
                request.user,
                'Teacher List',
                ['Teacher name', 'Account ID', 'Gender'],
                rows,
                export_format,
                report_template='teacher_list',
            )
    return render(request, 'hod_template/manage_teachers.html', {
        'teachers': teachers,
        'matching_count': matching_count,
        'search_query': search_query,
        'page_title': 'Manage Teachers',
    })


@login_required
def manage_admins(request):
    if request.user.user_type != '1':
        return redirect(reverse('admin_home'))

    form = TeacherAccountForm(request.POST or None, request.FILES or None)
    if request.method == 'POST' and form.is_valid():
        data = form.cleaned_data
        with transaction.atomic():
            user = CustomUser.objects.create_user(
                email='',
                password=None,
                user_type='1',
                first_name=data['first_name'],
                last_name=data['last_name'],
                gender=data['gender'],
                address='',
                profile_pic=data.get('profile_pic') or '',
            )
            Admin.objects.get_or_create(admin=user)
            pin = issue_pin(user)
        return render_pin_confirmation(
            request,
            'admin',
            pin,
            return_url=reverse('manage_admins'),
            return_label='Return to administrators',
            account_id=user.username,
        )

    admins = Admin.objects.select_related('admin').order_by(
        'admin__last_name', 'admin__first_name')
    return render(request, 'hod_template/manage_admins.html', {
        'form': form,
        'admins': admins,
        'page_title': 'Manage Administrators',
    })


@login_required
@require_GET
def manage_assignments(request):
    filter_form = TeacherGradeFilterForm(request.GET)
    selected_teacher = None
    selected_class = None
    teacher_profile_for_user = None
    if request.user.user_type == '3':
        teacher_profile_for_user = Teacher.objects.filter(
            teacher=request.user).first()
        if teacher_profile_for_user:
            filter_form.fields['teacher'].queryset = Teacher.objects.filter(
                pk=teacher_profile_for_user.pk)
            filter_form.fields['school_class'].queryset = (
                ordered_school_classes().filter(
                    teacher_setups__teacher=teacher_profile_for_user
                ).distinct()
            )
        else:
            filter_form.fields['teacher'].queryset = Teacher.objects.none()
            filter_form.fields['school_class'].queryset = SchoolClass.objects.none()
    if filter_form.is_valid():
        selected_teacher = filter_form.cleaned_data['teacher']
        selected_class = filter_form.cleaned_data['school_class']
    if request.user.user_type == '3':
        selected_teacher = teacher_profile_for_user

    setups = TeacherClassSetup.objects.select_related(
        'teacher__teacher', 'school_class').prefetch_related(
            Prefetch(
                'school_class__subject_set',
                queryset=Subject.objects.order_by('name'),
                to_attr='setup_subjects',
            ))
    if request.user.user_type == '3':
        if teacher_profile_for_user:
            setups = setups.filter(teacher=teacher_profile_for_user)
        else:
            setups = setups.none()
    elif selected_teacher:
        setups = setups.filter(teacher=selected_teacher)
    if selected_class:
        setups = setups.filter(school_class=selected_class)

    rows = []
    setup_teacher_ids = set()
    for setup in setups:
        setup_teacher_ids.add(setup.teacher_id)
        requests_for_setup = LearnerTeacherRequest.objects.filter(
            teacher=setup.teacher,
            school_class=setup.school_class,
        )
        accepted_count = requests_for_setup.filter(
            status=LearnerTeacherRequest.ACCEPTED).count()
        pending_count = requests_for_setup.filter(
            status=LearnerTeacherRequest.PENDING).count()
        declined_count = requests_for_setup.filter(
            status=LearnerTeacherRequest.DECLINED).count()
        rows.append({
            'teacher': setup.teacher,
            'school_class': setup.school_class,
            'subjects': [
                subject for subject in setup.school_class.setup_subjects
                if subject.teacher_id == setup.teacher_id
            ],
            'accepted_count': accepted_count,
            'pending_count': (
                pending_count if request.user.user_type == '1' else ''),
            'declined_count': (
                declined_count if request.user.user_type == '1' else ''),
            'learners_url': (
                f"{reverse('assignment_learners')}?teacher={setup.teacher_id}"
                f"&school_class={setup.school_class_id}"
            ),
        })

    teachers_without_setup = Teacher.objects.select_related(
        'teacher').order_by('teacher__last_name', 'teacher__first_name')
    if request.user.user_type == '3':
        teachers_without_setup = teachers_without_setup.none()
    elif selected_teacher:
        teachers_without_setup = teachers_without_setup.filter(
            pk=selected_teacher.pk)
    if selected_class:
        teachers_without_setup = teachers_without_setup.none()
    for teacher in teachers_without_setup:
        if teacher.pk in setup_teacher_ids:
            continue
        rows.append({
            'teacher': teacher,
            'school_class': None,
            'subjects': [],
            'accepted_count': 0,
            'pending_count': 0,
            'declined_count': 0,
            'learners_url': '',
        })

    configured_class_ids = TeacherClassSetup.objects.values_list(
        'school_class_id', flat=True)
    unmatched_learners = Student.objects.select_related(
        'admin', 'school_class').filter(
            Q(school_class__isnull=True)
            | ~Q(school_class_id__in=configured_class_ids)
        ).order_by('admin__last_name', 'admin__first_name')
    if request.user.user_type == '3':
        unmatched_learners = unmatched_learners.none()
    if selected_class:
        unmatched_learners = unmatched_learners.filter(
            school_class=selected_class)
    unmatched_learners = sort_learners(
        unmatched_learners,
        lambda learner: learner.admin,
    )

    unconfigured_subjects = Subject.objects.filter(
        Q(teacher__isnull=True) | Q(school_class__isnull=True)
    ).select_related('teacher__teacher', 'school_class').order_by('name')
    if selected_teacher:
        unconfigured_subjects = unconfigured_subjects.filter(
            Q(teacher=selected_teacher) | Q(teacher__isnull=True))
    if request.user.user_type == '3':
        unconfigured_subjects = unconfigured_subjects.filter(
            teacher=selected_teacher)
        if not selected_teacher:
            unconfigured_subjects = unconfigured_subjects.none()
    if selected_class:
        unconfigured_subjects = unconfigured_subjects.filter(
            school_class=selected_class)

    export_format = request.GET.get('export')
    if export_format in ('pdf', 'csv'):
        export_rows = [[
            row['teacher'].teacher.get_full_name()
            or row['teacher'].teacher.username,
            row['school_class'] or 'Not set up',
            ', '.join(subject.name for subject in row['subjects']) or 'None',
            row['accepted_count'],
            row['pending_count'],
            row['declined_count'],
        ] for row in rows]
        export_rows.extend([
            [
                'Unmatched learner',
                learner.school_class or 'Not assigned',
                learner.admin.get_full_name() or learner.admin.username,
                '', '', '',
            ] for learner in unmatched_learners
        ])
        export_rows.extend([
            [
                'Not set up subject',
                subject.school_class or 'Not set up',
                subject.name,
                '', '', '',
            ] for subject in unconfigured_subjects
        ])
        if export_rows:
            return _export_data(
                request,
                request.user,
                'Assignments',
                [
                    'Teacher / record', 'Class', 'Subjects / learner',
                    'Accepted learners', 'Pending', 'Declined',
                ],
                export_rows,
                export_format,
                report_template='assignments',
            )
        messages.warning(request, 'There are no assignments to export.')
    elif export_format:
        return HttpResponse('Unsupported export format.', status=400)

    export_query = request.GET.copy()
    export_query.pop('export', None)
    report_count = (
        len(rows) + len(unmatched_learners)
        + unconfigured_subjects.count()
    )
    return render(request, 'hod_template/manage_assignments.html', {
        'filter_form': filter_form,
        'assignment_rows': rows,
        'unmatched_learners': unmatched_learners,
        'unconfigured_subjects': unconfigured_subjects,
        'report_count': report_count,
        'export_query': export_query.urlencode(),
        'page_title': 'Assignments',
    })


@login_required
@require_GET
def assignment_learners(request):
    teacher_id = request.GET.get('teacher', '')
    school_class_id = request.GET.get('school_class', '')
    if request.user.user_type == '3':
        teacher = get_object_or_404(
            Teacher, pk=teacher_id, teacher=request.user)
    else:
        teacher = get_object_or_404(Teacher, pk=teacher_id)
    school_class = get_object_or_404(SchoolClass, pk=school_class_id)
    if not TeacherClassSetup.objects.filter(
        teacher=teacher, school_class=school_class).exists():
        return HttpResponseForbidden()
    accepted_requests = LearnerTeacherRequest.objects.filter(
        teacher=teacher,
        school_class=school_class,
        status=LearnerTeacherRequest.ACCEPTED,
    ).select_related('student__admin', 'student__school_class')
    accepted_requests = sort_learners(
        accepted_requests,
        lambda learner_request: learner_request.student.admin,
    )
    export_format = request.GET.get('export')
    if export_format in ('pdf', 'csv'):
        return _export_data(
            request,
            request.user,
            f'Accepted Learners {school_class}',
            ['Learner name', 'Account ID', 'Class', 'Accepted date'],
            [[
                item.student.admin.get_full_name()
                or item.student.admin.username,
                item.student.admin.username,
                item.school_class or 'Not assigned',
                item.responded_at or '',
            ] for item in accepted_requests],
            export_format,
            report_template='accepted_learners',
        ) or HttpResponse('No accepted learners to export.', status=404)
    if export_format:
        return HttpResponse('Unsupported export format.', status=400)
    return render(request, 'hod_template/assignment_learners.html', {
        'teacher': teacher,
        'school_class': school_class,
        'requests': accepted_requests,
        'page_title': 'Accepted Learners',
    })


@login_required
def reset_user_credentials(request, user_id):
    if request.user.user_type == '1':
        account = get_object_or_404(
            CustomUser.objects.filter(
                Q(user_type__in=('2', '3'))
                | (Q(user_type='1') & ~Q(pk=request.user.pk))
            ),
            pk=user_id,
        )
        allowed = True
    elif request.user.user_type == '3':
        account = CustomUser.objects.filter(
            user_type='2',
            student__teacher_requests__teacher__teacher=request.user,
            student__teacher_requests__status=LearnerTeacherRequest.ACCEPTED,
            pk=user_id,
        ).distinct().first()
        if account:
            allowed = True
        else:
            account = get_object_or_404(
                CustomUser.objects.filter(user_type='3'), pk=user_id)
            allowed = False
    else:
        account = get_object_or_404(CustomUser, pk=user_id)
        allowed = False
    if not allowed:
        return HttpResponseForbidden()

    form = StaffCredentialResetForm(
        request.POST or None,
        user=account,
    )
    if request.method == 'POST' and form.is_valid():
        if form.cleaned_data['action'] == 'password':
            secret = form.cleaned_data['new_password']
            with transaction.atomic():
                account.set_password(secret)
                account.failed_login_attempts = 0
                account.locked_until = None
                account.save(update_fields=[
                    'password',
                    'failed_login_attempts',
                    'locked_until',
                ])
            secret_label = 'New password'
        else:
            with transaction.atomic():
                secret = issue_pin(account)
            secret_label = 'New PIN'
        response = render(request, 'main_app/credential_reset_confirmation.html', {
            'account': account,
            'secret': secret,
            'secret_label': secret_label,
            'return_url': reverse(
                {
                    '1': 'manage_admins',
                    '2': 'manage_student',
                    '3': 'manage_teachers',
                }[account.user_type]),
        })
        response['Cache-Control'] = 'no-store, no-cache, must-revalidate, private'
        response['Pragma'] = 'no-cache'
        return response

    return render(request, 'hod_template/reset_credentials.html', {
        'form': form,
        'account': account,
        'page_title': 'Reset account credentials',
    })


def add_teacher(request):
    form = TeacherAccountForm(request.POST or None, request.FILES or None)
    if request.method == 'POST' and form.is_valid():
        data = form.cleaned_data
        with transaction.atomic():
            user = CustomUser.objects.create_user(
                email='',
                password=None,
                user_type='3',
                first_name=data['first_name'],
                last_name=data['last_name'],
                gender=data['gender'],
                address='',
                profile_pic=data.get('profile_pic') or '',
            )
            Teacher.objects.get_or_create(teacher=user)
            pin = issue_pin(user)
        return render_pin_confirmation(
            request,
            'teacher',
            pin,
            return_url=reverse('manage_teachers'),
            return_label='Return to teachers',
            account_id=user.username,
        )
    return render(request, 'hod_template/edit_teacher.html', {
        'form': form,
        'page_title': 'Add Teacher',
    })


def edit_teacher(request, teacher_id):
    teacher = get_object_or_404(Teacher.objects.select_related('teacher'), pk=teacher_id)
    user = teacher.teacher
    form = TeacherAccountForm(
        request.POST or None,
        request.FILES or None,
        user=user,
    )
    if request.method == 'POST' and form.is_valid():
        data = form.cleaned_data
        user.first_name = data['first_name']
        user.last_name = data['last_name']
        user.gender = data['gender']
        if data.get('profile_pic'):
            user.profile_pic = data['profile_pic']
        with transaction.atomic():
            user.save()
        messages.success(request, 'Teacher account updated successfully.')
        return redirect(reverse('manage_teachers'))
    return render(request, 'hod_template/edit_teacher.html', {
        'form': form,
        'teacher': teacher,
        'page_title': 'Edit Teacher',
    })


def delete_teacher(request, teacher_id):
    teacher = get_object_or_404(Teacher.objects.select_related('teacher'), pk=teacher_id)
    if request.method == 'POST':
        teacher.teacher.delete()
        messages.success(request, 'Teacher account deleted successfully.')
    return redirect(reverse('manage_teachers'))


@login_required
def generate_report(request):
    if request.user.user_type not in ('1', '3'):
        return redirect(reverse('admin_home'))

    role = 'teacher' if request.user.user_type == '3' else 'admin'
    publication_action = request.POST.get('publication_action')
    if request.method == 'POST' and publication_action:
        publication_form = ReportCardPublicationForm(
            request.POST, user=request.user)
        if not publication_form.is_valid():
            return _render_generate_report_page(
                request,
                role,
                publication_form=publication_form,
            )

        school_class = publication_form.cleaned_data['school_class']
        academic_period = publication_form.cleaned_data['academic_period']
        school_wide = publication_form.cleaned_data.get(
            'school_wide', False)
        if school_wide:
            school_class = None
        rows = _report_publication_rows(
            request.user, school_class, academic_period, school_wide)

        if publication_action in ('publish_one', 'unpublish_one'):
            target_id = request.POST.get('student_id', '')
            target = next(
                (row for row in rows
                 if str(row['student'].pk) == target_id),
                None,
            )
            if target is None:
                return HttpResponseForbidden(
                    'The learner is outside this publication scope.')
            if publication_action == 'publish_one':
                if not target['has_results']:
                    messages.warning(
                        request,
                        'There are no reportable results for this learner.',
                    )
                else:
                    with transaction.atomic():
                        _update_report_card_publication(
                            request.user,
                            target['student'],
                            academic_period,
                            target['teacher_ids'],
                            publish=True,
                        )
                    messages.success(
                        request, 'The learner report card was published.')
            else:
                with transaction.atomic():
                    deleted_count = _update_report_card_publication(
                        request.user,
                        target['student'],
                        academic_period,
                        target['teacher_ids'],
                        publish=False,
                    )
                if deleted_count:
                    messages.success(
                        request, 'The learner report card was unpublished.')
                else:
                    messages.info(
                        request, 'This learner report card was not published.')
        elif publication_action == 'publish_all':
            targets = [row for row in rows if row['has_results']]
            with transaction.atomic():
                for target in targets:
                    _update_report_card_publication(
                        request.user,
                        target['student'],
                        academic_period,
                        target['teacher_ids'],
                        publish=True,
                    )
            if targets:
                messages.success(
                    request,
                    f'Published report cards for {len(targets)} learners.',
                )
            else:
                messages.warning(
                    request,
                    'No learners with reportable results matched this scope.',
                )
        elif publication_action == 'unpublish_all':
            targets = [row for row in rows if row['is_published']]
            with transaction.atomic():
                for target in targets:
                    _update_report_card_publication(
                        request.user,
                        target['student'],
                        academic_period,
                        target['teacher_ids'],
                        publish=False,
                    )
            if targets:
                messages.success(
                    request,
                    f'Unpublished report cards for {len(targets)} learners.',
                )
            else:
                messages.info(
                    request, 'No published report cards matched this scope.')
        else:
            return HttpResponse('Unsupported publication action.', status=400)

        query = (
            f'?manage_publications=1&academic_period={academic_period.pk}')
        if school_wide:
            query += '&school_wide=on'
        elif school_class:
            query += f'&school_class={school_class.pk}'
        return redirect(reverse('generate_report') + query)

    form = ReportGenerationForm(
        request.POST or None, role=role, user=request.user)
    if request.method != 'POST' or not form.is_valid():
        publication_form = ReportCardPublicationForm(
            request.GET if request.GET.get('manage_publications') else None,
            user=request.user,
        )
        publication_rows = None
        if request.GET.get('manage_publications'):
            if publication_form.is_valid():
                school_wide = publication_form.cleaned_data.get(
                    'school_wide', False)
                school_class = (
                    None if school_wide else
                    publication_form.cleaned_data['school_class'])
                publication_rows = _report_publication_rows(
                    request.user,
                    school_class,
                    publication_form.cleaned_data['academic_period'],
                    school_wide,
                )
        return _render_generate_report_page(
            request,
            role,
            form=form,
            publication_form=publication_form,
            publication_rows=publication_rows,
        )

    report_type = form.cleaned_data['report_type']
    school_class = form.cleaned_data.get('school_class')
    academic_period = form.cleaned_data.get('academic_period')
    subject = form.cleaned_data.get('subject')

    title, headers, rows = _report_data(
        report_type, school_class, academic_period, subject, user=request.user)
    export_format = request.POST.get('export_format', 'pdf')
    if export_format not in ('pdf', 'csv'):
        return HttpResponse('Unsupported export format.', status=400)
    if not rows:
        messages.warning(
            request, 'There is no data for the selected report options.')
        return _render_generate_report_page(
            request,
            role,
            form=form,
        )

    preamble = []
    postamble = []
    if report_type == 'class_list':
        class_teacher = (
            request.user.get_full_name().strip() or request.user.username
            if request.user.user_type == '3'
            else 'Not specified')
        preamble = [
            f'<b>Class:</b> {escape(str(school_class))}',
            f'<b>Academic period:</b> {escape(str(academic_period))}',
            f'<b>Class teacher:</b> {escape(class_teacher)}',
        ]
        postamble = [
            'Class teacher signature: ______________________________',
            f'Date: {timezone.localdate():%d %B %Y}',
        ]
    return _export_data(
        request,
        request.user,
        title,
        headers,
        rows,
        export_format,
        academic_period=academic_period,
        preamble=preamble,
        postamble=postamble,
        report_template=report_type,
    )


def _render_generate_report_page(
        request, role, form=None, publication_form=None,
        publication_rows=None):
    if form is None:
        form = ReportGenerationForm(role=role, user=request.user)
    if publication_form is None:
        publication_form = ReportCardPublicationForm(user=request.user)
    publication_school_wide = (
        bool(publication_form.data.get('school_wide'))
        if publication_form.is_bound else False
    )
    return render(request, 'hod_template/generate_report.html', {
        'form': form,
        'publication_form': publication_form,
        'publication_rows': publication_rows,
        'publication_results_loaded': publication_rows is not None,
        'publication_school_wide': publication_school_wide,
        'publication_publish_count': sum(
            row['has_results'] for row in publication_rows or []),
        'publication_unpublish_count': sum(
            row['is_published'] for row in publication_rows or []),
        'page_title': 'Generate Report',
    })


def _report_publication_rows(
        user, school_class, academic_period, school_wide=False):
    students = visible_students(user)
    if school_wide:
        students = students.filter(school_class__isnull=False)
    elif school_class:
        students = students.filter(school_class=school_class)

    results = StudentResult.objects.filter(student__in=students).filter(
        Q(academic_period=academic_period)
        | Q(
            academic_period__isnull=True,
            student__academic_period=academic_period,
        )
    ).exclude(subject__teacher__isnull=True)
    if user.user_type == '3':
        results = results.filter(
            subject__teacher__teacher=user,
            subject__school_class_id=F('student__school_class_id'),
        )

    teacher_ids_by_student = {}
    for student_id, teacher_id in results.values_list(
            'student_id', 'subject__teacher_id').distinct():
        teacher_ids_by_student.setdefault(student_id, set()).add(teacher_id)

    publications = ReportCardPublication.objects.filter(
        student__in=students,
        academic_period=academic_period,
    )
    if user.user_type == '3':
        publications = publications.filter(teacher__teacher=user)
    published_student_ids = set(
        publications.values_list('student_id', flat=True).distinct())

    learners = sort_learners(
        students.select_related('admin', 'school_class').distinct(),
        lambda student: student.admin,
    )
    return [{
        'student': student,
        'teacher_ids': teacher_ids_by_student.get(student.pk, set()),
        'has_results': bool(teacher_ids_by_student.get(student.pk)),
        'is_published': student.pk in published_student_ids,
    } for student in learners]


def _update_report_card_publication(
        user, student, academic_period, teacher_ids, publish):
    if not publish:
        publications = ReportCardPublication.objects.filter(
            student=student,
            academic_period=academic_period,
        )
        if user.user_type == '3':
            publications = publications.filter(teacher__teacher=user)
        deleted_count, _deleted = publications.delete()
        return deleted_count

    if not teacher_ids:
        return 0
    for teacher_id in teacher_ids:
        ReportCardPublication.objects.update_or_create(
            teacher_id=teacher_id,
            student=student,
            academic_period=academic_period,
            defaults={'published_at': timezone.now()},
        )
    return len(teacher_ids)


@login_required
@require_GET
def report_preview(request):
    if request.user.user_type not in ('1', '3'):
        return JsonResponse(
            {'message': 'Report preview is unavailable.'}, status=403)

    report_type = request.GET.get('report_type', '')
    if report_type == 'learner_report_card':
        form = ReportCardForm(request.GET, user=request.user)
        if not form.is_valid():
            return JsonResponse({
                'message': 'Select a learner and academic period to preview.',
            })
        student = form.cleaned_data['student']
        academic_period = form.cleaned_data['academic_period']
        subject_count = _report_card_results(
            request.user, student, academic_period
        ).values('subject_id').distinct().count()
        subject_label = 'subject' if subject_count == 1 else 'subjects'
        return JsonResponse({
            'message': (
                f'{subject_count} {subject_label}, {academic_period}'
                if subject_count else
                f'No matching results, {academic_period}'
            ),
            'count': subject_count,
        })

    role = 'teacher' if request.user.user_type == '3' else 'admin'
    form = ReportGenerationForm(
        request.GET, role=role, user=request.user)
    if not form.is_valid():
        return JsonResponse({
            'message': 'Choose report options to preview matching data.',
        })

    report_type = form.cleaned_data['report_type']
    school_class = form.cleaned_data.get('school_class')
    academic_period = form.cleaned_data.get('academic_period')
    subject = form.cleaned_data.get('subject')
    if report_type == 'school_summary':
        learner_count = visible_students(request.user).count()
        subject_count = visible_subjects(request.user).count()
        current_period = AcademicPeriod.objects.filter(
            is_current=True).first()
        period_label = f', {current_period}' if current_period else ''
        learner_label = 'learner' if learner_count == 1 else 'learners'
        subject_label = 'subject' if subject_count == 1 else 'subjects'
        return JsonResponse({
            'message': (
                f'{learner_count} {learner_label}, '
                f'{subject_count} {subject_label}'
                f'{period_label}'
            ),
            'learner_count': learner_count,
            'subject_count': subject_count,
        })

    title, _headers, rows = _report_data(
        report_type, school_class, academic_period, subject,
        user=request.user)
    if report_type == 'class_list':
        learner_count = max(0, len(rows) - 2)
        learner_label = 'learner' if learner_count == 1 else 'learners'
        return JsonResponse({
            'message': (
                f'{learner_count} {learner_label}, {school_class}, '
                f'{academic_period}'
            ),
            'learner_count': learner_count,
        })

    learner_count = _report_result_learners(
        request.user, school_class, subject).count()
    learner_label = 'learner' if learner_count == 1 else 'learners'
    return JsonResponse({
        'message': (
            f'{learner_count} {learner_label}, 1 subject, {academic_period}'
        ),
        'learner_count': learner_count,
        'subject_count': 1,
        'has_results': bool(rows),
    })


@login_required
def generate_report_card(request):
    if request.user.user_type not in ('1', '3'):
        return redirect(reverse('admin_home'))

    form = ReportCardForm(request.POST or None, user=request.user)
    if request.method == 'POST' and form.is_valid():
        student = form.cleaned_data['student']
        academic_period = form.cleaned_data['academic_period']
        if request.POST.get('action') == 'publish':
            if request.user.user_type != '3':
                return HttpResponseForbidden()
            has_results = _report_card_results(
                request.user, student, academic_period).exists()
            if not has_results:
                messages.warning(
                    request,
                    'There are no results for this learner and academic period.',
                )
            else:
                ReportCardPublication.objects.update_or_create(
                    teacher=request.user.teacher,
                    student=student,
                    academic_period=academic_period,
                    defaults={'published_at': timezone.now()},
                )
                messages.success(
                    request,
                    f'The report card for {student.admin.get_full_name()} '
                    'has been published to the learner.',
                )
                return redirect(reverse('generate_report_card'))

        export_format = request.POST.get('export_format', 'pdf')
        if request.POST.get('action') == 'publish':
            return render(request, 'hod_template/generate_report_card.html', {
                'form': form,
                'page_title': 'Generate Learner Report Card',
            })
        if export_format not in ('pdf', 'csv'):
            return HttpResponse('Unsupported export format.', status=400)
        response = _report_card_pdf(
            request,
            request.user,
            student,
            academic_period,
            export_format=export_format,
        )
        if response is not None:
            return response
        messages.warning(
            request, 'There are no results for this learner and academic period.')

    return render(request, 'hod_template/generate_report_card.html', {
        'form': form,
        'page_title': 'Generate Learner Report Card',
    })


def _report_card_pdf(
        request, user, student, academic_period, export_format='pdf',
        actor_user=None):
    results = list(_report_card_results(
        user, student, academic_period).select_related(
            'subject', 'academic_period', 'student__academic_period'
        ).order_by('subject__name'))
    if not results:
        return None

    prepared_results = [prepare_result(result) for result in results]
    assessment_columns = []
    seen_columns = set()
    for result in prepared_results:
        for assessment, _ in result.assessment_values:
            column = (
                assessment.name,
                str(assessment.weight),
                str(assessment.maximum_mark),
            )
            if column not in seen_columns:
                seen_columns.add(column)
                assessment_columns.append(column)
    rows = []
    totals = []
    for result in prepared_results:
        if result.total_score is not None:
            totals.append(result.total_score)
        values = {
            (assessment.name, str(assessment.weight),
             str(assessment.maximum_mark)): (
                '' if score is None else f'{score:g}')
            for assessment, score in result.assessment_values
        }
        teacher_user = (
            result.subject.teacher.teacher
            if result.subject.teacher else None)
        initials = ''.join(
            name[0] for name in (
                teacher_user.get_full_name() or teacher_user.username
            ).split() if name
        ) if teacher_user else ''
        row = [
            result.subject.name,
            *[values.get(column, '') for column in assessment_columns],
        ]
        row.extend([
            f'{result.total_score:.2f}' if result.total_score is not None else 'Incomplete',
            result.grade['symbol'] if result.grade else '-',
            result.grade['description'] if result.grade else '',
            initials,
        ])
        rows.append(row)

    next_period = None
    if academic_period.year and academic_period.term:
        next_period = AcademicPeriod.objects.filter(
            Q(year__gt=academic_period.year)
            | Q(
                year=academic_period.year,
                term__gt=academic_period.term,
            )
        ).order_by('year', 'term').first()
    assessment_headers = [
        f'{name} ({Decimal(weight):g}%)'
        for name, weight, _maximum in assessment_columns
    ]
    report_card_title = (
        f'END OF TERM {academic_period.term} {academic_period.year} '
        'LEARNER REPORT CARD'
        if academic_period.year and academic_period.term
        else f'END OF {academic_period} LEARNER REPORT CARD'
    )
    return _export_data(
        request,
        user,
        report_card_title,
        [
            'Subject',
            *assessment_headers,
            'Final %',
            'Grade',
            'Comment',
            "Teacher's initials",
        ],
        rows,
        export_format,
        academic_period=academic_period,
        postamble=[
            (
                f'<b>Overall total:</b> {sum(totals):.2f} &nbsp; '
                f'<b>Overall average:</b> '
                f'{sum(totals) / len(totals):.2f}'
                if totals else
                '<b>Overall total and average:</b> Not calculated; '
                'no complete final marks are available.'
            ),
        ],
        report_card={
            'student': student,
            'academic_period': academic_period,
            'overall_total': sum(totals) if totals else None,
            'overall_average': (
                sum(totals) / len(totals) if totals else None),
            'next_period': next_period,
        },
        report_template='learner_report_card',
        audit_user=actor_user,
    )


def _report_card_results(user, student, academic_period):
    results_queryset = StudentResult.objects.filter(
        student=student,
    ).filter(
        Q(academic_period=academic_period)
        | Q(
            academic_period__isnull=True,
            student__academic_period=academic_period,
        )
    )
    if user.user_type == '3':
        if not student.teacher_requests.filter(
            teacher__teacher=user,
            status=LearnerTeacherRequest.ACCEPTED,
        ).exists():
            return StudentResult.objects.none()
        results_queryset = results_queryset.filter(
            subject__teacher__teacher=user,
            subject__school_class=student.school_class,
        )
    elif user.user_type == '2':
        results_queryset = results_queryset.filter(
            subject__teacher__report_card_publications__student=student,
            subject__teacher__report_card_publications__academic_period=(
                academic_period),
        ).distinct()
    return results_queryset


def _report_data(report_type, school_class, academic_period, subject, user=None):
    students = visible_students(user) if user else Student.objects.all()
    subjects = visible_subjects(user) if user else Subject.objects.all()
    results_queryset = visible_results(user) if user else StudentResult.objects.all()

    if report_type == 'school_summary':
        school_classes = ordered_school_classes()
        if user and user.user_type == '3':
            school_classes = school_classes.filter(
                teacher_setups__teacher__teacher=user).distinct()
        school_classes = school_classes.annotate(
            learner_count=Count(
                'student', filter=Q(student__in=students)),
            boys=Count(
                'student',
                filter=Q(student__admin__gender='M', student__in=students)),
            girls=Count(
                'student',
                filter=Q(student__admin__gender='F', student__in=students)),
        )
        class_rows = [[
            'Learners by class',
            str(item),
            f'Boys: {item.boys}; girls: {item.girls}',
            str(item.learner_count),
        ] for item in school_classes]
        unassigned_count = students.filter(
            school_class__isnull=True).count()
        if unassigned_count:
            class_rows.append([
                'Learners by class', 'Not assigned to a class',
                'Gender split unavailable', str(unassigned_count),
            ])
        if (
            not students.exists()
            and not school_classes.exists()
            and not subjects.exists()
        ):
            return 'School Summary', [], []
        teacher_rows = []
        teachers = Teacher.objects.select_related('teacher').prefetch_related(
            'class_setups__school_class', 'subjects')
        if user and user.user_type == '3':
            teachers = teachers.filter(teacher=user)
        for teacher in teachers:
            class_names = ', '.join(
                str(setup.school_class)
                for setup in teacher.class_setups.all())
            subject_names = ', '.join(
                f'{subject.name} ({subject.school_class or "Not set up"})'
                for subject in teacher.subjects.all())
            request_summary = (
                f'Accepted: {teacher.learner_requests.filter(status=(
                    LearnerTeacherRequest.ACCEPTED)).count()}'
            )
            if not user or user.user_type != '3':
                request_summary += (
                    f'; Pending: {teacher.learner_requests.filter(status=(
                        LearnerTeacherRequest.PENDING)).count()}'
                )
            teacher_rows.append([
                'Teacher assignments',
                teacher.teacher.get_full_name() or teacher.teacher.username,
                (
                    f'Classes: {class_names or "None"}; '
                    f'Subjects: {subject_names or "None"}'
                ),
                request_summary,
            ])
        unmatched_count = students.filter(teacher_requests__isnull=True).count()
        current_period = AcademicPeriod.objects.filter(
            is_current=True).first()
        average_by_class = {}
        result_rows = results_queryset
        if current_period:
            result_rows = result_rows.filter(
                Q(academic_period=current_period)
                | Q(
                    academic_period__isnull=True,
                    student__academic_period=current_period,
                ))
        for result in result_rows.select_related('student__school_class'):
            prepared = prepare_result(result)
            if prepared.total_score is None or not result.student.school_class:
                continue
            average_by_class.setdefault(
                result.student.school_class, []).append(prepared.total_score)
        average_rows = [[
            'Average final mark',
            str(school_class),
            'Current academic period',
            f'{sum(marks) / len(marks):.2f}',
        ] for school_class, marks in average_by_class.items()]
        rows = [
            ['School totals', 'Learners', 'All genders', str(students.count())],
            ['School totals', 'Classes', '', str(school_classes.count())],
            ['School totals', 'Subjects', '', str(subjects.count())],
            ['School totals', 'Teachers', '', str(teachers.count())],
            ['Learner requests', 'Unmatched learners', '', str(unmatched_count)],
            *class_rows,
            *teacher_rows,
            *average_rows,
        ]
        return 'School Summary', [
            'Section', 'Class / teacher', 'Details', 'Count / value',
        ], rows

    if report_type == 'class_list':
        learners_queryset = students.filter(
            school_class=school_class
        ).filter(
            Q(academic_period=academic_period)
            | Q(studentresult__academic_period=academic_period)
        ).select_related('admin').distinct()
        if not learners_queryset.exists():
            return f'Class List - {school_class} - {academic_period}', [
                'No.', 'Learner name', 'Gender', 'Account ID'], []
        learners = sort_learners(
            learners_queryset,
            lambda learner: learner.admin,
        )
        rows = [[
            str(index),
            f'{learner.admin.first_name} {learner.admin.last_name}',
            learner.admin.get_gender_display(),
            learner.admin.username,
        ] for index, learner in enumerate(learners, 1)]
        boys = learners_queryset.filter(admin__gender='M').count()
        girls = learners_queryset.filter(admin__gender='F').count()
        rows.extend([
            ['', 'Boys total', '', str(boys)],
            ['', 'Girls total', '', str(girls)],
        ])
        return f'Class List - {school_class} - {academic_period}', [
            'No.', 'Learner name', 'Gender', 'Account ID'], rows

    if report_type == 'learner_results':
        results = results_queryset.filter(
            student__school_class=school_class,
        ).filter(
            Q(academic_period=academic_period)
            | Q(
                academic_period__isnull=True,
                student__academic_period=academic_period,
            )
        ).filter(
            Q(subject__school_class=school_class)
            | Q(subject__school_class__isnull=True)
        ).select_related('student__admin', 'subject').order_by(
            '-created_at', '-pk')
        if subject:
            results = results.filter(subject=subject)
        prepared_results = [prepare_result(result) for result in results]
        plan = AssessmentPlan.objects.filter(
            school_class=school_class,
            subject=subject,
            academic_period=academic_period,
        ).first()
        if plan:
            assessments = list(plan.assessments.order_by('position', 'pk'))
        else:
            assessments = [
                SimpleNamespace(
                    pk=None, name='Test 1', weight=Decimal('50')),
                SimpleNamespace(
                    pk=None, name='Examination', weight=Decimal('50')),
            ]
        assessment_headers = [
            f'{assessment.name} ({assessment.weight:g}%)'
            for assessment in assessments
        ]
        accepted_learners_queryset = _report_result_learners(
            user, school_class, subject)
        if not accepted_learners_queryset.exists():
            return (
                f'Results Mark Sheet - {school_class} - {subject} - '
                f'{academic_period}',
                ['Learner name', *assessment_headers, 'Final %', 'Grade'],
                [],
            )
        accepted_learners = sort_learners(
            accepted_learners_queryset,
            lambda learner: learner.admin,
        )
        results_by_student = {}
        for result in prepared_results:
            results_by_student.setdefault(result.student_id, result)
        rows = []
        for learner in accepted_learners:
            result = results_by_student.get(learner.pk)
            values = {
                assessment.pk: score
                for assessment, score in (
                    result.assessment_values if result else [])
            }
            assessment_values = [
                (
                    f'{values.get(assessment.pk):g}'
                    if values.get(assessment.pk) is not None else ''
                )
                for assessment in assessments
            ]
            rows.append([
                (
                    learner.admin.get_full_name().strip()
                    or learner.admin.username
                ),
                *assessment_values,
                (
                    f'{result.total_score:.2f}'
                    if result and result.total_score is not None
                    else 'Incomplete'
                ),
                result.grade['symbol'] if result and result.grade else '-',
            ])
        final_marks = [
            result.total_score
            for result in results_by_student.values()
            if result.total_score is not None
        ]
        if final_marks:
            rows.extend([
                ['Class average', *([''] * len(assessments)),
                 f'{sum(final_marks) / len(final_marks):.2f}', ''],
                ['Highest mark', *([''] * len(assessments)),
                 f'{max(final_marks):.2f}', ''],
                ['Lowest mark', *([''] * len(assessments)),
                 f'{min(final_marks):.2f}', ''],
                [
                    'Pass rate (50% and above)',
                    *([''] * len(assessments)),
                    f'{100 * sum(mark >= Decimal("50") for mark in final_marks) / len(final_marks):.2f}%',
                    '',
                ],
            ])
        grade_counts = {}
        for result in results_by_student.values():
            if result.grade:
                symbol = result.grade['symbol']
                grade_counts[symbol] = grade_counts.get(symbol, 0) + 1
        rows.extend([
            [
                f'Learners with grade {grade["symbol"]}',
                *([''] * len(assessments)),
                str(grade_counts.get(grade['symbol'], 0)),
                '',
            ]
            for grade in GRADING_SCALE
        ])
        return (
            f'Results Mark Sheet - {school_class} - {subject} - '
            f'{academic_period}',
            ['Learner name', *assessment_headers, 'Final %', 'Grade'],
            rows,
        )

    raise ValueError(f'Unsupported report type: {report_type}')


def _report_result_learners(user, school_class, subject):
    if user and user.user_type == '3':
        return _accepted_learners_for_subject(user, school_class, subject)
    students = visible_students(user) if user else Student.objects.all()
    return students.filter(
        school_class=school_class,
    ).select_related('admin').order_by(
        'admin__last_name', 'admin__first_name', 'admin__username')

def _create_report_pdf(
        user, title, headers, rows, filename='school-report.pdf',
        preamble=None, postamble=None, report_card=None,
        report_template='table'):
    buffer = BytesIO()
    generated_at = timezone.localtime(timezone.now())
    display_name = user.get_full_name().strip() or user.username
    profile = SchoolProfile.objects.order_by('pk').first()
    school_name = (
        profile.school_name if profile and profile.school_name
        else 'C. Heuva Junior Secondary School')
    contacts = []
    if profile:
        contacts = [
            value.strip() for value in (
                profile.postal_address, profile.phone_numbers)
            if value and value.strip()
        ]
    motto = profile.motto.strip() if profile and profile.motto else ''
    logo_path = None
    if profile and profile.logo:
        try:
            path = Path(profile.logo.path)
            if path.is_file():
                logo_path = str(path)
        except (OSError, ValueError, NotImplementedError):
            logger.warning('School profile logo is unavailable for PDF export.')
    crest_path = _report_crest_path()
    logo_path = logo_path or crest_path
    styles = getSampleStyleSheet()
    fonts_path = Path(reportlab.__file__).parent / 'fonts'
    regular_font = 'Helvetica'
    bold_font = 'Helvetica-Bold'
    try:
        registered_fonts = pdfmetrics.getRegisteredFontNames()
        if 'HeuvaSans' not in registered_fonts:
            pdfmetrics.registerFont(TTFont('HeuvaSans', str(fonts_path / 'Vera.ttf')))
            pdfmetrics.registerFont(
                TTFont('HeuvaSans-Bold', str(fonts_path / 'VeraBd.ttf')))
        regular_font = 'HeuvaSans'
        bold_font = 'HeuvaSans-Bold'
    except (OSError, ValueError):
        logger.exception('Could not register bundled report fonts.')
    cell_style = ParagraphStyle(
        'ReportCell', parent=styles['BodyText'], fontName=regular_font,
        fontSize=8, leading=10, spaceAfter=0)
    header_style = ParagraphStyle(
        'ReportHeader', parent=cell_style, fontName=bold_font,
        textColor=colors.white)
    report_title = Paragraph(
        f'<u>{escape(title.upper())}</u>',
        ParagraphStyle(
            'ReportTitle', parent=styles['Heading2'],
            fontName=bold_font, alignment=1, fontSize=16, leading=20))
    body = [report_title, Spacer(1, 5 * mm)]
    pagesize = landscape(A4) if len(headers) >= 8 else A4
    available_width = pagesize[0] - 32 * mm
    if report_card:
        student = report_card['student']
        account = student.admin
        details = Table([
            [
                Paragraph('<b>Name</b>', cell_style),
                Paragraph(escape(
                    account.get_full_name().strip() or account.username),
                    cell_style),
                Paragraph('<b>Account ID</b>', cell_style),
                Paragraph(escape(account.username or ''), cell_style),
            ],
            [
                Paragraph('<b>Class</b>', cell_style),
                Paragraph(escape(str(student.school_class or '')), cell_style),
                Paragraph('<b>Gender</b>', cell_style),
                Paragraph(escape(account.get_gender_display()), cell_style),
            ],
            [
                Paragraph('<b>Academic period</b>', cell_style),
                Paragraph(escape(str(report_card['academic_period'])),
                          cell_style),
                '', '',
            ],
        ], colWidths=[
            available_width * 0.17, available_width * 0.33,
            available_width * 0.17, available_width * 0.33,
        ])
        details.setStyle(TableStyle([
            ('BOX', (0, 0), (-1, -1), 0.7, colors.HexColor('#243b53')),
            ('INNERGRID', (0, 0), (-1, -1), 0.35,
             colors.HexColor('#cbd2d9')),
            ('SPAN', (1, 2), (3, 2)),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('LEFTPADDING', (0, 0), (-1, -1), 5),
            ('RIGHTPADDING', (0, 0), (-1, -1), 5),
            ('TOPPADDING', (0, 0), (-1, -1), 5),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ]))
        photo = ''
        if account.profile_pic:
            try:
                photo_file = Path(account.profile_pic.path)
                if photo_file.is_file():
                    photo = ReportImage(
                        str(photo_file), width=25 * mm, height=32 * mm)
            except (OSError, ValueError, NotImplementedError):
                logger.warning('Learner photo is unavailable on report card.')
        identity = details
        if photo:
            identity = Table(
                [[photo, details]],
                colWidths=[30 * mm, available_width - 30 * mm],
                style=TableStyle([
                    ('BOX', (0, 0), (0, 0), 0.7,
                     colors.HexColor('#243b53')),
                    ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                    ('LEFTPADDING', (0, 0), (-1, -1), 4),
                    ('RIGHTPADDING', (0, 0), (-1, -1), 4),
                    ('TOPPADDING', (0, 0), (-1, -1), 4),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                ]),
            )
        body.extend([identity, Spacer(1, 4 * mm)])
    if preamble and not report_card:
        body.extend(Paragraph(line, cell_style) for line in preamble)
        body.append(Spacer(1, 3 * mm))
    def make_table(table_headers, table_rows, proportions=None):
        count = len(table_headers)
        if proportions is None or len(proportions) != count:
            proportions = [1 / count] * count
        scale = available_width / sum(proportions)
        column_widths = [scale * value for value in proportions]
        table_data = [[
            Paragraph(escape(str(value)), header_style)
            for value in table_headers
        ]]
        table_data.extend([
            [Paragraph(escape(str(value)), cell_style) for value in row]
            for row in table_rows
        ])
        table = Table(
            table_data, colWidths=column_widths, repeatRows=1,
            hAlign='LEFT', splitByRow=1)
        table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#243b53')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('GRID', (0, 0), (-1, -1), 0.35, colors.HexColor('#cbd2d9')),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [
                colors.white, colors.HexColor('#f2f5f7')]),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('LEFTPADDING', (0, 0), (-1, -1), 7),
            ('RIGHTPADDING', (0, 0), (-1, -1), 7),
            ('TOPPADDING', (0, 0), (-1, -1), 6),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ]))
        return table

    def section_heading(text):
        heading = ParagraphStyle(
            'ReportSectionHeading', parent=cell_style,
            fontName=bold_font, textColor=colors.HexColor('#243b53'),
            fontSize=10, leading=13, spaceBefore=4 * mm, spaceAfter=2 * mm)
        return Paragraph(escape(text), heading)

    if report_template == 'school_summary' and headers:
        sections = []
        section_index = {}
        for row in rows:
            section = str(row[0])
            if section not in section_index:
                section_index[section] = len(sections)
                sections.append((section, []))
            sections[section_index[section]][1].append(row[1:])
        for section, section_rows in sections:
            body.extend([
                section_heading(section),
                make_table(headers[1:], section_rows, [0.30, 0.46, 0.24]),
            ])
    elif report_template == 'class_list':
        learner_rows = [
            row for row in rows
            if len(row) > 1 and row[1] not in ('Boys total', 'Girls total')
        ]
        totals = [
            [row[1], row[-1]] for row in rows
            if len(row) > 1 and row[1] in ('Boys total', 'Girls total')
        ]
        body.append(make_table(headers, learner_rows, [0.08, 0.44, 0.20, 0.28]))
        if totals:
            body.extend([
                section_heading('Class gender totals'),
                make_table(['Category', 'Learners'], totals, [0.65, 0.35]),
            ])
    elif report_template == 'learner_results':
        statistics = []
        grade_counts = []
        learner_rows = []
        for row in rows:
            label = str(row[0])
            if label.startswith('Learners with grade '):
                grade_counts.append([label[len('Learners with grade '):], row[-2]])
            elif label in (
                    'Class average', 'Highest mark', 'Lowest mark',
                    'Pass rate (50% and above)'):
                statistics.append([label, row[-2]])
            else:
                learner_rows.append(row)
        assessment_count = max(0, len(headers) - 3)
        assessment_width = 0.57 / assessment_count if assessment_count else 0
        result_widths = [0.25]
        result_widths.extend([assessment_width] * assessment_count)
        result_widths.extend([0.13, 0.05])
        body.append(make_table(headers, learner_rows, result_widths))
        if statistics:
            body.extend([
                section_heading('Class statistics'),
                make_table(['Measure', 'Value'], statistics, [0.65, 0.35]),
            ])
        if grade_counts:
            body.extend([
                section_heading('Learners by grade'),
                make_table(['Grade', 'Learners'], grade_counts, [0.65, 0.35]),
            ])
    elif report_template == 'assignments':
        unmatched_rows = [
            [row[1], row[2]] for row in rows
            if row[0] == 'Unmatched learner'
        ]
        unconfigured_subjects = [
            [row[1], row[2]] for row in rows
            if row[0] == 'Not set up subject'
        ]
        teacher_rows = [
            row for row in rows
            if row[0] not in ('Unmatched learner', 'Not set up subject')
        ]
        body.extend([
            section_heading('Assignment overview'),
            make_table(
                ['Measure', 'Count'],
                [
                    ['Teacher entries', len(teacher_rows)],
                    ['Unmatched learners', len(unmatched_rows)],
                    ['Subjects needing setup', len(unconfigured_subjects)],
                ],
                [0.72, 0.28],
            ),
        ])
        if teacher_rows:
            body.extend([
                section_heading('Teacher classes and subjects'),
                make_table(
                    [
                        'Teacher', 'Class', 'Subjects', 'Accepted',
                        'Pending', 'Declined',
                    ],
                    teacher_rows,
                    [0.22, 0.12, 0.25, 0.14, 0.13, 0.14],
                ),
            ])
        if unmatched_rows:
            body.extend([
                section_heading('Unmatched learners'),
                make_table(
                    ['Class', 'Learner'], unmatched_rows, [0.30, 0.70]),
            ])
        if unconfigured_subjects:
            body.extend([
                section_heading('Subjects needing setup'),
                make_table(
                    ['Class', 'Subject'], unconfigured_subjects,
                    [0.30, 0.70]),
            ])
    elif report_template in ('activity_log', 'activity_timeline'):
        roles = {str(row[2]) for row in rows}
        actions = {str(row[3]) for row in rows}
        summary_rows = [
            ['Entries', len(rows)],
            ['Roles represented', len(roles)],
            ['Actions represented', len(actions)],
        ]
        if rows:
            summary_rows.append([
                'Date range',
                f'{str(rows[-1][0])[:10]} to {str(rows[0][0])[:10]}',
            ])
        body.extend([
            section_heading(
                'User activity overview'
                if report_template == 'activity_timeline'
                else 'Activity overview'),
            make_table(['Measure', 'Value'], summary_rows, [0.65, 0.35]),
            section_heading(
                'User activity'
                if report_template == 'activity_timeline'
                else 'Activity details'),
            make_table(
                headers, rows,
                [0.14, 0.14, 0.08, 0.11, 0.15, 0.12, 0.18, 0.08]),
        ])
    elif report_template == 'learner_list':
        class_index = headers.index('Class')
        teachers_index = headers.index('Accepted teachers')
        unassigned_count = sum(
            str(row[class_index]) == 'Not assigned' for row in rows)
        waiting_count = sum(
            str(row[teachers_index]) == 'Waiting for a teacher'
            for row in rows)
        body.extend([
            section_heading('Learner register overview'),
            make_table(
                ['Measure', 'Count'],
                [
                    ['Learners listed', len(rows)],
                    ['Without a class', unassigned_count],
                    ['Waiting for a teacher', waiting_count],
                ],
                [0.72, 0.28],
            ),
            section_heading('Learner register'),
            make_table(
                headers, rows,
                [0.24, 0.14, 0.10, 0.18, 0.12, 0.22]),
        ])
    elif report_template == 'teacher_list':
        gender_counts = {}
        for row in rows:
            gender = str(row[2])
            gender_counts[gender] = gender_counts.get(gender, 0) + 1
        body.extend([
            section_heading('Teacher register overview'),
            make_table(
                ['Measure', 'Count'],
                [
                    ['Teachers listed', len(rows)],
                    *[[gender, count] for gender, count in sorted(
                        gender_counts.items())],
                ],
                [0.72, 0.28],
            ),
            section_heading('Teacher register'),
            make_table(
                headers, rows, [0.42, 0.28, 0.30]),
        ])
    elif report_template == 'accepted_learners':
        body.extend([
            section_heading('Accepted learners overview'),
            make_table(
                ['Measure', 'Count'],
                [['Accepted learners', len(rows)]],
                [0.72, 0.28],
            ),
            section_heading('Accepted learner register'),
            make_table(
                headers, rows, [0.32, 0.20, 0.18, 0.30]),
        ])
    elif report_template == 'learner_report_card':
        assessment_count = max(0, len(headers) - 5)
        assessment_width = 0.20 / assessment_count if assessment_count else 0
        body.append(make_table(
            headers,
            rows,
            [
                0.19,
                *([assessment_width] * assessment_count),
                0.08, 0.07, 0.37, 0.09,
            ],
        ))
    else:
        body.append(make_table(headers, rows))
    if report_card:
        overall_total = report_card['overall_total']
        overall_average = report_card['overall_average']
        totals_box = Table([[
            Paragraph(
                '<b>Overall Total:</b> ' +
                (f'{overall_total:.2f}' if overall_total is not None
                 else 'Incomplete'),
                cell_style,
            ),
            Paragraph(
                '<b>Overall Average:</b> ' +
                (f'{overall_average:.2f}' if overall_average is not None
                 else 'Incomplete'),
                cell_style,
            ),
        ]], colWidths=[available_width / 2] * 2)
        totals_box.setStyle(TableStyle([
            ('BOX', (0, 0), (-1, -1), 0.7, colors.HexColor('#243b53')),
            ('INNERGRID', (0, 0), (-1, -1), 0.35,
             colors.HexColor('#cbd2d9')),
            ('LEFTPADDING', (0, 0), (-1, -1), 7),
            ('TOPPADDING', (0, 0), (-1, -1), 7),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 7),
        ]))
        body.extend([Spacer(1, 3 * mm), totals_box, Spacer(1, 3 * mm)])
        comment_box = Table([
            [Paragraph('<b>Class teacher&#39;s comment</b>', cell_style)],
            [' '],
            [' '],
            [Paragraph(
                'Signature: _____________________  Date: __________',
                cell_style)],
            [Paragraph('<b>Principal&#39;s comment</b>', cell_style)],
            [' '],
            [' '],
            [Paragraph(
                'Signature: _____________________  Date: __________',
                cell_style)],
        ], colWidths=[available_width], rowHeights=[
            7 * mm, 7 * mm, 7 * mm, 7 * mm,
            7 * mm, 7 * mm, 7 * mm, 7 * mm,
        ])
        comment_box.setStyle(TableStyle([
            ('BOX', (0, 0), (-1, -1), 0.7, colors.HexColor('#243b53')),
            ('INNERGRID', (0, 0), (-1, -1), 0.35,
             colors.HexColor('#cbd2d9')),
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#f2f5f7')),
            ('BACKGROUND', (0, 4), (-1, 4), colors.HexColor('#f2f5f7')),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ]))
        next_period = report_card['next_period']
        starts = (
            next_period.start_year.strftime('%d %B %Y')
            if next_period and next_period.start_year else '')
        ends = (
            next_period.end_year.strftime('%d %B %Y')
            if next_period and next_period.end_year else '')
        grading_rows = [[
            Paragraph('<b>Grade</b>', header_style),
            Paragraph('<b>Scale</b>', header_style),
            Paragraph('<b>Description</b>', header_style),
            Paragraph('<b>US grade</b>', header_style),
        ]]
        grading_rows.extend([
            [
                grade['symbol'],
                f'{grade["minimum"]:.2f}-{grade["maximum"]:.2f}',
                grade['description'],
                grade['us_grade'],
            ]
            for grade in GRADING_SCALE
        ])
        grading_key = Table(
            grading_rows,
            colWidths=[
                available_width * 0.14,
                available_width * 0.25,
                available_width * 0.39,
                available_width * 0.22,
            ],
            repeatRows=1,
            splitByRow=1,
        )
        grading_key.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#243b53')),
            ('GRID', (0, 0), (-1, -1), 0.35, colors.HexColor('#cbd2d9')),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [
                colors.white, colors.HexColor('#f2f5f7')]),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('LEFTPADDING', (0, 0), (-1, -1), 5),
            ('RIGHTPADDING', (0, 0), (-1, -1), 5),
        ]))
        body.extend([
            comment_box,
            Spacer(1, 3 * mm),
            Paragraph(
                f'<b>Next term begins on:</b> {escape(starts)}'
                f' &nbsp;&nbsp; <b>Ends on:</b> {escape(ends)}',
                cell_style),
            Spacer(1, 3 * mm),
            grading_key,
            Spacer(1, 3 * mm),
            Paragraph(
                '<font color="#b00020"><b>This report is invalid without '
                'a valid school stamp</b></font>',
                ParagraphStyle(
                    'StampNotice', parent=cell_style, alignment=1,
                    fontSize=9, leading=11)),
        ])
    if postamble and not report_card:
        body.extend(Paragraph(line, cell_style) for line in postamble)
    document = SimpleDocTemplate(
        buffer, pagesize=pagesize, rightMargin=16 * mm, leftMargin=16 * mm,
        topMargin=48 * mm, bottomMargin=25 * mm,
        title=title, author=display_name)

    def draw_page(canvas, doc):
        canvas.saveState()
        page_width, page_height = pagesize
        if crest_path:
            try:
                if hasattr(canvas, 'setFillAlpha'):
                    canvas.setFillAlpha(0.07)
                canvas.drawImage(
                    crest_path,
                    page_width / 2 - 35 * mm,
                    page_height / 2 - 35 * mm,
                    width=70 * mm,
                    height=70 * mm,
                    preserveAspectRatio=True,
                    anchor='c',
                    mask='auto',
                )
                if hasattr(canvas, 'setFillAlpha'):
                    canvas.setFillAlpha(1)
            except (OSError, ValueError) as error:
                logger.warning('Could not draw report watermark at %s: %s',
                               crest_path, error)
        if logo_path:
            try:
                canvas.drawImage(
                    logo_path, 16 * mm, page_height - 30 * mm,
                    width=19 * mm, height=19 * mm,
                    preserveAspectRatio=True, anchor='c', mask='auto')
            except (OSError, ValueError) as error:
                logger.warning('Could not draw report logo at %s: %s',
                               logo_path, error)
        canvas.setFillColor(colors.HexColor('#243b53'))
        canvas.setFont('Times-Bold', 15)
        canvas.drawCentredString(page_width / 2, page_height - 15 * mm,
                                 school_name[:100])
        canvas.setFont(regular_font, 8)
        header_y = page_height - 20 * mm
        for contact in contacts:
            canvas.drawCentredString(page_width / 2, header_y, contact[:140])
            header_y -= 4 * mm
        if motto:
            canvas.drawCentredString(page_width / 2, header_y, motto[:140])
            canvas.line(page_width / 2 - 30 * mm, header_y - 1 * mm,
                        page_width / 2 + 30 * mm, header_y - 1 * mm)
        canvas.setStrokeColor(colors.HexColor('#cbd2d9'))
        canvas.line(16 * mm, page_height - 36 * mm,
                    page_width - 16 * mm, page_height - 36 * mm)
        canvas.setFont(regular_font, 8)
        canvas.setFillColor(colors.HexColor('#52606d'))
        canvas.drawString(
            16 * mm, page_height - 42 * mm,
            f'Generated: {generated_at:%d %B %Y %H:%M} by {display_name}')
        canvas.line(16 * mm, 16 * mm, page_width - 16 * mm, 16 * mm)
        canvas.restoreState()

    from reportlab.pdfgen.canvas import Canvas

    def numbered_canvas(*args, **kwargs):
        class NumberedCanvas(Canvas):
            def __init__(self, *canvas_args, **canvas_kwargs):
                super().__init__(*canvas_args, **canvas_kwargs)
                self.saved_page_states = []

            def showPage(self):
                self.saved_page_states.append(dict(self.__dict__))
                self._startPage()

            def save(self):
                total_pages = len(self.saved_page_states)
                for state in self.saved_page_states:
                    self.__dict__.update(state)
                    self.setFont(regular_font, 8)
                    self.setFillColor(colors.HexColor('#52606d'))
                    self.drawRightString(
                        pagesize[0] - 16 * mm,
                        10 * mm,
                        f'Page {self._pageNumber} of {total_pages}')
                    Canvas.showPage(self)
                Canvas.save(self)

        return NumberedCanvas(*args, **kwargs)

    document.build(
        body,
        onFirstPage=draw_page,
        onLaterPages=draw_page,
        canvasmaker=numbered_canvas,
    )
    response = HttpResponse(buffer.getvalue(), content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


def _export_data(
        request, user, title, headers, rows, export_format,
        academic_period=None, preamble=None, postamble=None,
        report_card=None, report_template='table', audit_user=None):
    if not rows:
        return None
    if export_format not in ('pdf', 'csv'):
        raise ValueError(f'Unsupported export format: {export_format}')

    period = academic_period or AcademicPeriod.objects.filter(
        is_current=True).first()
    filename_slug = slugify(title) or 'sims-export'
    period_slug = slugify(str(period)) if period else 'academic-period-unset'
    if period_slug not in filename_slug:
        filename_slug = f'{filename_slug}-{period_slug}'
    filename = f'{filename_slug}.{export_format}'

    if export_format == 'pdf':
        response = _create_report_pdf(
            user,
            title,
            headers,
            rows,
            filename=filename,
            preamble=preamble,
            postamble=postamble,
            report_card=report_card,
            report_template=report_template,
        )
    else:
        response = HttpResponse(
            content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = (
            f'attachment; filename="{filename}"')
        response.write('\ufeff')
        writer = csv.writer(response)
        profile = SchoolProfile.objects.order_by('pk').first()
        school_name = (
            profile.school_name if profile and profile.school_name
            else 'C. Heuva Junior Secondary School')
        generated_at = timezone.localtime(timezone.now())
        writer.writerow([_csv_safe(school_name)])
        writer.writerow([_csv_safe(title)])
        writer.writerow([_csv_safe(
            f'Generated: {generated_at:%d %B %Y %H:%M} by '
            f'{user.get_full_name().strip() or user.username}')])
        if profile:
            for label, value in (
                ('Postal address', profile.postal_address),
                ('Phone numbers', profile.phone_numbers),
                ('Motto', profile.motto),
            ):
                if value and value.strip():
                    writer.writerow([_csv_safe(label), _csv_safe(value)])
        writer.writerow([])
        if preamble:
            writer.writerows([
                [_csv_safe(unescape(re.sub(r'<[^>]+>', '', line)))]
                for line in preamble
            ])
            writer.writerow([])
        if report_card:
            student = report_card['student']
            writer.writerow([
                _csv_safe('Learner'),
                _csv_safe(
                    student.admin.get_full_name().strip()
                    or student.admin.username),
            ])
            writer.writerow([
                _csv_safe('Account ID'), _csv_safe(student.admin.username)])
            writer.writerow([
                _csv_safe('Class'),
                _csv_safe(student.school_class or 'Not assigned')])
            writer.writerow([
                _csv_safe('Gender'),
                _csv_safe(student.admin.get_gender_display())])
            writer.writerow([
                _csv_safe('Academic period'),
                _csv_safe(report_card['academic_period'])])
            writer.writerow([])
        writer.writerow([_csv_safe(value) for value in headers])
        writer.writerows([
            [_csv_safe(value) for value in row]
            for row in rows
        ])
        if postamble and not report_card:
            writer.writerow([])
            writer.writerows([
                [unescape(re.sub(r'<[^>]+>', '', line))]
                for line in postamble
            ])
        if report_card:
            writer.writerow([])
            writer.writerow([
                'Overall Total',
                report_card['overall_total']
                if report_card['overall_total'] is not None
                else 'Incomplete',
            ])
            writer.writerow([
                'Overall Average',
                report_card['overall_average']
                if report_card['overall_average'] is not None
                else 'Incomplete',
            ])
            next_period = report_card['next_period']
            writer.writerow([
                'Next term begins on',
                next_period.start_year if next_period else '',
            ])
            writer.writerow([
                'Ends on',
                next_period.end_year if next_period else '',
            ])
            writer.writerow([])
            writer.writerow([
                'Grade', 'Scale', 'Description', 'US grade'])
            writer.writerows([
                [
                    grade['symbol'],
                    f'{grade["minimum"]:.2f}-{grade["maximum"]:.2f}',
                    grade['description'],
                    grade['us_grade'],
                ]
                for grade in GRADING_SCALE
            ])

    log_actor = audit_user or user
    ActivityLog.objects.create(
        actor=log_actor,
        actor_username=log_actor.get_username() or '',
        action='export',
        object_type='data_export',
        object_id='',
        description=f'Exported {title} as {export_format.upper()}'[:255],
        changes={
            'format': export_format,
            'row_count': len(rows),
        },
    )
    return response


def _csv_safe(value):
    cell = '' if value is None else str(value)
    if cell.lstrip().startswith(('=', '+', '-', '@')):
        return "'" + cell
    return cell


def _report_crest_path():
    relative_path = 'main_app/img/crest-logo.png'
    crest_path = finders.find(relative_path)
    if crest_path and Path(crest_path).is_file():
        return crest_path

    collected_path = Path(settings.STATIC_ROOT) / relative_path
    if collected_path.is_file():
        return str(collected_path)

    logger.warning('Report crest image was not found at %s or %s',
                   relative_path, collected_path)
    return None


def add_student(request):
    if request.method != 'POST':
        return redirect(reverse('manage_student'))

    student_form = StudentForm(request.POST or None, request.FILES or None)
    context = {'form': student_form, 'page_title': 'Add Learner'}
    if request.method == 'POST':
        if student_form.is_valid():
            first_name = student_form.cleaned_data.get('first_name')
            last_name = student_form.cleaned_data.get('last_name')
            address = student_form.cleaned_data.get('address')
            gender = student_form.cleaned_data.get('gender')
            password = student_form.cleaned_data.get('password')
            passport = request.FILES.get('profile_pic')
            passport_url = ''
            if passport:
                fs = FileSystemStorage()
                filename = fs.save(passport.name, passport)
                passport_url = fs.url(filename)
            with transaction.atomic():
                user = CustomUser.objects.create_user(
                    email='',
                    password=password,
                    user_type='2',
                    first_name=first_name,
                    last_name=last_name,
                    gender=gender,
                    address=address,
                    profile_pic=passport_url,
                )
                student = user.student
                student.school_class = school_class_from_label(
                    student_form.cleaned_data.get('grade_and_class'))
                student.academic_period = student_form.cleaned_data.get('academic_period')
                accepting_teacher = None
                if request.user.user_type == '3':
                    accepting_teacher = teacher_profile(request.user)
                    student.created_by = accepting_teacher
                student.save()
                create_class_requests(
                    student, accepted_teacher=accepting_teacher)
            messages.success(request, 'Learner account added successfully.')
            return redirect(reverse('manage_student'))
        else:
            messages.error(request, "Could Not Add: ")
    return _render_manage_students(
        request, student_form=student_form, open_modal='addStudentModal')


def _render_manage_students(request, student_form=None, open_modal=None):
    if student_form is None:
        student_form = StudentForm()
    learner_ids_without_profiles = CustomUser.objects.filter(
        user_type='2', student__isnull=True).values_list('id', flat=True)
    for learner_id in learner_ids_without_profiles:
        Student.objects.get_or_create(admin_id=learner_id)
    filter_form = TeacherGradeFilterForm(request.GET)
    students = visible_students(request.user)
    if request.user.user_type == '1' and filter_form.is_valid():
        if filter_form.cleaned_data['teacher']:
            students = students.filter(
                teacher_requests__teacher=filter_form.cleaned_data['teacher'],
                teacher_requests__status=LearnerTeacherRequest.ACCEPTED,
            ).distinct()
        if filter_form.cleaned_data['school_class']:
            students = students.filter(
                school_class=filter_form.cleaned_data['school_class'])
    search_query = request.GET.get('search', '').strip()
    if search_query:
        students = students.filter(
            Q(admin__first_name__icontains=search_query)
            | Q(admin__last_name__icontains=search_query)
            | Q(admin__username__icontains=search_query)
        )
    learner_accounts = CustomUser.objects.filter(
        pk__in=students.values('admin_id')).select_related(
            'student__created_by__teacher',
            'student__school_class',
            'student__academic_period',
    )
    learner_accounts = sort_learners(
        learner_accounts,
        lambda account: account,
    )
    for account in learner_accounts:
        account.accepted_teacher_names = list(
            account.student.teacher_requests.filter(
                status=LearnerTeacherRequest.ACCEPTED,
            ).select_related('teacher__teacher').values_list(
                'teacher__teacher__first_name',
                'teacher__teacher__last_name',
                'teacher__teacher__username',
            )
        )
    export_query_params = request.GET.copy()
    export_query_params.pop('export', None)
    export_query_params.pop('page', None)
    export_format = request.GET.get('export')
    matching_count = len(learner_accounts)
    if export_format in ('pdf', 'csv'):
        rows = [[
            account.get_full_name() or account.username,
            account.username,
            account.get_gender_display(),
            account.student.school_class or 'Not assigned',
            account.student.academic_period or 'Not assigned',
            ', '.join(
                ' '.join(filter(None, (first_name, last_name))) or username
                for first_name, last_name, username
                in account.accepted_teacher_names
            ) or 'Waiting for a teacher',
        ] for account in learner_accounts]
        if not rows:
            messages.warning(request, 'No learners are available to export.')
        else:
            return _export_data(
                request,
                request.user,
                'Learner List',
                [
                    'Learner name', 'Account ID', 'Gender', 'Class',
                    'Academic Period', 'Accepted teachers',
                ],
                rows,
                export_format,
                report_template='learner_list',
            )
    elif export_format:
        return HttpResponse('Unsupported export format.', status=400)

    context = {
        'students': learner_accounts,
        'student_form': student_form,
        'filter_form': filter_form,
        'open_modal': open_modal,
        'search_query': search_query,
        'matching_count': matching_count,
        'export_query_params': export_query_params.urlencode(),
        'page_title': 'Manage Learners',
    }
    return render(request, "hod_template/manage_student.html", context)


def manage_student(request):
    return _render_manage_students(request)


@login_required
@require_POST
def reset_imported_learner_activation(request, student_id):
    if request.user.user_type not in ('1', '3'):
        return redirect(reverse('login_page'))
    student = get_object_or_404(
        visible_students(request.user).select_related('admin'),
        pk=student_id,
    )
    account = student.admin
    if not account.is_imported_learner:
        messages.error(
            request,
            'Only imported learners can have first-sign-in status reset.',
        )
    elif account.password_setup_required:
        messages.info(
            request,
            'This learner is already marked as not yet activated.',
        )
    else:
        account.set_unusable_password()
        account.password_setup_required = True
        account.save(update_fields=[
            'password', 'password_setup_required',
        ])
        ActivityLog.objects.create(
            actor=request.user,
            actor_username=request.user.get_username() or '',
            target_user=account,
            target_username=account.get_username() or '',
            target_user_id_snapshot=str(account.pk),
            action='update',
            object_type='authentication',
            object_id=str(account.pk),
            description='Staff reset an imported learner first-sign-in status.',
            changes={},
        )
        messages.success(
            request,
            'The learner must set a new password at their next sign-in.',
        )
    return redirect(reverse('manage_student'))


def _render_learner_import_page(
    request,
    upload_form=None,
    options_form=None,
    preview_batch=None,
    import_batch=None,
):
    if upload_form is None:
        upload_form = LearnerImportUploadForm()
    if import_batch is None:
        import_batch = LearnerImportBatch.objects.filter(
            created_by=request.user,
            status__in=(
                LearnerImportBatch.PROCESSING,
                LearnerImportBatch.FAILED,
            ),
        ).first()
        if import_batch is None:
            import_batch = LearnerImportBatch.objects.filter(
                created_by=request.user,
                status=LearnerImportBatch.COMPLETED,
                credentials_acknowledged_at__isnull=True,
            ).first()
    preview_rows = (
        sorted(
            preview_batch.rows,
            key=lambda row: learner_name_sort_key(
                row['data'].get('last_name'),
                row['data'].get('first_name'),
                row['data'].get('account_id'),
            ),
        )
        if preview_batch else []
    )
    if options_form is None:
        options_form = LearnerImportOptionsForm(
            user=request.user,
            missing_class_count=sum(
                not row['data'].get('class_name') for row in preview_rows
            ),
            imported_class_labels={
                row['data'].get('class_name') for row in preview_rows
                if row['data'].get('class_name')
            },
        )
    return render(request, 'hod_template/import_learners.html', {
        'upload_form': upload_form,
        'options_form': options_form,
        'preview_batch': preview_batch,
        'preview_rows': preview_rows,
        'preview_has_errors': any(
            row['errors'] for row in preview_rows),
        'missing_class_count': options_form.missing_class_count,
        'class_suggestions': options_form.class_suggestions,
        'import_batch': import_batch,
        'max_rows': settings.SIMS_LEARNER_IMPORT_MAX_ROWS,
        'page_title': 'Import Learners',
    })


def _recover_legacy_learner_preview(request):
    preview = request.session.pop('learner_import_preview', None)
    if not preview:
        return None

    rows = preview.get('rows', [])
    try:
        is_expired = (
            timezone.now().timestamp()
            - float(preview.get('created_at', 0)) > 900)
    except (TypeError, ValueError):
        is_expired = True
    if (
        is_expired
        or not rows
        or len(rows) > settings.SIMS_LEARNER_IMPORT_MAX_ROWS
    ):
        messages.warning(
            request,
            'The interrupted learner preview expired. Upload the file again '
            'to start a new import.',
        )
        return None

    account_ids = [
        row.get('data', {}).get('account_id')
        for row in rows
        if row.get('data', {}).get('account_id')
    ]
    existing_ids = set(CustomUser.objects.filter(
        username__in=account_ids).values_list('username', flat=True))
    if existing_ids:
        messages.error(
            request,
            'Some account IDs in the interrupted preview already exist. '
            'Review Manage Learners before importing the file again; this '
            'preview was not resumed to avoid duplicate accounts.',
        )
        return None

    batch = LearnerImportBatch.objects.create(
        created_by=request.user,
        rows=rows,
        total_rows=len(rows),
        status=LearnerImportBatch.PREVIEW,
    )
    messages.info(
        request,
        f'Recovered the interrupted preview ({len(rows)} learner rows). '
        'Review it below to continue.',
    )
    return batch


@login_required
def import_learners(request):
    if request.user.user_type not in ('1', '3'):
        return redirect(reverse('login_page'))

    if request.method == 'POST' and request.POST.get('action') == 'upload':
        request.session.pop('learner_import_preview', None)
        upload_form = LearnerImportUploadForm(request.POST, request.FILES)
        if upload_form.is_valid():
            try:
                preview_rows = parse_learner_upload(
                    upload_form.cleaned_data['file'])
            except LearnerImportError as error:
                upload_form.add_error('file', str(error))
            else:
                batch = LearnerImportBatch.objects.create(
                    created_by=request.user,
                    rows=preview_rows,
                    total_rows=len(preview_rows),
                    status=LearnerImportBatch.PREVIEW,
                )
                return _render_learner_import_page(
                    request,
                    preview_batch=batch,
                )
        return _render_learner_import_page(
            request,
            upload_form=upload_form,
        )

    if request.method == 'POST' and request.POST.get('action') == 'confirm':
        batch = get_object_or_404(
            LearnerImportBatch,
            token=request.POST.get('import_token'),
            created_by=request.user,
            status=LearnerImportBatch.PREVIEW,
        )
        if timezone.now() - batch.created_at > timedelta(minutes=15):
            batch.status = LearnerImportBatch.FAILED
            batch.error_message = (
                'This import preview expired. Upload the file again.')
            batch.save(update_fields=['status', 'error_message', 'updated_at'])
            messages.error(request, batch.error_message)
            return redirect(reverse('import_learners'))

        preview_rows = validate_learner_rows(batch.rows)
        missing_class_count = sum(
            not row['data'].get('class_name') for row in preview_rows)
        options_form = LearnerImportOptionsForm(
            request.POST,
            user=request.user,
            missing_class_count=missing_class_count,
            imported_class_labels={
                row['data'].get('class_name') for row in preview_rows
                if row['data'].get('class_name')
            },
        )
        batch.rows = preview_rows
        batch.save(update_fields=['rows', 'updated_at'])
        if options_form.is_valid() and not any(
            row['errors'] for row in preview_rows
        ):
            class_for_blank_rows = options_form.cleaned_data.get(
                'class_for_blank_rows')
            if class_for_blank_rows:
                for row in preview_rows:
                    if not row['data'].get('class_name'):
                        row['data']['class_name'] = class_for_blank_rows
                        row['data']['class'] = class_for_blank_rows
            batch.academic_period = options_form.cleaned_data[
                'academic_period']
            batch.teacher = (
                teacher_profile(request.user)
                if request.user.user_type == '3' else None
            )
            batch.status = LearnerImportBatch.PROCESSING
            batch.rows = preview_rows
            batch.save(update_fields=[
                'rows',
                'academic_period',
                'teacher',
                'status',
                'updated_at',
            ])
            return _render_learner_import_page(
                request,
                import_batch=batch,
            )
        return _render_learner_import_page(
            request,
            options_form=options_form,
            preview_batch=batch,
        )

    preview_batch = _recover_legacy_learner_preview(request)
    if preview_batch is None:
        preview_batch = LearnerImportBatch.objects.filter(
            created_by=request.user,
            status=LearnerImportBatch.PREVIEW,
        ).first()
        if (
            preview_batch
            and timezone.now() - preview_batch.created_at
            > timedelta(minutes=15)
        ):
            preview_batch.status = LearnerImportBatch.FAILED
            preview_batch.error_message = (
                'This import preview expired. Upload the file again.')
            preview_batch.save(update_fields=[
                'status', 'error_message', 'updated_at',
            ])
            preview_batch = None
            messages.warning(
                request,
                'The learner preview expired. Upload the file again.',
            )
    return _render_learner_import_page(
        request,
        preview_batch=preview_batch,
    )


def _learner_import_batch_for_user(request, token):
    if request.user.user_type not in ('1', '3'):
        return None
    return get_object_or_404(
        LearnerImportBatch,
        token=token,
        created_by=request.user,
    )


@login_required
@require_GET
def learner_import_status(request, token):
    batch = _learner_import_batch_for_user(request, token)
    if batch is None:
        return HttpResponseForbidden('Staff access is required.')
    return JsonResponse({
        'status': batch.status,
        'recorded': batch.imported_rows,
        'total': batch.total_rows,
        'error': batch.error_message,
    })


@login_required
@require_POST
def process_learner_import(request, token):
    batch = _learner_import_batch_for_user(request, token)
    if batch is None:
        return HttpResponseForbidden('Staff access is required.')
    if batch.status != LearnerImportBatch.PROCESSING:
        return JsonResponse({
            'status': batch.status,
            'recorded': batch.imported_rows,
            'total': batch.total_rows,
            'error': batch.error_message,
        })

    invalid_class_row = None
    for row in batch.rows:
        try:
            parsed_class = parse_class_label(
                row.get('data', {}).get('class_name', ''))
        except ClassLabelError:
            parsed_class = None
        if parsed_class is None:
            invalid_class_row = row
            break
    if invalid_class_row is not None:
        line_number = invalid_class_row.get('line')
        batch.status = LearnerImportBatch.FAILED
        batch.error_message = (
            f'Choose a valid class for import row {line_number}.'
            if line_number is not None
            else 'Choose a valid class for an import row.')
        batch.save(update_fields=['status', 'error_message', 'updated_at'])
        return JsonResponse({
            'status': batch.status,
            'recorded': batch.imported_rows,
            'total': batch.total_rows,
            'error': batch.error_message,
        }, status=409)

    try:
        with transaction.atomic():
            batch = LearnerImportBatch.objects.select_for_update().get(
                pk=batch.pk)
            if batch.status != LearnerImportBatch.PROCESSING:
                return JsonResponse({
                    'status': batch.status,
                    'recorded': batch.imported_rows,
                    'total': batch.total_rows,
                    'error': batch.error_message,
                })

            first_index = batch.imported_rows
            end_index = min(
                first_index + LEARNER_IMPORT_CHUNK_SIZE,
                batch.total_rows,
            )
            class_cache = {}
            class_setup_cache = set()
            for row_index in range(first_index, end_index):
                row = batch.rows[row_index]
                data = row['data']
                account_id = data['account_id']
                user = CustomUser.objects.create_user(
                    username=account_id,
                    password=None,
                    user_type='2',
                    is_imported_learner=True,
                    password_setup_required=True,
                    first_name=data['first_name'],
                    last_name=data['last_name'],
                    gender=data['gender'],
                    email='',
                    address='',
                )
                student = user.student
                class_label = data['class_name']
                if class_label not in class_cache:
                    class_cache[class_label] = school_class_from_label(
                        class_label)
                if batch.teacher and class_label not in class_setup_cache:
                    TeacherClassSetup.objects.get_or_create(
                        teacher=batch.teacher,
                        school_class=class_cache[class_label],
                    )
                    class_setup_cache.add(class_label)
                student.school_class = class_cache[class_label]
                student.academic_period = batch.academic_period
                student.created_by = batch.teacher
                student.save()
                create_class_requests(
                    student,
                    accepted_teacher=(
                        batch.teacher
                        if request.user.user_type == '3' else None
                    ),
                )

            batch.imported_rows = end_index
            if batch.imported_rows == batch.total_rows:
                batch.status = LearnerImportBatch.COMPLETED
                ActivityLog.objects.create(
                    actor=request.user,
                    actor_username=request.user.get_username() or '',
                    action='import',
                    object_type='learner_import',
                    description=(
                        f'Imported {batch.total_rows} learners.'),
                    changes={'row_count': batch.total_rows},
                )
            batch.save(update_fields=[
                'imported_rows', 'status', 'updated_at',
            ])
    except OperationalError:
        logger.warning(
            'Database busy while processing learner import %s at row %s.',
            token,
            batch.imported_rows + 1,
            exc_info=True,
        )
        batch.refresh_from_db()
        return JsonResponse({
            'status': batch.status,
            'recorded': batch.imported_rows,
            'total': batch.total_rows,
            'error': (
                'The database is busy. This batch was not recorded; '
                'previously saved learners are safe. Resume the import '
                'when the connection is available.'
            ),
        }, status=503)
    except IntegrityError:
        logger.exception(
            'Integrity error while processing learner import %s at row %s.',
            token,
            batch.imported_rows + 1,
        )
        batch.refresh_from_db()
        batch.status = LearnerImportBatch.FAILED
        batch.error_message = (
            'The current batch could not be saved because one or more '
            'account IDs conflict with existing records. No learners from '
            'this batch were added; previously saved learners are safe. '
            'Review the account IDs before uploading again.'
        )
        batch.save(update_fields=['status', 'error_message', 'updated_at'])
        return JsonResponse({
            'status': batch.status,
            'recorded': batch.imported_rows,
            'total': batch.total_rows,
            'error': batch.error_message,
        }, status=409)

    return JsonResponse({
        'status': batch.status,
        'recorded': batch.imported_rows,
        'total': batch.total_rows,
        'error': batch.error_message,
    })


@login_required
@require_http_methods(['GET', 'POST'])
def complete_learner_import(request, token):
    batch = _learner_import_batch_for_user(request, token)
    if batch is None:
        return HttpResponseForbidden('Staff access is required.')
    if request.method == 'POST':
        if batch.status != LearnerImportBatch.COMPLETED:
            return redirect(reverse('import_learners'))
        batch.credentials_acknowledged_at = timezone.now()
        batch.rows = []
        batch.save(update_fields=[
            'credentials_acknowledged_at', 'rows', 'updated_at',
        ])
        messages.success(
            request,
            'The one-time learner credentials have been hidden.',
        )
        return redirect(reverse('import_learners'))
    if (
        batch.status != LearnerImportBatch.COMPLETED
        or batch.credentials_acknowledged_at is not None
    ):
        return redirect(reverse('import_learners'))

    account_ids = [
        row['data']['account_id'] for row in batch.rows
    ]
    accounts = CustomUser.objects.in_bulk(
        account_ids, field_name='username')
    imported_accounts = []
    for row in batch.rows:
        account_id = row['data']['account_id']
        account = accounts.get(account_id)
        if account is None:
            messages.error(
                request,
                'Learner credentials could not be retrieved. Contact an '
                'administrator before importing this file again.',
            )
            return redirect(reverse('import_learners'))
        imported_accounts.append(account)
    credentials = [{
            'name': account.get_full_name() or account.username,
            'school_class': str(account.student.school_class or ''),
            'account_id': account.username,
        }
        for account in sort_learners(
            imported_accounts, lambda imported_account: imported_account)
    ]

    response = render(
        request,
        'hod_template/import_learner_credentials.html',
        {
            'credentials': credentials,
            'import_batch': batch,
            'page_title': 'Learner Import Complete',
            'school_profile': SchoolProfile.objects.order_by('pk').first(),
            'generated_at': timezone.localtime(timezone.now()),
            'generated_by': (
                request.user.get_full_name().strip()
                or request.user.username
            ),
        },
    )
    response['Cache-Control'] = (
        'no-store, no-cache, must-revalidate, private')
    response['Pragma'] = 'no-cache'
    return response


@login_required
def download_learner_import_template(request):
    if request.user.user_type not in ('1', '3'):
        return redirect(reverse('login_page'))

    export_format = request.GET.get('format', 'csv').lower()
    if export_format == 'csv':
        response = HttpResponse(content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = (
            'attachment; filename="learner-import-template.csv"')
        response.write('\ufeff')
        csv.writer(response).writerow([
            'first_name', 'last_name', 'gender', 'class',
        ])
        return response
    if export_format == 'xlsx':
        from openpyxl import Workbook

        workbook = Workbook(write_only=True)
        worksheet = workbook.create_sheet('Learners')
        worksheet.append([
            'first_name', 'last_name', 'gender', 'class',
        ])
        buffer = BytesIO()
        workbook.save(buffer)
        response = HttpResponse(
            buffer.getvalue(),
            content_type=(
                'application/vnd.openxmlformats-officedocument.'
                'spreadsheetml.sheet'),
        )
        response['Content-Disposition'] = (
            'attachment; filename="learner-import-template.xlsx"')
        return response
    return HttpResponse('Unsupported template format.', status=400)


def edit_student(request, student_id):
    student = get_object_or_404(
        visible_students(request.user), id=student_id)
    form = StudentForm(
        request.POST or None,
        request.FILES or None,
        instance=student,
    )
    context = {
        'form': form,
        'student_id': student_id,
        'page_title': 'Edit Learner'
    }
    if request.method == 'POST':
        if form.is_valid():
            first_name = form.cleaned_data.get('first_name')
            last_name = form.cleaned_data.get('last_name')
            address = form.cleaned_data.get('address')
            gender = form.cleaned_data.get('gender')
            password = form.cleaned_data.get('password') or None
            school_class = school_class_from_label(
                form.cleaned_data.get('grade_and_class'))
            academic_period = form.cleaned_data.get('academic_period')
            passport = request.FILES.get('profile_pic') or None
            try:
                user = CustomUser.objects.get(id=student.admin.id)
                if passport != None:
                    fs = FileSystemStorage()
                    filename = fs.save(passport.name, passport)
                    passport_url = fs.url(filename)
                    user.profile_pic = passport_url
                if password != None:
                    user.set_password(password)
                    user.failed_login_attempts = 0
                    user.locked_until = None
                user.first_name = first_name
                user.last_name = last_name
                user.gender = gender
                user.address = address
                user.save()
                class_changed = student.school_class_id != getattr(
                    school_class, 'pk', None)
                if class_changed:
                    reset_class_requests(student)
                student.school_class = school_class
                student.academic_period = academic_period
                student.save()
                if class_changed:
                    create_class_requests(student)
                messages.success(request, "Successfully Updated")
                return redirect(reverse('edit_student', args=[student_id]))
            except Exception as e:
                messages.error(request, "Could Not Update " + str(e))
        else:
            messages.error(request, "Please Fill Form Properly!")
    else:
        return render(request, "hod_template/edit_student_template.html", context)
    return render(request, "hod_template/edit_student_template.html", context)


def _initial_assessment_rows(school_class, subject, academic_period):
    plan = AssessmentPlan.objects.filter(
        school_class=school_class,
        subject=subject,
        academic_period=academic_period,
    ).first()
    source_plan = plan or previous_period_plan(
        subject, school_class, academic_period)
    assessments = active_assessments(source_plan)
    if assessments:
        rows = [
            {
                'pk': item.pk if plan else '',
                'name': item.name,
                'weight': item.weight,
                'maximum_mark': item.maximum_mark,
                'index': index,
            }
            for index, item in enumerate(assessments)
        ]
    else:
        rows = [
            {
                'pk': '',
                'name': name,
                'weight': Decimal('50'),
                'maximum_mark': Decimal('100'),
                'index': index,
            }
            for index, name in enumerate(('Test 1', 'Examination'))
        ]
    return plan, rows


def _accepted_learners_for_subject(user, school_class, subject):
    if not subject.teacher_id:
        return Student.objects.none()
    if user.user_type == '3' and subject.teacher.teacher_id != user.pk:
        return Student.objects.none()
    return Student.objects.filter(
        school_class=school_class,
        teacher_requests__teacher=subject.teacher,
        teacher_requests__status=LearnerTeacherRequest.ACCEPTED,
    ).select_related('admin').distinct().order_by(
        'admin__last_name', 'admin__first_name', 'admin__username')


def _result_rows_for_selection(school_class, subject, academic_period, learners):
    results = StudentResult.objects.filter(
        subject=subject,
        student__in=learners,
    ).filter(
        Q(academic_period=academic_period)
        | Q(
            academic_period__isnull=True,
            student__academic_period=academic_period,
        )
    ).select_related('student__admin', 'student__school_class')
    results_by_student = {}
    for result in results.order_by('-created_at', '-pk'):
        results_by_student.setdefault(result.student_id, result)
    return results_by_student


def _posted_assessment_rows(request, plan, starting_rows):
    submitted_ids = request.POST.getlist('assessment_ids')
    if not submitted_ids:
        return (
            starting_rows,
            ['Select a class, subject, and academic period first.'],
            {},
        )

    remove_id = request.POST.get('remove_assessment', '')
    errors = []
    field_errors = {}
    rows = []
    names = set()
    for index, raw_id in enumerate(submitted_ids):
        if raw_id and not raw_id.isdigit():
            errors.append('An assessment selection is invalid.')
            continue
        if remove_id and raw_id == remove_id:
            continue
        assessment = None
        if raw_id:
            assessment = (
                plan.assessments.filter(
                    pk=int(raw_id), is_previous_record=False).first()
                if plan else None
            )
            if assessment is None:
                errors.append('An assessment does not belong to this setup.')
                continue
        name = request.POST.get(f'assessment_{index}_name', '').strip()
        name_field = f'assessment_{index}_name'
        weight_field = f'assessment_{index}_weight'
        maximum_field = f'assessment_{index}_maximum'
        raw_weight = request.POST.get(f'assessment_{index}_weight', '')
        raw_maximum = request.POST.get(
            f'assessment_{index}_maximum', '')
        if not name or len(name) > 100:
            field_errors[name_field] = (
                'Assessment names must contain 1 to 100 characters.')
            continue
        if name.casefold() == 'previous total (test + exam)':
            field_errors[name_field] = (
                'That assessment name is reserved for previous records.')
            continue
        row_has_error = False
        try:
            weight = Decimal(raw_weight)
        except (InvalidOperation, TypeError, ValueError):
            field_errors[weight_field] = (
                f'Enter a valid weight for {name}.')
            weight = None
            row_has_error = True
        if weight is not None and (
            not weight.is_finite()
            or weight.as_tuple().exponent < -2
            or not Decimal('0') <= weight <= Decimal('100')
        ):
            field_errors[weight_field] = (
                f'{name} weight must be between 0 and 100%.')
            row_has_error = True
        try:
            maximum = Decimal(raw_maximum)
        except (InvalidOperation, TypeError, ValueError):
            field_errors[maximum_field] = (
                f'Enter a valid maximum mark for {name}.')
            maximum = None
            row_has_error = True
        if maximum is not None and (
            not maximum.is_finite()
            or maximum.as_tuple().exponent < -2
            or not Decimal('0') < maximum <= Decimal('99999.99')
        ):
            field_errors[maximum_field] = (
                f'{name} maximum mark must be greater than zero.')
            row_has_error = True
        if name.casefold() in names:
            field_errors[name_field] = (
                'Assessment names must be unique.')
            row_has_error = True
        names.add(name.casefold())
        if not row_has_error:
            rows.append({
                'assessment': assessment,
                'name': name,
                'weight': weight,
                'maximum_mark': maximum,
                'index': index,
            })

    if not rows and not field_errors:
        errors.append('Keep at least one assessment in the setup.')
    if remove_id:
        removal = (
            plan.assessments.filter(
                pk=remove_id, is_previous_record=False).first()
            if plan and remove_id.isdigit() else None
        )
        if removal is None or removal.name.casefold() == 'examination':
            errors.append('The examination cannot be removed.')
    return rows, errors, field_errors


def _posted_scores(request, rows, learners):
    values = {}
    field_errors = {}
    errors = []
    expected_fields = set()
    submitted_assessment_count = len(
        request.POST.getlist('assessment_ids'))
    for learner in learners:
        expected_fields.update(
            f'score_{learner.pk}_{index}'
            for index in range(submitted_assessment_count)
        )
        learner_values = {}
        for row in rows:
            field_name = f"score_{learner.pk}_{row['index']}"
            raw_value = request.POST.get(field_name, '').strip()
            if not raw_value:
                learner_values[row['index']] = None
                continue
            try:
                score = Decimal(raw_value)
            except (InvalidOperation, TypeError, ValueError):
                field_errors[field_name] = (
                    f'Enter a valid mark for {learner.admin.get_full_name()} '
                    f'in {row["name"]}.')
                continue
            if (
                not score.is_finite()
                or score < 0
                or score > row['maximum_mark']
            ):
                field_errors[field_name] = (
                    f'{row["name"]} marks must be between 0 and '
                    f'{row["maximum_mark"]}.')
                continue
            learner_values[row['index']] = score
        values[learner.pk] = learner_values
    if any(
        field_name.startswith('score_') and field_name not in expected_fields
        for field_name in request.POST
    ):
        errors.append(
            'Marks can only be saved for learners and assessments in this '
            'selection.')
    return values, field_errors, errors


def _remove_assessment_scores(plan, assessment_id):
    results = StudentResult.objects.filter(
        subject=plan.subject,
    ).filter(
        Q(academic_period=plan.academic_period)
        | Q(
            academic_period__isnull=True,
            student__academic_period=plan.academic_period,
        )
    )
    key = str(assessment_id)
    for result in results:
        scores = dict(result.assessment_scores or {})
        if scores.pop(key, None) is not None:
            result.assessment_scores = scores
            result.save()


@login_required
def manage_student_results(request):
    if request.user.user_type == '2':
        return redirect(reverse('student_home'))
    if request.user.user_type not in ('1', '3'):
        return redirect(reverse('admin_home'))

    filter_form = ManageResultsFilterForm(
        request.POST if request.method == 'POST' else request.GET or None,
        user=request.user,
    )
    selected_class = selected_subject = selected_period = None
    if filter_form.is_valid():
        selected_class = filter_form.cleaned_data.get('school_class')
        selected_subject = filter_form.cleaned_data.get('subject')
        selected_period = filter_form.cleaned_data.get('academic_period')

    plan = None
    assessment_rows = []
    initial_assessment_rows = []
    learners = []
    learner_rows = []
    assessment_field_errors = {}
    mark_field_errors = {}
    if selected_class and selected_subject and selected_period:
        plan, assessment_rows = _initial_assessment_rows(
            selected_class, selected_subject, selected_period)
        for row in assessment_rows:
            row['original_name'] = row['name']
            row['original_weight'] = row['weight']
            row['original_maximum_mark'] = row['maximum_mark']
        initial_assessment_rows = [
            dict(row) for row in assessment_rows
        ]
        learners = sort_learners(
            _accepted_learners_for_subject(
                request.user, selected_class, selected_subject),
            lambda learner: learner.admin,
        )
        existing_results = _result_rows_for_selection(
            selected_class, selected_subject, selected_period, learners)
        if request.method == 'POST':
            submitted_ids = request.POST.getlist('assessment_ids')
            if submitted_ids:
                initial_rows_by_id = {
                    str(row['pk']): row
                    for row in initial_assessment_rows
                    if row.get('pk')
                }
                submitted_rows = []
                for index, raw_id in enumerate(submitted_ids):
                    original = initial_rows_by_id.get(raw_id)
                    if (
                        original is None
                        and not raw_id
                        and index < len(initial_assessment_rows)
                    ):
                        original = initial_assessment_rows[index]
                    original = original or {}
                    submitted_rows.append({
                        'pk': raw_id,
                        'name': request.POST.get(
                            f'assessment_{index}_name', ''),
                        'weight': request.POST.get(
                            f'assessment_{index}_weight', ''),
                        'maximum_mark': request.POST.get(
                            f'assessment_{index}_maximum', ''),
                        'index': index,
                        'original_name': original.get('name', ''),
                        'original_weight': original.get('weight', ''),
                        'original_maximum_mark': original.get(
                            'maximum_mark', ''),
                    })
                assessment_rows = submitted_rows
        for learner in learners:
            result = existing_results.get(learner.pk)
            if result:
                prepare_result(result)
            score_cells = []
            for row in assessment_rows:
                raw_id = str(row.get('pk') or '')
                original_value = (
                    (result.assessment_scores or {}).get(raw_id)
                    if result and raw_id else None
                )
                value = original_value
                if request.method == 'POST':
                    value = request.POST.get(
                        f"score_{learner.pk}_{row['index']}", '')
                score_cells.append({
                    'assessment': row,
                    'value': value if value is not None else '',
                    'original_value': (
                        original_value if original_value is not None else ''),
                    'field_name': f"score_{learner.pk}_{row['index']}",
                })
            learner_rows.append({
                'student': learner,
                'result': result,
                'score_cells': score_cells,
                'previous_score': (
                    result.assessment_values[0][1]
                    if result and result.is_previous_record
                    and result.assessment_values else None
                ),
            })

    if request.method == 'POST':
        action = request.POST.get('action') or (
            'remove_assessment'
            if request.POST.get('remove_assessment') else ''
        )
        if not filter_form.is_valid() or not (
            selected_class and selected_subject and selected_period
        ):
            messages.error(
                request,
                'Choose a valid class, subject, and academic period.',
            )
        elif action not in ('save', 'add_test', 'remove_assessment'):
            messages.error(request, 'Choose a valid results action.')
        else:
            posted_rows, errors, assessment_field_errors = (
                _posted_assessment_rows(request, plan, assessment_rows)
            )
            remove_id = request.POST.get('remove_assessment', '')
            if (
                action == 'remove_assessment'
                and len(posted_rows) < 1
            ):
                errors.append('Keep at least one assessment in the setup.')
            scores, mark_field_errors, score_errors = _posted_scores(
                request, posted_rows, learners)
            errors.extend(score_errors)
            if errors or assessment_field_errors or mark_field_errors:
                for error in errors:
                    messages.error(request, error)
            else:
                saved_mark_count = sum(
                    value is not None
                    for learner_scores in scores.values()
                    for value in learner_scores.values()
                )
                with transaction.atomic():
                    if plan is None:
                        plan = AssessmentPlan.objects.create(
                            school_class=selected_class,
                            subject=selected_subject,
                            academic_period=selected_period,
                        )
                    saved_rows = []
                    for position, row in enumerate(posted_rows):
                        assessment = row['assessment'] or Assessment(
                            plan=plan)
                        assessment.plan = plan
                        assessment.name = row['name']
                        assessment.weight = row['weight']
                        assessment.maximum_mark = row['maximum_mark']
                        assessment.position = position
                        assessment.save()
                        saved_rows.append((row, assessment))

                    removed_assessment = None
                    removed_assessment_id = None
                    if remove_id:
                        removed_assessment = plan.assessments.filter(
                            pk=remove_id,
                            is_previous_record=False,
                        ).first()
                        if removed_assessment:
                            removed_assessment_id = removed_assessment.pk
                            removed_assessment.delete()

                    for learner in learners:
                        result = existing_results.get(learner.pk)
                        learner_scores = scores[learner.pk]
                        has_mark = any(
                            value is not None
                            for value in learner_scores.values())
                        if result is None and not has_mark:
                            continue
                        if result is None:
                            result = StudentResult(
                                student=learner,
                                subject=selected_subject,
                                academic_period=selected_period,
                                created_by=(
                                    teacher_profile(request.user)
                                    if request.user.user_type == '3'
                                    else None
                                ),
                            )
                        result.academic_period = selected_period
                        score_map = dict(result.assessment_scores or {})
                        if removed_assessment:
                            score_map.pop(str(removed_assessment.pk), None)
                        for row, assessment in saved_rows:
                            score = learner_scores[row['index']]
                            if score is None:
                                score_map.pop(str(assessment.pk), None)
                            else:
                                score_map[str(assessment.pk)] = str(score)
                        if has_mark:
                            result.is_previous_record = False
                        result.assessment_scores = score_map
                        result.save()

                    if removed_assessment:
                        _remove_assessment_scores(
                            plan, removed_assessment_id)

                    if action == 'add_test':
                        current_assessments = list(
                            plan.assessments.filter(
                                is_previous_record=False).order_by(
                                    'position', 'pk'))
                        test_numbers = [
                            int(match.group(1))
                            for item in current_assessments
                            if (match := re.fullmatch(
                                r'Test\s+(\d+)', item.name.strip(),
                                re.IGNORECASE))
                        ]
                        next_number = max(test_numbers, default=0) + 1
                        examination = next(
                            (
                                item for item in current_assessments
                                if item.name.casefold() == 'examination'
                            ),
                            None,
                        )
                        position = (
                            examination.position
                            if examination else len(current_assessments)
                        )
                        if examination:
                            Assessment.objects.filter(
                                plan=plan,
                                is_previous_record=False,
                                position__gte=position,
                            ).update(position=F('position') + 1)
                        Assessment.objects.create(
                            plan=plan,
                            name=f'Test {next_number}',
                            weight=Decimal('0'),
                            maximum_mark=Decimal('100'),
                            position=position,
                        )
                    messages.success(
                        request,
                        (
                            'Assessment added.'
                            if action == 'add_test'
                            else 'Assessment removed and its marks deleted.'
                            if action == 'remove_assessment'
                            else (
                                f'Saved {saved_mark_count} marks and '
                                'assessment settings.'
                            )
                        ),
                    )
                    query = (
                        f'?school_class={selected_class.pk}'
                        f'&subject={selected_subject.pk}'
                        f'&academic_period={selected_period.pk}'
                    )
                    return redirect(
                        reverse('manage_student_results') + query)

    for row in assessment_rows:
        row['name_error'] = assessment_field_errors.get(
            f'assessment_{row["index"]}_name', '')
        row['weight_error'] = assessment_field_errors.get(
            f'assessment_{row["index"]}_weight', '')
        row['maximum_error'] = assessment_field_errors.get(
            f'assessment_{row["index"]}_maximum', '')
    for learner_row in learner_rows:
        for cell in learner_row['score_cells']:
            cell['error'] = mark_field_errors.get(cell['field_name'], '')

    display_weight_total = Decimal('0')
    for row in assessment_rows:
        try:
            weight = Decimal(str(row['weight']))
        except (InvalidOperation, TypeError, ValueError):
            continue
        if weight.is_finite():
            display_weight_total += weight
    test_rows = [
        row for row in assessment_rows
        if row['name'].strip().casefold() != 'examination'
    ]
    if test_rows:
        test_rows[-1]['is_last_test'] = True

    return render(request, 'hod_template/manage_student_results.html', {
        'filter_form': filter_form,
        'selected_class': selected_class,
        'selected_subject': selected_subject,
        'selected_period': selected_period,
        'assessment_rows': assessment_rows,
        'learner_rows': learner_rows,
        'assessment_total': display_weight_total,
        'grading_scale': [
            {
                'symbol': grade['symbol'],
                'minimum': grade['minimum'],
                'description': grade['description'],
            }
            for grade in GRADING_SCALE
        ],
        'page_title': 'Manage Results',
    })


def edit_student_result(request, result_id):
    result = get_object_or_404(visible_results(request.user), pk=result_id)
    query = (
        f'?school_class={result.student.school_class_id}'
        f'&subject={result.subject_id}'
        f'&academic_period={result.academic_period_id}'
    )
    return redirect(reverse('manage_student_results') + query)


def delete_student_result(request, result_id):
    result = get_object_or_404(
        visible_results(request.user), id=result_id)
    if request.method == 'POST':
        result.delete()
        messages.success(request, 'Learner result deleted successfully.')
    return redirect(reverse('manage_student_results'))


@csrf_exempt
def student_feedback_message(request):
    if request.method != 'POST':
        feedbacks = FeedbackStudent.objects.all()
        context = {
            'feedbacks': feedbacks,
            'page_title': 'Learner Feedback Messages'
        }
        return render(request, 'hod_template/student_feedback_template.html', context)
    else:
        feedback_id = request.POST.get('id')
        try:
            feedback = get_object_or_404(FeedbackStudent, id=feedback_id)
            reply = request.POST.get('reply')
            feedback.reply = reply
            feedback.save()
            return HttpResponse(True)
        except Exception as e:
            return HttpResponse(False)


@csrf_exempt
@csrf_exempt
def view_student_leave(request):
    if request.method != 'POST':
        allLeave = LeaveReportStudent.objects.all()
        context = {
            'allLeave': allLeave,
            'page_title': 'Leave Applications From Learners'
        }
        return render(request, "hod_template/student_leave_view.html", context)
    else:
        id = request.POST.get('id')
        status = request.POST.get('status')
        if (status == '1'):
            status = 1
        else:
            status = -1
        try:
            leave = get_object_or_404(LeaveReportStudent, id=id)
            leave.status = status
            leave.save()
            return HttpResponse(True)
        except Exception as e:
            return False


def admin_view_profile(request):
    custom_user = request.user
    security_question_post = (
        request.method == 'POST'
        and request.POST.get('action') == 'security_question'
    )
    form = ProfileUpdateForm(
        None if security_question_post else request.POST or None,
        request.FILES or None,
        user=custom_user,
    )
    security_form, security_saved = security_question_profile_form(
        request, custom_user)
    context = {'form': form,
               'page_title': 'My Profile',
               'custom_user': custom_user,
               'security_question_form': security_form,
               'security_question_missing': not bool(
                   custom_user.security_question),
               }
    if security_saved:
        return redirect(reverse('admin_view_profile'))
    if (
        request.method == 'POST'
        and not security_question_post
        and form.is_valid()
    ):
        new_password, issued_pin = update_profile_from_form(
            custom_user, form)
        if new_password:
            update_session_auth_hash(request, custom_user)
        if issued_pin:
            return render_pin_confirmation(
                request,
                'admin',
                issued_pin,
                return_url=reverse('admin_view_profile'),
                return_label='Return to profile',
                pin_title='New PIN',
            )
        messages.success(request, 'Profile updated successfully.')
        return redirect(reverse('admin_view_profile'))
    return render(request, "hod_template/admin_view_profile.html", context)


def admin_notify_student(request):
    student = sort_learners(
        CustomUser.objects.filter(user_type=2),
        lambda account: account,
    )
    context = {
        'page_title': "Send Notifications To Learners",
        'students': student
    }
    return render(request, "hod_template/student_notification.html", context)


@csrf_exempt
@login_required
@require_POST
def send_student_notification(request):
    if request.user.user_type != '1':
        return HttpResponseForbidden('Administrator access required.')
    student_id = request.POST.get('id')
    message = (request.POST.get('message') or '').strip()
    if not message:
        return HttpResponse('False', status=400)
    if student_id == 'all':
        students = sort_learners(
            Student.objects.select_related('admin'),
            lambda learner: learner.admin,
        )
    else:
        students = Student.objects.filter(
            admin_id=student_id).select_related('admin')
        get_object_or_404(students, admin_id=student_id)

    failed_pushes = 0
    for student in students:
        NotificationStudent.objects.create(student=student, message=message)
        if not student.admin.fcm_token:
            continue
        url = "https://fcm.googleapis.com/fcm/send"
        body = {
            'notification': {
                'title': "School Information Management System",
                'body': message,
                'click_action': reverse('student_view_notification'),
                'icon': static('dist/img/AdminLTELogo.png')
            },
            'to': student.admin.fcm_token
        }
        headers = {
            'Authorization':
                'key=AAAA3Bm8j_M:APA91bElZlOLetwV696SoEtgzpJr2qbxBfxVBfDWFiopBWzfCfzQp2nRyC7_A2mlukZEHV4g1AmyC6P_HonvSkY2YyliKt5tT3fe_1lrKod2Daigzhb2xnYQMxUWjCAIQcUexAMPZePB',
            'Content-Type': 'application/json',
        }
        try:
            response = requests.post(
                url, data=json.dumps(body), headers=headers, timeout=10)
            response.raise_for_status()
        except requests.RequestException:
            failed_pushes += 1
            logger.exception(
                'Push notification delivery failed for learner %s.',
                student.pk,
            )
    return HttpResponse('Partial' if failed_pushes else 'True')


@csrf_exempt
def delete_student(request, student_id):
    student = get_object_or_404(
        CustomUser.objects.filter(
            student__in=visible_students(request.user)),
        student__id=student_id)
    student.delete()
    messages.success(request, "Learner deleted successfully!")
    return redirect(reverse('manage_student'))


