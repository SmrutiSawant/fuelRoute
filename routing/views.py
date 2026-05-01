import json
import traceback
from django.http import JsonResponse
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from .fuel_optimizer import plan_route, _load_stations


@method_decorator(csrf_exempt, name="dispatch")
class RouteView(View):
    """
    POST /api/route/
    Body: { "start": "Chicago, IL", "finish": "Los Angeles, CA" }

    GET  /api/route/?start=Chicago,IL&finish=Los+Angeles,CA
    """
    def get(self, request):
        start = request.GET.get("start", "").strip()
        finish = request.GET.get("finish", "").strip()
        return self._process(start, finish)

    def post(self, request):
        try:
            body = json.loads(request.body)
        except (json.JSONDecodeError, ValueError):
            return JsonResponse({"error": "Invalid JSON body."}, status=400)
        start = body.get("start", "").strip()
        finish = body.get("finish", "").strip()
        return self._process(start, finish)

    def _process(self, start, finish):
        if not start or not finish:
            return JsonResponse(
                {"error": "Both 'start' and 'finish' parameters are required."},
                status=400,
            )
        try:
            result = plan_route(start, finish)
            return JsonResponse(result)
        except ValueError as e:
            return JsonResponse({"error": str(e)}, status=400)
        except Exception as e:
            traceback.print_exc()
            return JsonResponse(
                {"error": "An unexpected error occurred.", "detail": str(e)},
                status=500,
            )


@method_decorator(csrf_exempt, name="dispatch")
class StationsView(View):
    """GET /api/stations/ — lists all fuel stations (filterable by ?state=TX)"""
    def get(self, request):
        _, stations = _load_stations()
        state_filter = request.GET.get("state", "").strip().upper()
        if state_filter:
            stations = [s for s in stations if s["state_code"] == state_filter]
        return JsonResponse({"count": len(stations), "stations": stations})


@method_decorator(csrf_exempt, name="dispatch")
class HealthView(View):
    """GET /api/health/ — liveness probe"""
    def get(self, request):
        _, stations = _load_stations()
        return JsonResponse({"status": "ok", "stations_loaded": len(stations)})
