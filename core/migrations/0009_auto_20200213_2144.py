# Generated by Django 2.1.3 on 2020-02-13 21:44

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0008_repository_activated'),
    ]

    operations = [
        migrations.AlterField(
            model_name='branch',
            name='head',
            field=models.TextField(),
        ),
        migrations.AlterField(
            model_name='pull',
            name='base',
            field=models.TextField(null=True),
        ),
        migrations.AlterField(
            model_name='pull',
            name='compared_to',
            field=models.TextField(null=True),
        ),
        migrations.AlterField(
            model_name='pull',
            name='head',
            field=models.TextField(null=True),
        ),
    ]