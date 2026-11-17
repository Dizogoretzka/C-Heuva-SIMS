from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Prefetch
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from .forms import (
    ProfileUpdateForm,
    TeacherClassSetupForm,
    TeacherRequestFilterForm,
    TeacherSubjectSetupForm,
)
from .learner_ordering import sort_learners
from .models import (
    LearnerTeacherRequest,
    Student,
    Subject,
    Teacher,
    TeacherClassSetup,
    school_class_from_label,
)
from .scoping import visible_students, visible_subjects
from .teacher_requests import create_requests_for_class
from .views import render_pin_confirmation, update_profile_from_form
from .security_reset import security_question_profile_form


@login_required
def teacher_home(request):
    return render(request, 'hod_template/home_content.html', {
        'page_title': 'Teacher Dashboard',
        'security_question_missing': not bool(
            request.user.security_question),
        'total_students': visible_students(request.user).count(),
        'total_subject': visible_subjects(request.user).count(),
        'is_teacher': True,
    })


@login_required
def teacher_profile(request):
    user = request.user
    security_question_post = (
        request.method == 'POST'
        and request.POST.get('action') == 'security_question'
    )
    form = ProfileUpdateForm(
        None if security_question_post else request.POST or None,
        request.FILES or None,
        user=user,
    )
    security_form, security_saved = security_question_profile_form(
        request, user)
    if security_saved:
        return redirect(reverse('teacher_profile'))
    if (
        request.method == 'POST'
        and not security_question_post
        and form.is_valid()
    ):
        new_password, issued_pin = update_profile_from_form(user, form)
        if new_password or issued_pin:
            update_session_auth_hash(request, user)
        if issued_pin:
            return render_pin_confirmation(
                request,
                'teacher',
                issued_pin,
                return_url=reverse('teacher_profile'),
                return_label='Return to profile',
                pin_title='New PIN',
            )
        messages.success(request, 'Profile updated successfully.')
        return redirect(reverse('teacher_profile'))

    return render(request, 'hod_template/admin_view_profile.html', {
        'form': form,
        'custom_user': user,
        'page_title': 'My Profile',
        'security_question_form': security_form,
        'security_question_missing': not bool(user.security_question),
    })


@login_required
def teacher_classes_subjects(request):
    teacher = get_object_or_404(Teacher, teacher=request.user)
    class_form = TeacherClassSetupForm()
    subject_form = TeacherSubjectSetupForm(user=request.user)

    if request.method == 'POST' and request.POST.get('action') == 'add_class':
        class_form = TeacherClassSetupForm(request.POST)
        if class_form.is_valid():
            school_class = school_class_from_label(
                class_form.cleaned_data['grade_and_class'])
            with transaction.atomic():
                _, created = TeacherClassSetup.objects.get_or_create(
                    teacher=teacher,
                    school_class=school_class,
                )
                if created:
                    create_requests_for_class(school_class)
            messages.success(request, 'Class setup saved.')
            return redirect(reverse('teacher_classes_subjects'))

    if request.method == 'POST' and request.POST.get('action') == 'add_subject':
        subject_form = TeacherSubjectSetupForm(
            request.POST, user=request.user)
        if subject_form.is_valid():
            school_class = subject_form.cleaned_data['school_class']
            if not TeacherClassSetup.objects.filter(
                teacher=teacher, school_class=school_class,
            ).exists():
                subject_form.add_error(
                    'school_class', 'Choose one of your configured classes.')
            else:
                subject_name = subject_form.cleaned_data['name'].strip()
                if Subject.objects.filter(
                    teacher=teacher,
                    school_class=school_class,
                    name__iexact=subject_name,
                ).exists():
                    subject_form.add_error(
                        'name',
                        'You have already added this subject for the class.')
                else:
                    Subject.objects.create(
                        name=subject_name,
                        teacher=teacher,
                        school_class=school_class,
                        created_by=teacher,
                    )
                    messages.success(request, 'Subject setup saved.')
                    return redirect(reverse('teacher_classes_subjects'))

    setups = TeacherClassSetup.objects.filter(
        teacher=teacher).select_related('school_class').prefetch_related(
            Prefetch(
                'school_class__subject_set',
                queryset=Subject.objects.filter(teacher=teacher).order_by(
                    'name'),
                to_attr='setup_subjects',
            ))
    return render(request, 'teacher_template/classes_subjects.html', {
        'setups': setups,
        'class_form': class_form,
        'subject_form': subject_form,
        'page_title': 'My Classes and Subjects',
    })


@login_required
def learner_requests(request):
    teacher = get_object_or_404(Teacher, teacher=request.user)
    filter_form = TeacherRequestFilterForm(request.GET, user=request.user)
    school_class = None
    if filter_form.is_valid():
        school_class = filter_form.cleaned_data.get('school_class')

    pending_requests = LearnerTeacherRequest.objects.filter(
        teacher=teacher,
        status=LearnerTeacherRequest.PENDING,
    ).select_related(
        'student__admin', 'student__school_class', 'school_class')
    if school_class:
        pending_requests = pending_requests.filter(
            school_class=school_class)

    if request.method == 'POST':
        post_filter = TeacherRequestFilterForm(
            request.POST, user=request.user)
        if post_filter.is_valid():
            school_class = post_filter.cleaned_data.get('school_class')
            pending_requests = LearnerTeacherRequest.objects.filter(
                teacher=teacher,
                status=LearnerTeacherRequest.PENDING,
            )
            if school_class:
                pending_requests = pending_requests.filter(
                    school_class=school_class)
            action = request.POST.get('action')
            if action in ('accept', 'decline'):
                pending_requests = pending_requests.filter(
                    pk=request.POST.get('request_id'))
            if action in ('accept', 'decline', 'accept_all', 'decline_all'):
                affected_count = pending_requests.count()
                if not affected_count:
                    messages.error(request, 'No pending requests matched.')
                    target = reverse('learner_requests')
                    if school_class:
                        target = f'{target}?school_class={school_class.pk}'
                    return redirect(target)
                status = (
                    LearnerTeacherRequest.ACCEPTED
                    if action in ('accept', 'accept_all')
                    else LearnerTeacherRequest.DECLINED
                )
                responded_at = timezone.now()
                with transaction.atomic():
                    for learner_request in pending_requests:
                        learner_request.status = status
                        learner_request.responded_at = responded_at
                        learner_request.save(
                            update_fields=['status', 'responded_at'])
                messages.success(
                    request,
                    f'{affected_count} learner request(s) updated.',
                )
                target = reverse('learner_requests')
                if school_class:
                    target = f'{target}?school_class={school_class.pk}'
                return redirect(target)
        messages.error(request, 'Choose a valid request action and class.')

    pending_count = pending_requests.count()
    pending_requests = sort_learners(
        pending_requests.select_related(
            'student__admin', 'student__school_class', 'school_class'),
        lambda learner_request: learner_request.student.admin,
    )
    return render(request, 'teacher_template/learner_requests.html', {
        'requests': pending_requests,
        'filter_form': filter_form,
        'pending_count': pending_count,
        'page_title': 'Learner Requests',
    })


