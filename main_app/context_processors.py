from .models import AcademicPeriod, LearnerTeacherRequest, Teacher


def current_academic_period(request):
    return {
        'current_academic_period': AcademicPeriod.objects.filter(
            is_current=True).first(),
    }


def pending_teacher_request_count(request):
    count = 0
    user = getattr(request, 'user', None)
    if user and user.is_authenticated and user.user_type == '3':
        teacher = Teacher.objects.filter(teacher=user).first()
        if teacher:
            count = LearnerTeacherRequest.objects.filter(
                teacher=teacher,
                status=LearnerTeacherRequest.PENDING,
            ).count()
    return {'pending_teacher_request_count': count}


def active_sidebar_item(request):
    route_groups = {
        'admin_home': 'home',
        'teacher_home': 'home',
        'student_home': 'home',
        'admin_view_profile': 'profile',
        'manage_admins': 'admins',
        'teacher_profile': 'profile',
        'student_view_profile': 'profile',
        'manage_student': 'learners',
        'add_student': 'learners',
        'edit_student': 'learners',
        'delete_student': 'learners',
        'import_learners': 'learners',
        'download_learner_import_template': 'learners',
        'manage_teachers': 'teachers',
        'add_teacher': 'teachers',
        'edit_teacher': 'teachers',
        'delete_teacher': 'teachers',
        'manage_assignments': 'assignments',
        'assignment_learners': 'assignments',
        'manage_academic_periods': 'periods',
        'edit_academic_period': 'periods',
        'set_current_academic_period': 'periods',
        'delete_academic_period': 'periods',
        'school_profile': 'school_profile',
        'activity_log': 'activity',
        'activity_user_timeline': 'activity',
        'student_feedback_message': 'feedback',
        'view_student_leave': 'leave',
        'admin_notify_student': 'notifications',
        'send_student_notification': 'notifications',
        'teacher_classes_subjects': 'classes_subjects',
        'learner_requests': 'requests',
        'manage_student_results': 'results',
        'edit_student_result': 'results',
        'delete_student_result': 'results',
        'generate_report_card': 'report_cards',
        'generate_report': 'reports',
        'student_view_subjects': 'subjects',
        'student_view_result': 'results',
        'student_view_notification': 'notifications',
        'student_apply_leave': 'leave',
        'student_feedback': 'feedback',
        'recycle_bin': 'recycle_bin',
    }
    match = getattr(request, 'resolver_match', None)
    return {'active_sidebar_item': route_groups.get(
        getattr(match, 'url_name', None))}
