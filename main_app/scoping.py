from .models import (
    LearnerTeacherRequest,
    Student,
    StudentResult,
    Subject,
    Teacher,
)


def teacher_profile(user):
    return Teacher.objects.get(teacher=user)


def visible_students(user):
    students = Student.objects.select_related(
        'admin', 'created_by__teacher',
        'school_class', 'academic_period')
    if user.user_type == '3':
        students = students.filter(
            teacher_requests__teacher__teacher=user,
            teacher_requests__status=LearnerTeacherRequest.ACCEPTED,
        ).distinct()
    return students


def visible_subjects(user):
    subjects = Subject.objects.select_related(
        'teacher__teacher', 'created_by__teacher', 'school_class')
    if user.user_type == '3':
        subjects = subjects.filter(
            teacher__teacher=user,
            school_class__teacher_setups__teacher__teacher=user,
        )
    return subjects


def visible_results(user):
    results = StudentResult.objects.select_related(
        'student__admin',
        'subject__teacher__teacher',
        'created_by__teacher',
    )
    if user.user_type == '3':
        results = results.filter(
            student__teacher_requests__teacher__teacher=user,
            student__teacher_requests__status=LearnerTeacherRequest.ACCEPTED,
            subject__teacher__teacher=user,
            subject__school_class__teacher_setups__teacher__teacher=user,
        )
    return results
