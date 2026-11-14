import re

import django
from django.db import migrations, models


_CLASS_LABEL_PATTERN = re.compile(
    r'^\s*(?:grade\s+)?(?P<grade>1[0-2]|[1-9])'
    r'(?:\s*-?\s*(?P<letter>[a-g]))?\s*$',
    re.IGNORECASE,
)


def _replace_old_group_word(value):
    if not isinstance(value, str):
        return value
    value = re.sub(r'\bcourses\b', 'classes', value, flags=re.IGNORECASE)
    return re.sub(r'\bcourse\b', 'class', value, flags=re.IGNORECASE)


def _rewrite_change_values(value):
    if isinstance(value, dict):
        return {
            _replace_old_group_word(key): _rewrite_change_values(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_rewrite_change_values(item) for item in value]
    return _replace_old_group_word(value)


def convert_class_data(apps, schema_editor):
    SchoolClass = apps.get_model('main_app', 'SchoolClass')
    Student = apps.get_model('main_app', 'Student')
    Subject = apps.get_model('main_app', 'Subject')
    ActivityLog = apps.get_model('main_app', 'ActivityLog')
    database = schema_editor.connection.alias

    for school_class in SchoolClass.objects.using(database).order_by('pk'):
        old_label = school_class.name
        match = _CLASS_LABEL_PATTERN.fullmatch(old_label or '')
        if not match:
            SchoolClass.objects.using(database).filter(
                pk=school_class.pk).update(
                    grade_number=None,
                    class_letter='',
                    legacy_label=old_label or '',
                )
            Student.objects.using(database).filter(
                school_class_id=school_class.pk).update(school_class=None)
            Subject.objects.using(database).filter(
                school_class_id=school_class.pk).update(school_class=None)
            continue

        grade_number = int(match.group('grade'))
        class_letter = (match.group('letter') or '').lower()
        existing = SchoolClass.objects.using(database).filter(
            grade_number=grade_number,
            class_letter=class_letter,
        ).exclude(pk=school_class.pk).order_by('pk').first()
        if existing:
            Student.objects.using(database).filter(
                school_class_id=school_class.pk).update(
                    school_class_id=existing.pk)
            Subject.objects.using(database).filter(
                school_class_id=school_class.pk).update(
                    school_class_id=existing.pk)
            school_class.delete(using=database)
            continue

        SchoolClass.objects.using(database).filter(
            pk=school_class.pk).update(
                grade_number=grade_number,
                class_letter=class_letter,
                legacy_label='',
            )

    logs = ActivityLog.objects.using(database).filter(
        object_type='main_app.course')
    for entry in logs.iterator():
        entry.object_type = 'main_app.schoolclass'
        entry.description = _replace_old_group_word(entry.description)
        entry.changes = _rewrite_change_values(entry.changes)
        entry.save(update_fields=['object_type', 'description', 'changes'])


class Migration(migrations.Migration):

    dependencies = [
        (
            'main_app',
            '0022_remove_attendance_unique_course_subject_academic_period_attendance_date_and_more',
        ),
    ]

    operations = [
        migrations.RenameModel(
            old_name='Course',
            new_name='SchoolClass',
        ),
        migrations.RenameField(
            model_name='student',
            old_name='course',
            new_name='school_class',
        ),
        migrations.RenameField(
            model_name='subject',
            old_name='course',
            new_name='school_class',
        ),
        migrations.AddField(
            model_name='schoolclass',
            name='grade_number',
            field=models.PositiveSmallIntegerField(
                blank=True,
                null=True,
                validators=[
                    django.core.validators.MinValueValidator(1),
                    django.core.validators.MaxValueValidator(12),
                ],
            ),
        ),
        migrations.AddField(
            model_name='schoolclass',
            name='class_letter',
            field=models.CharField(blank=True, default='', max_length=1),
        ),
        migrations.AddField(
            model_name='schoolclass',
            name='legacy_label',
            field=models.CharField(blank=True, default='', max_length=120),
        ),
        migrations.RunPython(
            convert_class_data,
            migrations.RunPython.noop,
        ),
        migrations.RemoveField(
            model_name='schoolclass',
            name='name',
        ),
        migrations.AddConstraint(
            model_name='schoolclass',
            constraint=models.UniqueConstraint(
                condition=models.Q(('grade_number__isnull', False)),
                fields=('grade_number', 'class_letter'),
                name='unique_school_class_grade_letter',
            ),
        ),
        migrations.AlterModelOptions(
            name='schoolclass',
            options={'ordering': ['grade_number', 'class_letter']},
        ),
    ]
