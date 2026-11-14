import os, django
from pathlib import Path
os.environ.setdefault("DJANGO_SETTINGS_MODULE","student_management_system.settings")
django.setup()
from main_app.models import SchoolClass, Student, Subject
from django.apps import apps
print("Course model present:", any(m.__name__=="Course" for m in apps.get_models()))
print("SchoolClass count:", SchoolClass.objects.count())
unconverted = list(SchoolClass.objects.filter(grade_number__isnull=True).values_list("pk","legacy_label"))
print("Unconverted SchoolClass:", unconverted)
print("Students without class:", Student.objects.filter(school_class__isnull=True).count(), "/", Student.objects.count())
print("Subjects without class:", Subject.objects.filter(school_class__isnull=True).count(), "/", Subject.objects.count())
hits=[]
root=Path("main_app")
for path in list(root.rglob("*.html"))+list(root.rglob("*.py")):
    if path.name=="tests.py" or "migrations" in path.parts:
        continue
    text=path.read_text(encoding="utf-8", errors="ignore")
    for i,line in enumerate(text.splitlines(),1):
        low=line.lower()
        if "course" in low and "discourse" not in low:
            hits.append(f"{path.as_posix()}:{i}:{line.strip()[:140]}")
print("COURSE_HITS", len(hits))
for h in hits[:50]:
    print(h)
