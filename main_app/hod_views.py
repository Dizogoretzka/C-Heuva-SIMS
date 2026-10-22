import json
import logging
from datetime import date
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape

import requests
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth import update_session_auth_hash
from django.core.files.storage import FileSystemStorage
from django.core.paginator import Paginator
from django.contrib.staticfiles import finders
from django.db import transaction
from django.db.models import Count, ExpressionWrapper, F, FloatField, Q
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.templatetags.static import static
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .forms import *
from .credentials import issue_pin
from .grading import get_grade_band
from .models import *
from .views import render_pin_confirmation, update_profile_from_form

logger = logging.getLogger(__name__)


def admin_home(request):
    total_students = Student.objects.all().count()
    recent_logins = LoginActivity.objects.select_related(
        'student__admin', 'student__course'
    ).order_by('-logged_in_at')[:5]
    context = {
        'page_title': "Administrative Dashboard",
        'total_students': total_students,
        'total_subject': Subject.objects.count(),
        'total_grade_bands': GradeBand.objects.count(),
        'learners_logged_in_today': LoginActivity.objects.filter(
            logged_in_at__date=timezone.localdate()
        ).values('student_id').distinct().count(),
        'recent_logins': recent_logins,
    }
    return render(request, 'hod_template/home_content.html', context)


@login_required
def learner_login_activity(request):
    if request.user.user_type != '1':
        return redirect(reverse('student_home'))

    filter_form = LoginActivityFilterForm(request.GET)
    activities = LoginActivity.objects.select_related(
        'student__admin', 'student__course'
    ).order_by('-logged_in_at')
    if filter_form.is_valid():
        learner = filter_form.cleaned_data.get('learner')
        login_date = filter_form.cleaned_data.get('date')
        course = filter_form.cleaned_data.get('course')
        if learner:
            activities = activities.filter(
                Q(student__admin__first_name__icontains=learner)
                | Q(student__admin__last_name__icontains=learner)
                | Q(student__admin__username__icontains=learner)
            )
        if login_date:
            activities = activities.filter(logged_in_at__date=login_date)
        if course:
            activities = activities.filter(student__course=course)

    paginator = Paginator(activities, 25)
    page_obj = paginator.get_page(request.GET.get('page'))
    query_params = request.GET.copy()
    query_params.pop('page', None)
    context = {
        'page_title': 'Learner Login Activity',
        'filter_form': filter_form,
        'page_obj': page_obj,
        'query_params': query_params.urlencode(),
    }
    return render(request, 'hod_template/learner_login_activity.html', context)


def manage_teachers(request):
    teachers = Teacher.objects.select_related('teacher').order_by(
        'teacher__last_name', 'teacher__first_name')
    return render(request, 'hod_template/manage_teachers.html', {
        'teachers': teachers,
        'page_title': 'Manage Teachers',
    })


@login_required
def reset_user_credentials(request, user_id):
    account = get_object_or_404(
        CustomUser.objects.filter(user_type__in=('2', '3')),
        pk=user_id,
    )
    if request.user.user_type == '1':
        allowed = account.user_type in ('2', '3')
    elif request.user.user_type == '3':
        allowed = account.user_type == '2'
    else:
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
                'manage_teachers' if account.user_type == '3'
                else 'manage_student'),
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
                username=data['username'],
                email='',
                password=data['password'],
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
        user.username = data['username']
        user.gender = data['gender']
        if data.get('profile_pic'):
            user.profile_pic = data['profile_pic']
        if data.get('password'):
            user.set_password(data['password'])
            user.failed_login_attempts = 0
            user.locked_until = None
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


def attendance_records(request):
    records = Attendance.objects.select_related(
        'course', 'subject', 'session').annotate(
            present_count=Count(
                'records',
                filter=Q(records__status=True),
            ),
            absent_count=Count(
                'records',
                filter=Q(records__status=False),
            ),
        ).order_by('-date', 'course__name', 'subject__name')
    page_obj = Paginator(records, 25).get_page(request.GET.get('page'))
    return render(request, 'hod_template/attendance_records.html', {
        'page_obj': page_obj,
        'page_title': 'Attendance Records',
    })


@login_required
def generate_report(request):
    if request.user.user_type not in ('1', '3'):
        return redirect(reverse('admin_home'))

    role = 'teacher' if request.user.user_type == '3' else 'admin'
    form = ReportGenerationForm(request.POST or None, role=role)
    context = {'form': form, 'page_title': 'Generate Report'}
    if request.method != 'POST' or not form.is_valid():
        return render(request, 'hod_template/generate_report.html', context)

    report_type = form.cleaned_data['report_type']
    course = form.cleaned_data.get('course')
    session = form.cleaned_data.get('session')
    subject = form.cleaned_data.get('subject')

    title, headers, rows = _report_data(report_type, course, session, subject)
    if not rows:
        messages.warning(
            request, 'There is no data for the selected report options.')
        return render(request, 'hod_template/generate_report.html', context)

    return _create_report_pdf(request.user, title, headers, rows)


@login_required
def manage_grade_bands(request):
    if request.user.user_type != '3':
        return redirect(reverse('admin_home'))

    form = GradeBandForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, 'Grade band added successfully.')
        return redirect(reverse('manage_grade_bands'))

    bands = list(GradeBand.objects.order_by('minimum_score', 'maximum_score'))
    gaps = [
        (bands[index - 1].maximum_score, bands[index].minimum_score)
        for index in range(1, len(bands))
        if bands[index].minimum_score > bands[index - 1].maximum_score
    ]
    return render(request, 'hod_template/manage_grade_bands.html', {
        'form': form,
        'bands': bands,
        'gaps': gaps,
        'page_title': 'Grade Bands',
    })


@login_required
def edit_grade_band(request, band_id):
    if request.user.user_type != '3':
        return redirect(reverse('admin_home'))

    band = get_object_or_404(GradeBand, pk=band_id)
    form = GradeBandForm(request.POST or None, instance=band)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, 'Grade band updated successfully.')
        return redirect(reverse('manage_grade_bands'))
    return render(request, 'hod_template/edit_grade_band.html', {
        'form': form,
        'band': band,
        'page_title': 'Edit Grade Band',
    })


@login_required
def delete_grade_band(request, band_id):
    if request.user.user_type != '3':
        return redirect(reverse('admin_home'))

    band = get_object_or_404(GradeBand, pk=band_id)
    if request.method == 'POST':
        band.delete()
        messages.success(request, 'Grade band deleted successfully.')
    return redirect(reverse('manage_grade_bands'))


@login_required
def generate_report_card(request):
    if request.user.user_type != '3':
        return redirect(reverse('admin_home'))

    form = ReportCardForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        response = _report_card_pdf(
            request.user,
            form.cleaned_data['student'],
            form.cleaned_data['session'],
        )
        if response is not None:
            return response
        messages.warning(
            request, 'There are no results for this learner and session / term.')

    return render(request, 'hod_template/generate_report_card.html', {
        'form': form,
        'page_title': 'Generate Learner Report Card',
    })


def _report_card_pdf(user, student, session):
    results = list(StudentResult.objects.filter(
        student=student,
        student__session=session,
    ).select_related('subject').order_by('subject__name'))
    if not results:
        return None

    grade_bands = list(GradeBand.objects.order_by('minimum_score'))
    rows = []
    totals = []
    for result in results:
        total = result.test + result.exam
        band = get_grade_band(total, grade_bands)
        totals.append(total)
        rows.append([
            result.subject.name,
            f'{result.test:g}',
            f'{result.exam:g}',
            f'{total:g}',
            band.symbol if isinstance(band, GradeBand) else band,
            band.description if isinstance(band, GradeBand) else '',
            band.remark if isinstance(band, GradeBand) else '',
        ])

    return _create_report_pdf(
        user,
        'Learner Report Card',
        [
            'Subject', 'Test / CA', 'Examination', 'Total',
            'Grade', 'Description', 'Band remark',
        ],
        rows,
        filename='learner-report-card.pdf',
        preamble=[
            f'<b>Learner:</b> {escape(student.admin.get_full_name().strip() or student.admin.username)}',
            f'<b>Course / grade:</b> {escape(str(student.course) if student.course else "Not assigned")}',
            f'<b>Session / term:</b> {escape(str(session))}',
        ],
        postamble=[
            f'<b>Overall average total:</b> {sum(totals) / len(totals):.2f}',
            '<b>Class teacher remarks:</b>',
            '________________________________________________________________________________',
            '<b>Principal remarks:</b>',
            '________________________________________________________________________________',
        ],
    )


def _report_data(report_type, course, session, subject):
    if report_type == 'school_summary':
        courses = Course.objects.annotate(
            learner_count=Count('student')).order_by('name')
        course_rows = [[course.name, str(course.learner_count)]
                       for course in courses]
        unassigned_count = Student.objects.filter(course__isnull=True).count()
        if unassigned_count:
            course_rows.append(
                ['Not assigned to a course', str(unassigned_count)])
        if not Student.objects.exists() and not Course.objects.exists() and not Subject.objects.exists():
            return 'School Summary', [], []
        rows = [
            ['Total learners', str(Student.objects.count())],
            ['Total courses / grades', str(Course.objects.count())],
            ['Total subjects', str(Subject.objects.count())],
            *course_rows,
        ]
        return 'School Summary', ['Metric / course / grade', 'Count'], rows

    if report_type == 'class_list':
        learners = Student.objects.filter(
            course=course, session=session
        ).select_related('admin').order_by('admin__last_name', 'admin__first_name')
        rows = [[f'{learner.admin.first_name} {learner.admin.last_name}',
                 learner.admin.username] for learner in learners]
        return f'Class List - {course.name} - {session}', [
            'Learner name', 'Username'], rows

    if report_type == 'learner_results':
        results = StudentResult.objects.filter(
            student__course=course,
            student__session=session,
            subject__course=course,
        ).select_related('student__admin', 'subject').order_by(
            'student__admin__last_name', 'student__admin__first_name', 'subject__name')
        if subject:
            results = results.filter(subject=subject)
        rows = [[
            f'{result.student.admin.first_name} {result.student.admin.last_name}',
            result.subject.name,
            f'{result.test:g}',
            f'{result.exam:g}',
            f'{result.test + result.exam:g}',
        ] for result in results]
        return f'Learner Results - {course.name} - {session}', [
            'Learner name', 'Subject', 'Test / CA', 'Exam', 'Total'], rows

    if report_type == 'attendance_summary':
        filters = Q(
            attendance_records__attendance__course=course,
            attendance_records__attendance__session=session,
        )
        if subject:
            filters &= Q(
                attendance_records__attendance__subject=subject)
        learners = Student.objects.filter(
            course=course,
            session=session,
        ).select_related('admin').annotate(
            marked_count=Count('attendance_records', filter=filters),
            present_count=Count(
                'attendance_records',
                filter=filters & Q(attendance_records__status=True),
            ),
        ).order_by('admin__last_name', 'admin__first_name')
        rows = []
        for learner in learners:
            if not learner.marked_count:
                continue
            absent_count = learner.marked_count - learner.present_count
            percentage = learner.present_count / learner.marked_count * 100
            rows.append([
                learner.admin.get_full_name() or learner.admin.username,
                str(learner.present_count),
                str(absent_count),
                f'{percentage:.1f}%',
            ])
        return f'Attendance Summary - {course.name} - {session}', [
            'Learner', 'Present', 'Absent', 'Attendance'], rows

    raise ValueError(f'Unsupported report type: {report_type}')

def _create_report_pdf(
        user, title, headers, rows, filename='school-report.pdf',
        preamble=None, postamble=None):
    buffer = BytesIO()
    generated_date = date.today().strftime('%B %d, %Y')
    display_name = user.get_full_name().strip() or user.username
    styles = getSampleStyleSheet()
    cell_style = ParagraphStyle(
        'ReportCell', parent=styles['BodyText'], fontName='Helvetica',
        fontSize=8, leading=10, spaceAfter=0)
    header_style = ParagraphStyle(
        'ReportHeader', parent=cell_style, fontName='Helvetica-Bold',
        textColor=colors.white)
    report_title = Paragraph(escape(title), styles['Heading2'])
    body = [report_title, Spacer(1, 5 * mm)]
    body.extend(
        Paragraph(line, cell_style) for line in (preamble or []))
    if preamble:
        body.append(Spacer(1, 3 * mm))
    table_data = [[Paragraph(escape(str(value)), header_style) for value in headers]]
    table_data.extend([
        [Paragraph(escape(str(value)), cell_style) for value in row]
        for row in rows
    ])
    available_width = A4[0] - 32 * mm
    if len(headers) == 2:
        column_widths = [available_width * 0.66, available_width * 0.34]
    elif len(headers) == 3:
        column_widths = [available_width * 0.56, available_width * 0.22,
                         available_width * 0.22]
    elif len(headers) == 4:
        column_widths = [available_width * 0.40, available_width * 0.20,
                         available_width * 0.20, available_width * 0.20]
    elif len(headers) == 6:
        column_widths = [available_width * 0.25, available_width * 0.14,
                         available_width * 0.14, available_width * 0.12,
                         available_width * 0.10, available_width * 0.25]
    elif len(headers) == 7:
        column_widths = [available_width * 0.20, available_width * 0.12,
                         available_width * 0.12, available_width * 0.10,
                         available_width * 0.08, available_width * 0.18,
                         available_width * 0.20]
    else:
        column_widths = [available_width * 0.32, available_width * 0.25,
                         available_width * 0.15, available_width * 0.14,
                         available_width * 0.14]
    table = Table(
        table_data, colWidths=column_widths, repeatRows=1, hAlign='LEFT')
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
    body.append(table)
    body.extend(
        Paragraph(line, cell_style) for line in (postamble or []))
    document = SimpleDocTemplate(
        buffer, pagesize=A4, rightMargin=16 * mm, leftMargin=16 * mm,
        topMargin=42 * mm, bottomMargin=22 * mm,
        title=title, author=display_name)
    crest_path = _report_crest_path()

    def draw_page(canvas, doc):
        canvas.saveState()
        page_width, page_height = A4
        if doc.page == 1 and crest_path:
            try:
                canvas.drawImage(
                    crest_path,
                    16 * mm,
                    page_height - 27 * mm,
                    width=20 * mm,
                    height=20 * mm,
                    preserveAspectRatio=True,
                    anchor='c',
                    mask='auto',
                )
            except (OSError, ValueError) as error:
                logger.warning('Could not embed report crest at %s: %s',
                               crest_path, error)
        canvas.setFillColor(colors.HexColor('#243b53'))
        canvas.setFont('Helvetica-Bold', 13)
        canvas.drawCentredString(
            page_width / 2, page_height - 15 * mm,
            'C. Heuva Junior Secondary School')
        canvas.setStrokeColor(colors.HexColor('#cbd2d9'))
        canvas.line(16 * mm, page_height - 29 * mm,
                    page_width - 16 * mm, page_height - 29 * mm)
        canvas.setFont('Helvetica', 8)
        canvas.setFillColor(colors.HexColor('#52606d'))
        canvas.drawString(16 * mm, page_height - 35 * mm,
                          f'Date generated: {generated_date}')
        canvas.line(16 * mm, 16 * mm, page_width - 16 * mm, 16 * mm)
        canvas.drawString(16 * mm, 10 * mm, f'Generated by {display_name}')
        canvas.drawRightString(
            page_width - 16 * mm, 10 * mm, f'Page {doc.page}')
        canvas.restoreState()

    document.build(body, onFirstPage=draw_page, onLaterPages=draw_page)
    response = HttpResponse(buffer.getvalue(), content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


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
    student_form.fields.pop('course')
    student_form.fields.pop('session')
    context = {'form': student_form, 'page_title': 'Add Learner'}
    if request.method == 'POST':
        if student_form.is_valid():
            first_name = student_form.cleaned_data.get('first_name')
            last_name = student_form.cleaned_data.get('last_name')
            address = student_form.cleaned_data.get('address')
            username = student_form.cleaned_data.get('username')
            gender = student_form.cleaned_data.get('gender')
            password = student_form.cleaned_data.get('password')
            passport = request.FILES['profile_pic']
            fs = FileSystemStorage()
            filename = fs.save(passport.name, passport)
            passport_url = fs.url(filename)
            with transaction.atomic():
                user = CustomUser.objects.create_user(
                    username=username,
                    email='',
                    password=password,
                    user_type='2',
                    first_name=first_name,
                    last_name=last_name,
                    gender=gender,
                    address=address,
                    profile_pic=passport_url,
                )
                pin = issue_pin(user)
            return render_pin_confirmation(
                request,
                'learner',
                pin,
                return_url=reverse('manage_student'),
                return_label='Return to learners',
            )
        else:
            messages.error(request, "Could Not Add: ")
    return _render_manage_students(
        request, student_form=student_form, open_modal='addStudentModal')


def add_subject(request):
    if request.method != 'POST':
        return redirect(reverse('manage_subject'))

    form = SubjectForm(request.POST or None)
    form.fields.pop('course')
    context = {
        'form': form,
        'page_title': 'Add Subject'
    }
    if request.method == 'POST':
        if form.is_valid():
            try:
                subject = form.save()
                messages.success(request, "Successfully Added")
                return redirect(reverse('manage_subject'))

            except Exception as e:
                messages.error(request, "Could Not Add " + str(e))
        else:
            messages.error(request, "Fill Form Properly")

    return _render_manage_subjects(request, form=form, open_modal=True)


def _render_manage_students(request, student_form=None, open_modal=None):
    if student_form is None:
        student_form = StudentForm()
        student_form.fields.pop('course')
        student_form.fields.pop('session')
    learner_ids_without_profiles = CustomUser.objects.filter(
        user_type='2', student__isnull=True).values_list('id', flat=True)
    Student.objects.bulk_create(
        [Student(admin_id=learner_id)
         for learner_id in learner_ids_without_profiles],
        ignore_conflicts=True,
    )
    context = {
        'students': CustomUser.objects.filter(
            user_type=2).select_related('student'),
        'student_form': student_form,
        'open_modal': open_modal,
        'page_title': 'Manage Learners',
    }
    return render(request, "hod_template/manage_student.html", context)


def _render_manage_subjects(request, form=None, open_modal=False, read_only=False):
    if form is None:
        form = SubjectForm()
        form.fields.pop('course')
    context = {
        'subjects': Subject.objects.all(),
        'form': form,
        'open_modal': open_modal,
        'read_only': read_only,
        'page_title': 'Subjects' if read_only else 'Manage Subjects',
    }
    return render(request, "hod_template/manage_subject.html", context)


def manage_student(request):
    return _render_manage_students(request)


def manage_subject(request):
    return _render_manage_subjects(
        request, read_only=request.user.user_type == '3')


def edit_student(request, student_id):
    student = get_object_or_404(Student, id=student_id)
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
            username = form.cleaned_data.get('username')
            gender = form.cleaned_data.get('gender')
            password = form.cleaned_data.get('password') or None
            course = form.cleaned_data.get('course')
            session = form.cleaned_data.get('session')
            passport = request.FILES.get('profile_pic') or None
            try:
                user = CustomUser.objects.get(id=student.admin.id)
                if passport != None:
                    fs = FileSystemStorage()
                    filename = fs.save(passport.name, passport)
                    passport_url = fs.url(filename)
                    user.profile_pic = passport_url
                user.username = username
                if password != None:
                    user.set_password(password)
                    user.failed_login_attempts = 0
                    user.locked_until = None
                user.first_name = first_name
                user.last_name = last_name
                user.gender = gender
                user.address = address
                user.save()
                student.course = course
                student.session = session
                student.save()
                messages.success(request, "Successfully Updated")
                return redirect(reverse('edit_student', args=[student_id]))
            except Exception as e:
                messages.error(request, "Could Not Update " + str(e))
        else:
            messages.error(request, "Please Fill Form Properly!")
    else:
        return render(request, "hod_template/edit_student_template.html", context)
    return render(request, "hod_template/edit_student_template.html", context)


def edit_subject(request, subject_id):
    instance = get_object_or_404(Subject, id=subject_id)
    form = SubjectForm(request.POST or None, instance=instance)
    context = {
        'form': form,
        'subject_id': subject_id,
        'page_title': 'Edit Subject'
    }
    if request.method == 'POST':
        if form.is_valid():
            try:
                form.save()
                messages.success(request, "Successfully Updated")
                return redirect(reverse('edit_subject', args=[subject_id]))
            except Exception as e:
                messages.error(request, "Could Not Add " + str(e))
        else:
            messages.error(request, "Fill Form Properly")
    return render(request, 'hod_template/edit_subject_template.html', context)


def manage_student_results(request):
    form = StudentResultForm(request.POST or None)
    results = list(StudentResult.objects.select_related(
        'student__admin', 'subject').order_by(
            'student__admin__last_name', 'student__admin__first_name',
            'subject__name').annotate(
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
    context = {
        'form': form,
        'results': results,
        'page_title': 'Manage Learner Results',
    }
    if request.method == 'POST':
        if form.is_valid():
            form.save()
            messages.success(request, 'Learner result added successfully.')
            return redirect(reverse('manage_student_results'))
        messages.error(request, 'Please correct the errors in the result form.')
    return render(request, 'hod_template/manage_student_results.html', context)


def edit_student_result(request, result_id):
    result = get_object_or_404(StudentResult, id=result_id)
    form = StudentResultForm(request.POST or None, instance=result)
    context = {
        'form': form,
        'result': result,
        'page_title': 'Update Learner Result',
        'button_text': 'Save Changes',
    }
    if request.method == 'POST':
        if form.is_valid():
            form.save()
            messages.success(request, 'Learner result updated successfully.')
            return redirect(reverse('manage_student_results'))
        messages.error(request, 'Please correct the errors in the result form.')
    return render(request, 'hod_template/edit_student_result.html', context)


def delete_student_result(request, result_id):
    result = get_object_or_404(StudentResult, id=result_id)
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
    form = ProfileUpdateForm(
        request.POST or None,
        request.FILES or None,
        user=custom_user,
    )
    context = {'form': form,
               'page_title': 'My Profile',
               'custom_user': custom_user,
               }
    if request.method == 'POST' and form.is_valid():
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
    student = CustomUser.objects.filter(user_type=2)
    context = {
        'page_title': "Send Notifications To Learners",
        'students': student
    }
    return render(request, "hod_template/student_notification.html", context)


@csrf_exempt
def send_student_notification(request):
    id = request.POST.get('id')
    message = request.POST.get('message')
    student = get_object_or_404(Student, admin_id=id)
    try:
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
        headers = {'Authorization':
                   'key=AAAA3Bm8j_M:APA91bElZlOLetwV696SoEtgzpJr2qbxBfxVBfDWFiopBWzfCfzQp2nRyC7_A2mlukZEHV4g1AmyC6P_HonvSkY2YyliKt5tT3fe_1lrKod2Daigzhb2xnYQMxUWjCAIQcUexAMPZePB',
                   'Content-Type': 'application/json'}
        data = requests.post(url, data=json.dumps(body), headers=headers)
        notification = NotificationStudent(student=student, message=message)
        notification.save()
        return HttpResponse("True")
    except Exception as e:
        return HttpResponse("False")


@csrf_exempt
def delete_student(request, student_id):
    student = get_object_or_404(CustomUser, student__id=student_id)
    student.delete()
    messages.success(request, "Learner deleted successfully!")
    return redirect(reverse('manage_student'))


def delete_subject(request, subject_id):
    subject = get_object_or_404(Subject, id=subject_id)
    subject.delete()
    messages.success(request, "Subject deleted successfully!")
    return redirect(reverse('manage_subject'))
