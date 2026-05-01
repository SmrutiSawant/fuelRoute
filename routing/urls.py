from django.urls import path
from .views import RouteView, StationsView, HealthView

urlpatterns = [
    path("route/", RouteView.as_view(), name="route"),
    path("stations/", StationsView.as_view(), name="stations"),
    path("health/", HealthView.as_view(), name="health"),
]

from django.views.generic import TemplateView
urlpatterns += [
    path('', TemplateView.as_view(template_name='routing/index.html'), name='index'),
]
