from django.db import migrations, models
from django.db.models import Q


def rename_academic_period_activity_objects(apps, schema_editor):
    activity_log = apps.get_model('main_app', 'ActivityLog')
    activity_log.objects.filter(
        object_type='main_app.session').update(
            object_type='main_app.academicperiod')


class Migration(migrations.Migration):

    dependencies = [
        ('main_app', '0016_activitylog_target_user_id_snapshot_and_more'),
    ]

    operations = [
        migrations.RenameModel(
            old_name='Session',
            new_name='AcademicPeriod',
        ),
        migrations.RenameField(
            model_name='student',
            old_name='session',
            new_name='academic_period',
        ),
        migrations.RemoveConstraint(
            model_name='attendance',
            name='unique_course_subject_session_attendance_date',
        ),
        migrations.RenameField(
            model_name='attendance',
            old_name='session',
            new_name='academic_period',
        ),
        migrations.AlterField(
            model_name='academicperiod',
            name='start_year',
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name='academicperiod',
            name='end_year',
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='academicperiod',
            name='year',
            field=models.PositiveSmallIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='academicperiod',
            name='term',
            field=models.PositiveSmallIntegerField(
                blank=True,
                choices=[(1, 'Term 1'), (2, 'Term 2'), (3, 'Term 3')],
                null=True,
            ),
        ),
        migrations.AddField(
            model_name='academicperiod',
            name='is_current',
            field=models.BooleanField(default=False),
        ),
        migrations.AddConstraint(
            model_name='attendance',
            constraint=models.UniqueConstraint(
                fields=('course', 'subject', 'academic_period', 'date'),
                name='unique_course_subject_academic_period_attendance_date',
            ),
        ),
        migrations.AddConstraint(
            model_name='academicperiod',
            constraint=models.UniqueConstraint(
                condition=Q(year__isnull=False, term__isnull=False),
                fields=('year', 'term'),
                name='unique_academic_period_year_term',
            ),
        ),
        migrations.AddConstraint(
            model_name='academicperiod',
            constraint=models.UniqueConstraint(
                condition=Q(is_current=True),
                fields=('is_current',),
                name='only_one_current_academic_period',
            ),
        ),
        migrations.RunPython(
            rename_academic_period_activity_objects,
            migrations.RunPython.noop,
        ),
    ]
