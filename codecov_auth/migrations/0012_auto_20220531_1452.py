# Generated by Django 3.1.13 on 2022-05-31 14:52

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("codecov_auth", "0011_new_enterprise_plans"),
    ]

    operations = [
        migrations.AlterField(
            model_name="owner",
            name="is_superuser",
            field=models.BooleanField(default=False, null=True),
        ),
    ]