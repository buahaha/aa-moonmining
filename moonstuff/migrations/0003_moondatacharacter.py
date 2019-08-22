# Generated by Django 2.2.3 on 2019-08-22 03:53

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('eveonline', '0010_alliance_ticker'),
        ('moonstuff', '0002_auto_20181217_1814'),
    ]

    operations = [
        migrations.CreateModel(
            name='MoonDataCharacter',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('latest_notification', models.BigIntegerField(default=0, null=True)),
                ('character', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='eveonline.EveCharacter')),
            ],
        ),
    ]
