from django.db import migrations, models


def migrate_fixed_assessments(apps, schema_editor):
    StudentResult = apps.get_model('main_app', 'StudentResult')
    AssessmentWeight = apps.get_model('main_app', 'AssessmentWeight')
    database = schema_editor.connection.alias

    for result in StudentResult.objects.using(database).iterator():
        result.test_scores = {
            f'Test {number}': getattr(result, f'test_{number}')
            for number in range(1, 6)
        }
        result.save(update_fields=['test_scores'], using=database)

    for assessment in AssessmentWeight.objects.using(database).iterator():
        assessment.test_weights = [
            {
                'name': f'Test {number}',
                'weight': str(getattr(assessment, f'test_{number}_weight')),
            }
            for number in range(1, 6)
        ]
        assessment.save(update_fields=['test_weights'], using=database)


class Migration(migrations.Migration):

    dependencies = [
        ('main_app', '0020_weighted_assessments'),
    ]

    operations = [
        migrations.AddField(
            model_name='studentresult',
            name='test_scores',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name='assessmentweight',
            name='test_weights',
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.RunPython(
            migrate_fixed_assessments,
            migrations.RunPython.noop,
        ),
    ]
