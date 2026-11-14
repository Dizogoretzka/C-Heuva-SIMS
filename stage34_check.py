import os, django
os.environ.setdefault("DJANGO_SETTINGS_MODULE","student_management_system.settings")
django.setup()
from main_app.models import Teacher, Subject, Student, TeacherClassSetup, LearnerTeacherRequest, Assessment, AssessmentPlan, StudentResult, SchoolProfile
print("Teachers:", Teacher.objects.count())
print("Subjects total:", Subject.objects.count())
legacy = Subject.objects.filter(teacher__isnull=True)
print("Legacy subjects (not set up):", [(s.pk, s.name, s.school_class_id) for s in legacy])
print("TeacherClassSetup:", TeacherClassSetup.objects.count())
print("Requests:", list(LearnerTeacherRequest.objects.values_list("status").distinct()))
print("AssessmentPlans:", AssessmentPlan.objects.count())
print("Assessments:", Assessment.objects.count())
print("Previous results:", StudentResult.objects.filter(is_previous_record=True).count(), "/", StudentResult.objects.count())
print("SchoolProfile:", list(SchoolProfile.objects.values()))
teachers_needing_setup = []
for t in Teacher.objects.select_related("teacher"):
    setups = t.class_setups.count()
    subjects = t.subjects.count()
    if setups == 0 or subjects == 0:
        teachers_needing_setup.append((t.pk, str(t), setups, subjects))
print("Teachers needing setup:", teachers_needing_setup)
