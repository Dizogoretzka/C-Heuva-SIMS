from django.contrib import admin
from .models import *
# Register your models here.


admin.site.register(Student)
admin.site.register(Teacher)
admin.site.register(Subject)
admin.site.register(AcademicPeriod)
admin.site.register(StudentResult)
admin.site.register(AssessmentPlan)
admin.site.register(Assessment)
admin.site.register(SchoolProfile)


@admin.register(SchoolClass)
class SchoolClassAdmin(admin.ModelAdmin):
    list_display = ('grade_number', 'class_letter')

    def get_queryset(self, request):
        return super().get_queryset(request).filter(
            grade_number__isnull=False)


@admin.register(ActivityLog)
class ActivityLogAdmin(admin.ModelAdmin):
    list_display = (
        'created_at', 'actor_username', 'action', 'object_type',
        'object_id', 'target_username',
    )
    list_filter = ('action', 'object_type', 'created_at')
    search_fields = (
        'actor_username', 'target_username', 'description', 'object_id',
    )
    readonly_fields = (
        'actor', 'actor_username', 'target_user', 'target_username',
        'target_user_id_snapshot', 'action', 'object_type', 'object_id',
        'description', 'changes', 'created_at',
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
