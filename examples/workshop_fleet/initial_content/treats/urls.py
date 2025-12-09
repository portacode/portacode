from django.urls import path

from . import views

app_name = "treats"

urlpatterns = [
    path("", views.home, name="home"),
]
