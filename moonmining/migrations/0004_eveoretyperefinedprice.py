# Generated by Django 3.1.6 on 2021-04-09 16:30

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("moonmining", "0003_miningledgerrecord_user"),
    ]

    operations = [
        migrations.CreateModel(
            name="EveOreTypeRefinedPrice",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("value", models.FloatField(default=None, null=True)),
                (
                    "ore_type",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="refined_price",
                        to="moonmining.eveoretype",
                    ),
                ),
            ],
        ),
    ]