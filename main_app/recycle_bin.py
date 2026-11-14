import json

from django.apps import apps
from django.core import serializers
from django.db import router, transaction
from django.db.models import FileField, QuerySet
from django.db.models.deletion import Collector
from django.utils.text import capfirst

from .models import RecycleBinEntry


class RestoreConflictError(Exception):
    pass


def _serialized_record(instance):
    return json.loads(serializers.serialize('json', [instance]))[0]


def trash_queryset(queryset, owner, record_type, record_label):
    instances = list(queryset)
    if not instances:
        return None

    using = queryset.db or router.db_for_write(instances[0].__class__)
    collector = Collector(using=using)
    collector.collect(instances)
    collector.sort()

    deleted_objects = []
    seen_deleted = set()
    for model, model_instances in collector.data.items():
        for instance in model_instances:
            key = (model, instance.pk)
            if key not in seen_deleted:
                seen_deleted.add(key)
                deleted_objects.append(instance)
    for fast_delete in collector.fast_deletes:
        for instance in fast_delete:
            key = (fast_delete.model, instance.pk)
            if key not in seen_deleted:
                seen_deleted.add(key)
                deleted_objects.append(instance)

    updated_fields = {}
    for (field, _), instance_groups in collector.field_updates.items():
        for group in instance_groups:
            group_instances = list(group) if isinstance(group, QuerySet) else group
            for instance in group_instances:
                key = (instance.__class__, instance.pk)
                if key not in seen_deleted:
                    updated_fields.setdefault(key, (instance, set()))[1].add(
                        field.name)

    snapshot = {
        'deleted': [
            _serialized_record(instance)
            for instance in reversed(deleted_objects)
        ],
        'updated': [
            {
                'record': _serialized_record(instance),
                'fields': sorted(fields),
            }
            for instance, fields in updated_fields.values()
        ],
    }

    with transaction.atomic(using=using):
        entry = RecycleBinEntry.objects.using(using).create(
            owner=owner,
            record_type=record_type,
            record_label=record_label,
            object_count=len(deleted_objects),
            snapshot=snapshot,
        )
        collector.delete()
    return entry


def trash_instance(instance, owner, record_type, record_label=None):
    model = instance.__class__
    label = record_label or capfirst(str(instance))
    return trash_queryset(
        model._base_manager.filter(pk=instance.pk),
        owner,
        record_type,
        label,
    )


def restore_entry(entry):
    snapshot = entry.snapshot
    with transaction.atomic():
        for record in snapshot['deleted']:
            model = apps.get_model(record['model'])
            if model._base_manager.filter(pk=record['pk']).exists():
                raise RestoreConflictError(
                    f'{model._meta.verbose_name} already exists.')

        for deserialized in serializers.deserialize(
            'json', json.dumps(snapshot['deleted'])):
            model = deserialized.object.__class__
            existing = model._base_manager.filter(
                pk=deserialized.object.pk).first()
            if existing is None:
                deserialized.save(force_insert=True, save_m2m=False)
            else:
                values = {
                    field.attname: getattr(deserialized.object, field.attname)
                    for field in model._meta.concrete_fields
                    if not field.primary_key
                }
                model._base_manager.filter(pk=existing.pk).update(**values)

        for update in snapshot['updated']:
            record = update['record']
            deserialized = next(serializers.deserialize(
                'json', json.dumps([record])))
            model = deserialized.object.__class__
            instance = model._base_manager.filter(
                pk=record['pk']).first()
            if instance is None:
                raise RestoreConflictError(
                    f'{model._meta.verbose_name} no longer exists.')
            for field_name in update['fields']:
                field = model._meta.get_field(field_name)
                setattr(
                    instance,
                    field.attname,
                    getattr(deserialized.object, field.attname),
                )
            instance.save(update_fields=update['fields'])

        entry.delete()


def permanently_delete_entry(entry):
    using = entry._state.db or 'default'
    files = {}
    for record in entry.snapshot['deleted']:
        model = apps.get_model(record['model'])
        for field in model._meta.concrete_fields:
            if isinstance(field, FileField):
                name = record['fields'].get(field.name)
                if name:
                    files[(id(field.storage), name)] = (field.storage, name)

    other_snapshots = RecycleBinEntry.objects.using(using).exclude(
        pk=entry.pk).values_list('snapshot', flat=True)
    for storage, name in files.values():
        referenced = False
        for model in apps.get_models():
            for field in model._meta.concrete_fields:
                if (
                    isinstance(field, FileField)
                    and field.storage == storage
                    and model._base_manager.using(using).filter(
                        **{field.name: name}).exists()
                ):
                    referenced = True
                    break
            if referenced:
                break
        if not referenced:
            for snapshot in other_snapshots:
                if _snapshot_references_file(snapshot, storage, name):
                    referenced = True
                    break
        if not referenced:
            storage.delete(name)

    with transaction.atomic():
        entry.delete()


def _snapshot_references_file(snapshot, storage, name):
    for record in snapshot['deleted']:
        model = apps.get_model(record['model'])
        for field in model._meta.concrete_fields:
            if (
                isinstance(field, FileField)
                and field.storage == storage
                and record['fields'].get(field.name) == name
            ):
                return True
    return False
