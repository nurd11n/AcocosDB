from django.urls import path

from . import views

app_name = "notes"

urlpatterns = [
    path("", views.index, name="index"),
    path("<int:pk>/toggle/", views.toggle, name="toggle"),
    path("<int:pk>/pin/", views.pin, name="pin"),
    path("<int:pk>/edit/", views.edit, name="edit"),
    path("<int:pk>/delete/", views.delete, name="delete"),
]
