from django.urls import path

from . import views

app_name = "moonplanner"

urlpatterns = [
    path("", views.index, name="index"),
    path("add_corporation", views.add_corporation, name="add_corporation"),
    path("upload_survey", views.upload_survey, name="upload_survey"),
    path("extractions", views.extractions, name="extractions"),
    path(
        "extractions_data/<str:category>",
        views.extractions_data,
        name="extractions_data",
    ),
    path("moons", views.moons, name="moons"),
    path("moons_data/<str:category>", views.moons_data, name="moons_data"),
    path("moon/<int:moon_pk>", views.moon_details, name="moon_details"),
]
