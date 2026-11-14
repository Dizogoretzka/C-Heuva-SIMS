from django.utils import timezone

from .models import LearnerTeacherRequest, Student, TeacherClassSetup


def create_class_requests(student, accepted_teacher=None):
    setups = TeacherClassSetup.objects.none()
    if student.school_class_id is not None:
        setups = TeacherClassSetup.objects.filter(
            school_class_id=student.school_class_id).select_related('teacher')
    accepted_teacher_added = False
    for setup in setups:
        existing_request = LearnerTeacherRequest.objects.filter(
            student=student,
            teacher=setup.teacher,
            school_class_id=student.school_class_id,
            status__in=(
                LearnerTeacherRequest.PENDING,
                LearnerTeacherRequest.ACCEPTED,
                LearnerTeacherRequest.DECLINED,
            ),
        ).exists()
        if existing_request:
            continue
        status = (
            LearnerTeacherRequest.ACCEPTED
            if accepted_teacher and setup.teacher_id == accepted_teacher.pk
            else LearnerTeacherRequest.PENDING
        )
        accepted_teacher_added |= status == LearnerTeacherRequest.ACCEPTED
        LearnerTeacherRequest.objects.create(
            student=student,
            teacher=setup.teacher,
            school_class_id=student.school_class_id,
            status=status,
            responded_at=(
                timezone.now()
                if status == LearnerTeacherRequest.ACCEPTED else None
            ),
        )
    if accepted_teacher and not accepted_teacher_added:
        existing_request = LearnerTeacherRequest.objects.filter(
            student=student,
            teacher=accepted_teacher,
            school_class_id=student.school_class_id,
            status__in=(
                LearnerTeacherRequest.PENDING,
                LearnerTeacherRequest.ACCEPTED,
                LearnerTeacherRequest.DECLINED,
            ),
        ).exists()
        if not existing_request:
            LearnerTeacherRequest.objects.create(
                student=student,
                teacher=accepted_teacher,
                school_class_id=student.school_class_id,
                status=LearnerTeacherRequest.ACCEPTED,
                responded_at=timezone.now(),
            )


def reset_class_requests(student):
    for learner_request in LearnerTeacherRequest.objects.filter(
        student=student,
    ).exclude(status=LearnerTeacherRequest.CANCELLED):
        learner_request.status = LearnerTeacherRequest.CANCELLED
        learner_request.responded_at = timezone.now()
        learner_request.save(update_fields=['status', 'responded_at'])


def create_requests_for_class(school_class):
    for student in Student.objects.filter(school_class=school_class).iterator():
        create_class_requests(student)
