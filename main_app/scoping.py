from django.db.models import Q

from .models import Attendance, Student, StudentResult, Subject, Teacher


def teacher_profile(user):
    return Teacher.objects.get(teacher=user)


def visible_students(user):
    students = Student.objects.select_related(
        'admin', 'teacher__teacher', 'created_by__teacher', 'course', 'academic_period')
    if user.user_type == '3':
        students = students.filter(teacher__teacher=user)
    return students


def visible_subjects(user):
    subjects = Subject.objects.select_related(
        'teacher__teacher', 'created_by__teacher', 'course')
    if user.user_type == '3':
        subjects = subjects.filter(
            Q(teacher__teacher=user) | Q(teacher__isnull=True))
    return subjects


def visible_results(user):
    results = StudentResult.objects.select_related(
        'student__admin',
        'student__teacher__teacher',
        'subject__teacher__teacher',
        'created_by__teacher',
    )
    if user.user_type == '3':
        results = results.filter(
            student__teacher__teacher=user,
        ).filter(
            Q(subject__teacher__teacher=user)
            | Q(subject__teacher__isnull=True)
        )
    return results


def visible_attendance(user):
    records = Attendance.objects.select_related(
        'course', 'subject__teacher__teacher', 'academic_period',
        'created_by__teacher')
    if user.user_type == '3':
        records = records.filter(
            Q(subject__teacher__teacher=user)
            | Q(subject__teacher__isnull=True)
        )
    return records
