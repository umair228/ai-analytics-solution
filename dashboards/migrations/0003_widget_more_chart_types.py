from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboards", "0002_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="widget",
            name="widget_type",
            field=models.CharField(
                choices=[
                    ("bar", "Bar chart"),
                    ("line", "Line chart"),
                    ("pie", "Pie chart"),
                    ("area", "Area chart"),
                    ("scatter", "Scatter plot"),
                    ("histogram", "Histogram"),
                    ("heatmap", "Heatmap"),
                    ("boxplot", "Box plot"),
                    ("kpi", "KPI card"),
                    ("table", "Table"),
                ],
                default="bar",
                max_length=12,
            ),
        ),
    ]
