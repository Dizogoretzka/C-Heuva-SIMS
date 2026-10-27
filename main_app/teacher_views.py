from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from .forms import AttendanceSelectionForm, ProfileUpdateForm
from .models import Attendance, AttendanceReport, Student, Subject
from .scoping import visible_students, visible_subjects
from .views import render_pin_confirmation, update_profile_from_form


@login_required
def teacher_home(request):
    return render(request, 'hod_template/home_content.html', {
        'page_title': 'Teacher Dashboard',
        'total_students': visible_students(request.user).count(),
        'total_subject': visible_subjects(request.user).count(),
        'is_teacher': True,
    })


@login_required
def teacher_profile(request):
    user = request.user
    form = ProfileUpdateForm(
        request.POST or None,
        request.FILES or None,
        user=user,
    )
    if request.method == 'POST' and form.is_valid():
        new_password, issued_pin = update_profile_from_form(user, form)
        if new_password:
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
    })


@login_required
def teacher_attendance(request):
    selection_data = (
        request.POST
        if request.method == 'POST'
        else request.GET or None
    )
    selection_form = AttendanceSelectionForm(
        selection_data, user=request.user)
    attendance = None
    roster = []
    statuses = {}
    selection_loaded = False
    selection_details = {}

    if selection_form.is_valid():
        selection_loaded = True
        course = selection_form.cleaned_data['course']
        subject = selection_form.cleaned_data['subject']
        academic_period = selection_form.cleaned_data['academic_period']
        date = selection_form.cleaned_data['date']
        selection_details = {
            'course': course,
            'subject': subject,
            'academic_period': academic_period,
            'date': date,
        }
        attendance = Attendance.objects.filter(
            course=course,
            subject=subject,
            academic_period=academic_period,
            date=date,
        ).filter(
            Q(subject__teacher__teacher=request.user)
            | Q(subject__teacher__isnull=True)
        ).first()
        roster = list(visible_students(request.user).filter(
            course=course,
            academic_period=academic_period,
        ).select_related('admin').order_by(
            'admin__last_name', 'admin__first_name'))
        if attendance:
            statuses = {
                record.student_id: record.status
                for record in AttendanceReport.objects.filter(
                    attendance=attendance,
                    student__in=roster,
                )
            }

        if request.method == 'POST':
            submitted_statuses = {}
            submitted_values = {}
            for learner in roster:
                value = request.POST.get(f'attendance_{learner.pk}')
                submitted_values[learner.pk] = value
                if value not in ('present', 'absent'):
                    selection_form.add_error(
                        None, 'Select present or absent for every learner.')
                    break
                submitted_statuses[learner.pk] = value == 'present'

            if not selection_form.errors:
                with transaction.atomic():
                    attendance, _ = Attendance.objects.get_or_create(
                        course=course,
                        subject=subject,
                        academic_period=academic_period,
                        date=date,
                        defaults={
                            'created_by_id': request.user.teacher.pk,
                        },
                    )
                    for learner in roster:
                        attendance_report, created = (
                            AttendanceReport.objects.get_or_create(
                                attendance=attendance,
                                student=learner,
                                defaults={
                                    'status': submitted_statuses[learner.pk],
                                    'created_by_id': request.user.teacher.pk,
                                },
                            ))
                        if not created:
                            attendance_report.status = (
                                submitted_statuses[learner.pk])
                            attendance_report.save(update_fields=['status'])
                messages.success(request, 'Attendance saved successfully.')
                return redirect(
                    f'{reverse("teacher_attendance")}?course={course.pk}'
                    f'&subject={subject.pk}&academic_period={academic_period.pk}&date={date}'
                )

            statuses = submitted_values

    for learner in roster:
        value = statuses.get(learner.pk)
        learner.attendance_choice = (
            'present' if value is True or value == 'present'
            else 'absent' if value is False or value == 'absent'
            else ''
        )

    return render(request, 'hod_template/teacher_attendance.html', {
        'form': selection_form,
        'roster': roster,
        'statuses': statuses,
        'attendance': attendance,
        'selection_loaded': selection_loaded,
        'selection_details': selection_details,
        'page_title': 'Attendance',
    })
