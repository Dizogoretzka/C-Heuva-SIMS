"""student_management_system URL Configuration

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/3.1/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.urls import path

from . import hod_views, student_views, teacher_views, views

urlpatterns = [
    path("", views.login_page, name='login_page'),
    path("login/<str:role>/", views.role_login, name='role_login'),
    path("register/learner/", views.register_learner, name='register_learner'),
    path("firebase-messaging-sw.js", views.showFirebaseJS, name='showFirebaseJS'),
    path("doLogin/", views.doLogin, name='user_login'),
    path("logout_user/", views.logout_user, name='user_logout'),
    path("admin/home/", hod_views.admin_home, name='admin_home'),
    path("admin/academic-periods/", hod_views.manage_academic_periods,
         name='manage_academic_periods'),
    path("admin/academic-periods/<int:period_id>/edit/",
         hod_views.edit_academic_period, name='edit_academic_period'),
    path("admin/academic-periods/<int:period_id>/current/",
         hod_views.set_current_academic_period,
         name='set_current_academic_period'),
    path("admin/academic-periods/<int:period_id>/delete/",
         hod_views.delete_academic_period, name='delete_academic_period'),
    path("teacher/home/", teacher_views.teacher_home, name='teacher_home'),
    path("teacher/profile/", teacher_views.teacher_profile,
         name='teacher_profile'),
    path("admin/teachers/", hod_views.manage_teachers, name='manage_teachers'),
    path("admin/assignments/", hod_views.manage_assignments,
         name='manage_assignments'),
    path("admin/teachers/add/", hod_views.add_teacher, name='add_teacher'),
    path("admin/teachers/edit/<int:teacher_id>/",
         hod_views.edit_teacher, name='edit_teacher'),
    path("admin/teachers/delete/<int:teacher_id>/",
         hod_views.delete_teacher, name='delete_teacher'),
    path("staff/accounts/<int:user_id>/credentials/",
         hod_views.reset_user_credentials, name='reset_user_credentials'),
    path("admin/attendance/", hod_views.attendance_records,
         name='admin_attendance_records'),
    path("teacher/attendance/", teacher_views.teacher_attendance,
         name='teacher_attendance'),
    path("admin/learner-login-activity/",
         hod_views.learner_login_activity, name='learner_login_activity'),
    path("admin/activity/", hod_views.activity_log, name='activity_log'),
    path("admin/activity/user/<int:user_id>/",
         hod_views.activity_log, name='activity_user_timeline'),
    path("admin/reports/generate/", hod_views.generate_report,
         name='generate_report'),
    path("admin/reports/report-card/",
         hod_views.generate_report_card, name='generate_report_card'),
    path("send_student_notification/", hod_views.send_student_notification,
         name='send_student_notification'),
    path("admin_notify_student", hod_views.admin_notify_student,
         name='admin_notify_student'),
    path("admin_view_profile", hod_views.admin_view_profile,
         name='admin_view_profile'),
    path("student/view/feedback/", hod_views.student_feedback_message,
         name="student_feedback_message",),
    path("student/view/leave/", hod_views.view_student_leave,
         name="view_student_leave",),
    path("student/add/", hod_views.add_student, name='add_student'),
    path("student/import/", hod_views.import_learners,
         name='import_learners'),
    path("student/import/template/", hod_views.download_learner_import_template,
         name='download_learner_import_template'),
    path("subject/add/", hod_views.add_subject, name='add_subject'),
    path("student/manage/", hod_views.manage_student, name='manage_student'),
    path("subject/manage/", hod_views.manage_subject, name='manage_subject'),
    path("subject/delete/<int:subject_id>",
         hod_views.delete_subject, name='delete_subject'),

    path("student/delete/<int:student_id>",
         hod_views.delete_student, name='delete_student'),
    path("student/edit/<int:student_id>",
         hod_views.edit_student, name='edit_student'),
    path("subject/edit/<int:subject_id>",
         hod_views.edit_subject, name='edit_subject'),
    path("results/manage/", hod_views.manage_student_results,
         name='manage_student_results'),
    path("results/edit/<int:result_id>/", hod_views.edit_student_result,
         name='edit_student_result'),
    path("results/delete/<int:result_id>/", hod_views.delete_student_result,
         name='delete_student_result'),

    # Learner
    path("student/home/", student_views.student_home, name='student_home'),
    path("student/subjects/", student_views.student_view_subjects,
         name='student_view_subjects'),
    path("student/apply/leave/", student_views.student_apply_leave,
         name='student_apply_leave'),
    path("student/feedback/", student_views.student_feedback,
         name='student_feedback'),
    path("student/view/profile/", student_views.student_view_profile,
         name='student_view_profile'),
    path("student/fcmtoken/", student_views.student_fcmtoken,
         name='student_fcmtoken'),
    path("student/view/notification/", student_views.student_view_notification,
         name="student_view_notification"),
    path('student/view/result/', student_views.student_view_result,
         name='student_view_result'),
    path('student/report-card/', student_views.student_report_card,
         name='student_report_card'),
    path('student/attendance/', student_views.student_attendance,
         name='student_attendance'),

]
