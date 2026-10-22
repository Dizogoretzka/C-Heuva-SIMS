from django.shortcuts import redirect
from django.urls import reverse
from django.utils.deprecation import MiddlewareMixin


PUBLIC_URLS = {
    'login_page',
    'role_login',
    'register_role',
    'user_login',
    'favicon',
    'serve',
    'static_asset',
}

ADMIN_ONLY_URLS = {
    'admin_home',
    'learner_login_activity',
    'admin_attendance_records',
    'add_subject',
    'edit_subject',
    'delete_subject',
    'student_feedback_message',
    'view_student_leave',
    'admin_view_profile',
    'admin_notify_student',
    'send_student_notification',
    'manage_teachers',
    'add_teacher',
    'edit_teacher',
    'delete_teacher',
}

TEACHER_ONLY_URLS = {
    'teacher_home',
    'manage_student_results',
    'edit_student_result',
    'delete_student_result',
    'manage_grade_bands',
    'edit_grade_band',
    'delete_grade_band',
    'generate_report_card',
    'teacher_attendance',
}

SHARED_STAFF_URLS = {
    'manage_student',
    'add_student',
    'edit_student',
    'delete_student',
    'manage_subject',
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
