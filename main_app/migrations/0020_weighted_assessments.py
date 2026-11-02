from django.db import migrations, models
import django.db.models.deletion


def assign_existing_results_to_student_period(apps, schema_editor):
    StudentResult = apps.get_model('main_app', 'StudentResult')
    results = StudentResult.objects.filter(
        academic_period__isnull=True,
        student__academic_period__isnull=False,
    ).select_related('student')
    for result in results.iterator():
        result.academic_period_id = result.student.academic_period_id
        result.save(update_fields=['academic_period'])


class Migration(migrations.Migration):

    dependencies = [
        ('main_app', '0019_alter_activitylog_action'),
    ]

    operations = [
        migrations.RenameField(
            model_name='studentresult',
            old_name='test',
            new_name='test_1',
        ),
        migrations.AddField(
            model_name='studentresult',
            name='academic_period',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='results',
                to='main_app.academicperiod',
            ),
        ),
        migrations.AddField(
            model_name='studentresult',
            name='test_2',
            field=models.FloatField(default=0),
        ),
        migrations.AddField(
            model_name='studentresult',
            name='test_3',
            field=models.FloatField(default=0),
        ),
        migrations.AddField(
            model_name='studentresult',
            name='test_4',
            field=models.FloatField(default=0),
        ),
        migrations.AddField(
            model_name='studentresult',
            name='test_5',
            field=models.FloatField(default=0),
        ),
        migrations.CreateModel(
            name='AssessmentWeight',
            fields=[
                ('id', models.AutoField(
                    auto_created=True,
                    primary_key=True,
                    serialize=False,
                    verbose_name='ID',
                )),
                ('test_1_weight', models.DecimalField(
                    decimal_places=2, max_digits=5)),
                ('test_2_weight', models.DecimalField(
                    decimal_places=2, max_digits=5)),
                ('test_3_weight', models.DecimalField(
                    decimal_places=2, max_digits=5)),
                ('test_4_weight', models.DecimalField(
                    decimal_places=2, max_digits=5)),
                ('test_5_weight', models.DecimalField(
                    decimal_places=2, max_digits=5)),
                ('exam_weight', models.DecimalField(
                    decimal_places=2, max_digits=5)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('academic_period', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='assessment_weights',
                    to='main_app.academicperiod',
                )),
                ('subject', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='assessment_weights',
                    to='main_app.subject',
                )),
            ],
            options={
                'ordering': [
                    'academic_period__year',
                    'academic_period__term',
                    'subject__name',
                ],
            },
        ),
        migrations.AddConstraint(
            model_name='assessmentweight',
            constraint=models.UniqueConstraint(
                fields=('subject', 'academic_period'),
                name='unique_subject_period_assessment_weights',
            ),
        ),
        migrations.RunPython(
            assign_existing_results_to_student_period,
            migrations.RunPython.noop,
        ),
    ]
