import json

from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.core.files.storage import FileSystemStorage
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt

from .forms import *
from .grading import (
    prepare_result,
)
from .models import *
from .models import school_class_from_label
from .views import render_pin_confirmation, update_profile_from_form
from .teacher_requests import create_class_requests, reset_class_requests


def _learner_relationships(student):
    requests = list(
        student.teacher_requests.select_related(
            'teacher__teacher', 'school_class').order_by('teacher__teacher__last_name')
    )
    accepted = [
        item for item in requests
        if item.status == LearnerTeacherRequest.ACCEPTED
    ]
    if accepted:
        names = [
            item.teacher.teacher.get_full_name().strip()
            or item.teacher.teacher.get_username()
            or 'Teacher'
            for item in accepted
        ]
        status = f'Accepted by {", ".join(names)}'
    elif any(
        item.status == LearnerTeacherRequest.PENDING for item in requests
    ) or not requests or all(
        item.status == LearnerTeacherRequest.CANCELLED for item in requests
    ):
        status = 'Waiting for a teacher'
    else:
        status = 'Not accepted'
    return requests, accepted, status


def _learner_subjects(student, accepted_requests):
    teacher_ids = [item.teacher_id for item in accepted_requests]
    return Subject.objects.filter(
        school_class_id=student.school_class_id,
        teacher_id__in=teacher_ids,
    )


def student_home(request):
    student = get_object_or_404(Student, admin=request.user)
    _, accepted_requests, relationship_status = _learner_relationships(student)
    available_subjects = _learner_subjects(student, accepted_requests)
    available_results = StudentResult.objects.filter(
        student=student,
        subject__in=available_subjects,
    )
    report_card_available = bool(
        student.academic_period
        and available_results.filter(
            academic_period=student.academic_period).exists()
    )
    context = {
        'total_subject': available_subjects.count(),
        'student_result_count': available_results.count(),
        'report_card_available': report_card_available,
        'relationship_status': relationship_status,
        'page_title': 'Learner Homepage'
    }
    return render(request, 'student_template/home_content.html', context)


def student_view_subjects(request):
    student = get_object_or_404(Student, admin=request.user)
    _, accepted_requests, relationship_status = _learner_relationships(student)
    subjects = _learner_subjects(student, accepted_requests).select_related(
        'teacher__teacher').order_by('name')
    context = {
        'subjects': subjects,
        'relationship_status': relationship_status,
        'page_title': 'My Subjects',
    }
    return render(request, 'student_template/student_view_subjects.html', context)


def student_apply_leave(request):
    form = LeaveReportStudentForm(request.POST or None)
    student = get_object_or_404(Student, admin_id=request.user.id)
    context = {
        'form': form,
        'leave_history': LeaveReportStudent.objects.filter(student=student),
        'page_title': 'Apply for leave'
    }
    if request.method == 'POST':
        if form.is_valid():
            try:
                obj = form.save(commit=False)
                obj.student = student
                obj.save()
                messages.success(
                    request, "Application for leave has been submitted for review")
                return redirect(reverse('student_apply_leave'))
            except Exception:
                messages.error(request, "Could not submit")
        else:
            messages.error(request, "Form has errors!")
    return render(request, "student_template/student_apply_leave.html", context)


def student_feedback(request):
    form = FeedbackStudentForm(request.POST or None)
    student = get_object_or_404(Student, admin_id=request.user.id)
    context = {
        'form': form,
        'feedbacks': FeedbackStudent.objects.filter(student=student),
        'page_title': 'Learner Feedback'

    }
    if request.method == 'POST':
        if form.is_valid():
            try:
                obj = form.save(commit=False)
                obj.student = student
                obj.save()
                messages.success(
                    request, "Feedback submitted for review")
                return redirect(reverse('student_feedback'))
            except Exception:
                messages.error(request, "Could not Submit!")
        else:
            messages.error(request, "Form has errors!")
    return render(request, "student_template/student_feedback.html", context)


def student_view_profile(request):
    student = get_object_or_404(Student, admin=request.user)
    custom_user = student.admin
    form = ProfileUpdateForm(
        request.POST or None,
        request.FILES or None,
        user=custom_user,
        student=student,
    )
    context = {'form': form,
               'page_title': 'My Profile',
               'custom_user': custom_user,
               'student': student,
               }
    if request.method == 'POST' and form.is_valid():
        with transaction.atomic():
            new_password, issued_pin = update_profile_from_form(
                custom_user, form)
            new_school_class = school_class_from_label(
                form.cleaned_data['grade_and_class'])
            class_changed = student.school_class_id != getattr(
                new_school_class, 'pk', None)
            if class_changed:
                reset_class_requests(student)
            student.school_class = new_school_class
            student.save(update_fields=['school_class', 'updated_at'])
            if class_changed:
                create_class_requests(student)
        if new_password:
            update_session_auth_hash(request, custom_user)
        if issued_pin:
            return render_pin_confirmation(
                request,
                'learner',
                issued_pin,
                return_url=reverse('student_view_profile'),
                return_label='Return to profile',
                pin_title='New PIN',
            )
        messages.success(request, 'Profile updated successfully.')
        return redirect(reverse('student_view_profile'))

    return render(request, "student_template/student_view_profile.html", context)


@csrf_exempt
def student_fcmtoken(request):
    token = request.POST.get('token')
    student_user = get_object_or_404(CustomUser, id=request.user.id)
    try:
        student_user.fcm_token = token
        student_user.save()
        return HttpResponse("True")
    except Exception as e:
        return HttpResponse("False")


def student_view_notification(request):
    student = get_object_or_404(Student, admin=request.user)
    notifications = NotificationStudent.objects.filter(student=student)
    context = {
        'notifications': notifications,
        'page_title': "View Notifications"
    }
    return render(request, "student_template/student_view_notification.html", context)


def student_view_result(request):
    student = get_object_or_404(Student, admin=request.user)
    _, accepted_requests, relationship_status = _learner_relationships(student)
    available_subjects = _learner_subjects(student, accepted_requests)
    results = list(StudentResult.objects.filter(
        student=student,
        subject__in=available_subjects,
    ).select_related(
            'subject', 'academic_period', 'student__academic_period'))
    for result in results:
        prepare_result(result)
    has_report_card = bool(student.academic_period and results)
    context = {
        'results': results,
        'student': student,
        'relationship_status': relationship_status,
        'has_report_card': has_report_card,
        'assigned_report_cards': ReportCardPublication.objects.filter(
            student=student).select_related(
                'teacher__teacher', 'academic_period'),
        'page_title': "View Results"
    }
    return render(request, "student_template/student_view_result.html", context)


def student_report_card(request):
    student = get_object_or_404(Student, admin=request.user)
    if not student.academic_period:
        messages.warning(request, 'No academic period is assigned to your profile.')
        return redirect(reverse('student_view_result'))
    from .hod_views import _report_card_pdf

    export_format = request.GET.get('export', 'pdf')
    if export_format not in ('pdf', 'csv'):
        return HttpResponse('Unsupported export format.', status=400)
    response = _report_card_pdf(
        request,
        request.user,
        student,
        student.academic_period,
        export_format=export_format,
    )
    if response is None:
        messages.warning(
            request, 'There are no results for your assigned academic period.')
        return redirect(reverse('student_view_result'))
    return response


@login_required
def student_published_report_card(request, publication_id):
    if request.user.user_type != '2':
        return redirect(reverse('login_page'))
    publication = get_object_or_404(
        ReportCardPublication.objects.select_related(
            'teacher__teacher', 'student__admin', 'academic_period'),
        pk=publication_id,
        student__admin=request.user,
    )
    export_format = request.GET.get('export', 'pdf')
    if export_format not in ('pdf', 'csv'):
        return HttpResponse('Unsupported export format.', status=400)

    from .hod_views import _report_card_pdf

    response = _report_card_pdf(
        request,
        publication.teacher.teacher,
        publication.student,
        publication.academic_period,
        export_format=export_format,
        actor_user=request.user,
    )
    if response is None:
        messages.warning(
            request, 'The published report card no longer has reportable results.')
        return redirect(reverse('student_view_result'))
    return response


