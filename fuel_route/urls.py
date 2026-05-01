from django.urls import path, include

urlpatterns = [
    path('api/', include('routing.urls')),
    path('', include('routing.urls')),
]
