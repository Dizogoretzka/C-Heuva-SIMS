import json

from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.core.files.storage import FileSystemStorage
from django.db.models import ExpressionWrapper, F, FloatField, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt

from .forms import *
from .grading import get_grade_band
from .models import *
from .views import render_pin_confirmation, update_profile_from_form


def student_home(request):
    student = get_object_or_404(Student, admin=request.user)
    report_card_available = bool(
        student.session
        and StudentResult.objects.filter(
            student=student,
            student__session=student.session,
        ).exists()
    )
    context = {
        'total_subject': Subject.objects.count(),
        'student_result_count': StudentResult.objects.filter(
            student=student).count(),
        'report_card_available': report_card_available,
        'page_title': 'Learner Homepage'
    }
    return render(request, 'student_template/home_content.html', context)


def student_view_subjects(request):
    student = get_object_or_404(Student, admin=request.user)
    subjects = Subject.objects.filter(
        Q(course__isnull=True) | Q(course_id=student.course_id)
    ).order_by('name')
    context = {
        'subjects': subjects,
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
    )
    context = {'form': form,
               'page_title': 'My Profile',
               'custom_user': custom_user,
               'student': student,
               }
    if request.method == 'POST' and form.is_valid():
        new_password, issued_pin = update_profile_from_form(
            custom_user, form)
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
    results = list(StudentResult.objects.filter(student=student).select_related(
        'subject').annotate(
        total_score=ExpressionWrapper(
            F('test') + F('exam'), output_field=FloatField())))
    grade_bands = list(GradeBand.objects.order_by('minimum_score'))
    for result in results:
        result.grade_band = get_grade_band(result.total_score, grade_bands)
        result.grade_symbol = (
            result.grade_band.symbol
            if isinstance(result.grade_band, GradeBand)
            else result.grade_band
        )
    has_report_card = bool(student.session and results)
    context = {
        'results': results,
        'student': student,
        'has_report_card': has_report_card,
        'page_title': "View Results"
    }
    return render(request, "student_template/student_view_result.html", context)


def student_report_card(request):
    student = get_object_or_404(Student, admin=request.user)
    if not student.session:
        messages.warning(request, 'No session / term is assigned to your profile.')
        return redirect(reverse('student_view_result'))
    from .hod_views import _report_card_pdf

    response = _report_card_pdf(request.user, student, student.session)
    if response is None:
        messages.warning(
            request, 'There are no results for your assigned session / term.')
        return redirect(reverse('student_view_result'))
    return response


def student_attendance(request):
    student = get_object_or_404(Student, admin=request.user)
    records = AttendanceReport.objects.filter(
        student=student).select_related(
            'attendance__course',
            'attendance__subject',
            'attendance__session',
        ).order_by('-attendance__date')
    total = records.count()
    present = records.filter(status=True).count()
    context = {
        'records': records,
        'total_marked': total,
        'present_count': present,
        'attendance_percentage': (present / total * 100) if total else 0,
        'page_title': 'My Attendance',
    }
    return render(request, 'student_template/student_attendance.html', context)
