from django.urls import path

from . import views

app_name = "inbox"

urlpatterns = [
    path("", views.index, name="index"),
    path("<int:client_id>/", views.thread, name="thread"),
    path("<int:client_id>/reply/", views.reply, name="reply"),
    path("requests/<int:pk>/confirm/", views.request_confirm, name="request_confirm"),
    path("requests/<int:pk>/decline/", views.request_decline, name="request_decline"),
]
