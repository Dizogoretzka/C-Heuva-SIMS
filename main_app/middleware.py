from django.shortcuts import redirect
from django.urls import reverse
from django.utils.deprecation import MiddlewareMixin

from .activity import reset_current_request, set_current_request


PUBLIC_URLS = {
    'login_page',
    'role_login',
    'register_learner',
    'user_login',
    'favicon',
    'serve',
    'static_asset',
}

ADMIN_ONLY_URLS = {
    'admin_home',
    'manage_academic_periods',
    'edit_academic_period',
    'set_current_academic_period',
    'delete_academic_period',
    'learner_login_activity',
    'activity_log',
    'activity_user_timeline',
    'admin_attendance_records',
    'student_feedback_message',
    'view_student_leave',
    'admin_view_profile',
    'admin_notify_student',
    'send_student_notification',
    'manage_teachers',
    'manage_assignments',
    'add_teacher',
    'edit_teacher',
    'delete_teacher',
}

TEACHER_ONLY_URLS = {
    'teacher_home',
    'manage_student_results',
    'edit_student_result',
    'delete_student_result',
    'generate_report_card',
    'teacher_attendance',
}

SHARED_STAFF_URLS = {
    'manage_student',
    'add_student',
    'import_learners',
    'download_learner_import_template',
    'edit_student',
    'delete_student',
    'manage_subject',
    'add_subject',
    'edit_subject',
    'delete_subject',
    'generate_report',
    'reset_user_credentials',
}


class LoginCheckMiddleWare(MiddlewareMixin):
    @staticmethod
    def dashboard_for(user):
        return {
            '1': 'admin_home',
            '2': 'student_home',
            '3': 'teacher_home',
        }.get(user.user_type, 'login_page')

    def process_view(self, request, view_func, view_args, view_kwargs):
        user = request.user
        url_name = request.resolver_match.url_name
        module_name = view_func.__module__

        if not user.is_authenticated:
            if (
                url_name in PUBLIC_URLS
                or module_name == 'django.contrib.staticfiles.views'
            ):
                return None
            return redirect(reverse('login_page'))

        if url_name == 'password_change':
            return redirect(reverse(self.dashboard_for(user)))

        if user.user_type not in ('1', '2', '3'):
            return redirect(reverse('login_page'))

        if url_name in ADMIN_ONLY_URLS and user.user_type != '1':
            return redirect(reverse(self.dashboard_for(user)))
        if url_name in TEACHER_ONLY_URLS and user.user_type != '3':
            return redirect(reverse(self.dashboard_for(user)))
        if url_name in SHARED_STAFF_URLS and user.user_type not in ('1', '3'):
            return redirect(reverse(self.dashboard_for(user)))
        if module_name == 'main_app.student_views' and user.user_type != '2':
            return redirect(reverse(self.dashboard_for(user)))
        if module_name == 'main_app.teacher_views' and user.user_type != '3':
            return redirect(reverse(self.dashboard_for(user)))

        return None


class ActivityRequestMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        token = set_current_request(request)
        try:
            return self.get_response(request)
        finally:
            reset_current_request(token)
