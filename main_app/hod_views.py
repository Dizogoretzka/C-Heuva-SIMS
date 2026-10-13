import json
from datetime import date
from io import BytesIO
from xml.sax.saxutils import escape

import requests
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.files.storage import FileSystemStorage
from django.db.models import Count, ExpressionWrapper, F, FloatField, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.templatetags.static import static
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .forms import *
from .models import *


def admin_home(request):
    total_students = Student.objects.all().count()
    context = {
        'page_title': "Administrative Dashboard",
        'total_students': total_students,
        'total_subject': Subject.objects.count(),
    }
    return render(request, 'hod_template/home_content.html', context)


@login_required
def generate_report(request):
    if request.user.user_type != '1':
        return redirect(reverse('student_home'))

    form = ReportGenerationForm(request.POST or None)
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
                 learner.admin.email] for learner in learners]
        return f'Class List - {course.name} - {session}', [
            'Learner name', 'Email'], rows

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

    learners = Student.objects.filter(
        course=course, session=session
    ).select_related('admin').annotate(
        attendance_total=Count(
            'attendancereport',
            filter=Q(
                attendancereport__attendance__session=session,
                attendancereport__attendance__subject__course=course,
            ),
        ),
        attendance_present=Count(
            'attendancereport',
            filter=Q(
                attendancereport__attendance__session=session,
                attendancereport__attendance__subject__course=course,
                attendancereport__status=True,
            ),
        ),
    ).order_by('admin__last_name', 'admin__first_name')
    if not AttendanceReport.objects.filter(
        attendance__session=session,
        attendance__subject__course=course,
    ).exists():
        return f'Attendance Summary - {course.name} - {session}', [
            'Learner name', 'Present', 'Absent'], []
    rows = []
    for learner in learners:
        total = learner.attendance_total
        present = learner.attendance_present
        absent = total - present
        rows.append([
            f'{learner.admin.first_name} {learner.admin.last_name}',
            f'{present / total * 100:.1f}%' if total else '0.0%',
            f'{absent / total * 100:.1f}%' if total else '0.0%',
        ])
    return f'Attendance Summary - {course.name} - {session}', [
        'Learner name', 'Present', 'Absent'], rows

def _create_report_pdf(user, title, headers, rows):
    buffer = BytesIO()
    generated_date = date.today().strftime('%B %d, %Y')
    display_name = user.get_full_name().strip() or user.email
    styles = getSampleStyleSheet()
    cell_style = ParagraphStyle(
        'ReportCell', parent=styles['BodyText'], fontName='Helvetica',
        fontSize=8, leading=10, spaceAfter=0)
    header_style = ParagraphStyle(
        'ReportHeader', parent=cell_style, fontName='Helvetica-Bold',
        textColor=colors.white)
    report_title = Paragraph(escape(title), styles['Heading2'])
    body = [report_title, Spacer(1, 5 * mm)]
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
    document = SimpleDocTemplate(
        buffer, pagesize=A4, rightMargin=16 * mm, leftMargin=16 * mm,
        topMargin=34 * mm, bottomMargin=22 * mm,
        title=title, author=display_name)

    def draw_page(canvas, doc):
        canvas.saveState()
        page_width, page_height = A4
        canvas.setFillColor(colors.HexColor('#243b53'))
        canvas.setFont('Helvetica-Bold', 13)
        canvas.drawCentredString(
            page_width / 2, page_height - 15 * mm,
            'C. Heuva Junior Secondary School')
        canvas.setStrokeColor(colors.HexColor('#cbd2d9'))
        canvas.line(16 * mm, page_height - 19 * mm,
                    page_width - 16 * mm, page_height - 19 * mm)
        canvas.setFont('Helvetica', 8)
        canvas.setFillColor(colors.HexColor('#52606d'))
        canvas.drawString(16 * mm, page_height - 25 * mm,
                          f'Date generated: {generated_date}')
        canvas.line(16 * mm, 16 * mm, page_width - 16 * mm, 16 * mm)
        canvas.drawString(16 * mm, 10 * mm, f'Generated by {display_name}')
        canvas.drawRightString(
            page_width - 16 * mm, 10 * mm, f'Page {doc.page}')
        canvas.restoreState()

    document.build(body, onFirstPage=draw_page, onLaterPages=draw_page)
    response = HttpResponse(buffer.getvalue(), content_type='application/pdf')
    response['Content-Disposition'] = 'attachment; filename="school-report.pdf"'
    return response


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
            email = student_form.cleaned_data.get('email')
            gender = student_form.cleaned_data.get('gender')
            password = student_form.cleaned_data.get('password')
            passport = request.FILES['profile_pic']
            fs = FileSystemStorage()
            filename = fs.save(passport.name, passport)
            passport_url = fs.url(filename)
            try:
                user = CustomUser.objects.create_user(
                    email=email, password=password, user_type=2, first_name=first_name, last_name=last_name, profile_pic=passport_url)
                user.gender = gender
                user.address = address
                user.save()
                messages.success(request, "Successfully Added")
                return redirect(reverse('manage_student'))
            except Exception as e:
                messages.error(request, "Could Not Add: " + str(e))
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


def _render_manage_subjects(request, form=None, open_modal=False):
    if form is None:
        form = SubjectForm()
        form.fields.pop('course')
    context = {
        'subjects': Subject.objects.all(),
        'form': form,
        'open_modal': open_modal,
        'page_title': 'Manage Subjects',
    }
    return render(request, "hod_template/manage_subject.html", context)


def manage_student(request):
    return _render_manage_students(request)


def manage_subject(request):
    return _render_manage_subjects(request)


def edit_student(request, student_id):
    student = get_object_or_404(Student, id=student_id)
    form = StudentForm(request.POST or None, instance=student)
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
            email = form.cleaned_data.get('email')
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
                user.email = email
                if password != None:
                    user.set_password(password)
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
    context = {
        'form': form,
        'results': StudentResult.objects.select_related(
            'student__admin', 'subject').order_by(
                'student__admin__last_name', 'student__admin__first_name',
                'subject__name').annotate(
                    total_score=ExpressionWrapper(
                        F('test') + F('exam'), output_field=FloatField())),
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
def check_email_availability(request):
    email = request.POST.get("email")
    try:
        user = CustomUser.objects.filter(email=email).exists()
        if user:
            return HttpResponse(True)
        return HttpResponse(False)
    except Exception as e:
        return HttpResponse(False)


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
    admin = get_object_or_404(Admin, admin=request.user)
    form = AdminForm(request.POST or None, request.FILES or None,
                     instance=admin)
    context = {'form': form,
               'page_title': 'View/Edit Profile'
               }
    if request.method == 'POST':
        try:
            if form.is_valid():
                first_name = form.cleaned_data.get('first_name')
                last_name = form.cleaned_data.get('last_name')
                password = form.cleaned_data.get('password') or None
                passport = request.FILES.get('profile_pic') or None
                custom_user = admin.admin
                if password != None:
                    custom_user.set_password(password)
                if passport != None:
                    fs = FileSystemStorage()
                    filename = fs.save(passport.name, passport)
                    passport_url = fs.url(filename)
                    custom_user.profile_pic = passport_url
                custom_user.first_name = first_name
                custom_user.last_name = last_name
                custom_user.save()
                messages.success(request, "Profile Updated!")
                return redirect(reverse('admin_view_profile'))
            else:
                messages.error(request, "Invalid Data Provided")
        except Exception as e:
            messages.error(
                request, "Error Occured While Updating Profile " + str(e))
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
