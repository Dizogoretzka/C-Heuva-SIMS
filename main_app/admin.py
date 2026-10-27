from django.contrib import admin
from .models import *
# Register your models here.


admin.site.register(Student)
admin.site.register(Teacher)
admin.site.register(Subject)
admin.site.register(Course)
admin.site.register(AcademicPeriod)
admin.site.register(StudentResult)
admin.site.register(Attendance)
admin.site.register(AttendanceReport)


@admin.register(ActivityLog)
class ActivityLogAdmin(admin.ModelAdmin):
    list_display = (
        'created_at', 'actor_username', 'action', 'object_type',
        'object_id', 'target_username', 'ip_address',
    )
    list_filter = ('action', 'object_type', 'created_at')
    search_fields = (
        'actor_username', 'target_username', 'description', 'object_id',
    )
    readonly_fields = (
        'actor', 'actor_username', 'target_user', 'target_username',
        'target_user_id_snapshot', 'action', 'object_type', 'object_id',
        'description', 'changes', 'ip_address', 'created_at',
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
