from django.apps import AppConfig

class RoutingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "routing"

    def ready(self):
        # Pre-load station data into memory on startup for fast responses
        from .fuel_optimizer import _load_stations
        _load_stations()
