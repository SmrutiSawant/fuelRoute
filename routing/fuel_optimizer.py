"""
Fuel stop optimizer.

Strategy:
  1. Parse route geometry from OSRM (single API call).
  2. Sample waypoints every ~50 miles along the polyline.
  3. For each mandatory refuel window (every ~400–500 miles), find the
     cheapest station near those waypoints using a KD-tree spatial index
     built once at startup from the CSV.
  4. Return stops, map data, and cost summary.
"""

import csv
import math
import threading
from functools import lru_cache
from pathlib import Path

import requests
from django.conf import settings


# ---------------------------------------------------------------------------
# Haversine distance helpers
# ---------------------------------------------------------------------------

def haversine(lat1, lon1, lat2, lon2):
    """Return distance in miles between two lat/lon points."""
    R = 3958.8  # Earth radius in miles
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


# ---------------------------------------------------------------------------
# Simple KD-tree built from fuel station list for fast nearest-neighbour
# ---------------------------------------------------------------------------

class _KDNode:
    __slots__ = ("point", "left", "right")

    def __init__(self, point, left=None, right=None):
        self.point = point  # dict with lat, lon, + price fields
        self.left = left
        self.right = right


def _build_kd(points, depth=0):
    if not points:
        return None
    axis = depth % 2  # 0 = lat, 1 = lon
    key = "lat" if axis == 0 else "lon"
    points.sort(key=lambda p: p[key])
    mid = len(points) // 2
    return _KDNode(
        points[mid],
        _build_kd(points[:mid], depth + 1),
        _build_kd(points[mid + 1:], depth + 1),
    )


def _kd_nearest(node, target_lat, target_lon, depth=0, best=None):
    if node is None:
        return best
    pt = node.point
    dist = haversine(target_lat, target_lon, pt["lat"], pt["lon"])
    if best is None or dist < best[0]:
        best = (dist, pt)
    axis = depth % 2
    if axis == 0:
        diff = target_lat - pt["lat"]
    else:
        diff = target_lon - pt["lon"]
    near, far = (node.left, node.right) if diff <= 0 else (node.right, node.left)
    best = _kd_nearest(near, target_lat, target_lon, depth + 1, best)
    # Check if far branch could contain something closer
    if abs(diff) * (3958.8 if axis == 0 else 3958.8 * math.cos(math.radians(target_lat))) < best[0]:
        best = _kd_nearest(far, target_lat, target_lon, depth + 1, best)
    return best


def _kd_within(node, target_lat, target_lon, radius_miles, depth=0, results=None):
    """Return all stations within radius_miles of target."""
    if results is None:
        results = []
    if node is None:
        return results
    pt = node.point
    dist = haversine(target_lat, target_lon, pt["lat"], pt["lon"])
    if dist <= radius_miles:
        results.append((dist, pt))
    axis = depth % 2
    if axis == 0:
        diff = target_lat - pt["lat"]
        deg_per_mile = 1 / 69.0
    else:
        diff = target_lon - pt["lon"]
        deg_per_mile = 1 / (69.0 * math.cos(math.radians(target_lat)))
    near, far = (node.left, node.right) if diff <= 0 else (node.right, node.left)
    _kd_within(near, target_lat, target_lon, radius_miles, depth + 1, results)
    if abs(diff) * (1 / deg_per_mile) < radius_miles:
        _kd_within(far, target_lat, target_lon, radius_miles, depth + 1, results)
    return results


# ---------------------------------------------------------------------------
# Station data loader (lazy, singleton)
# ---------------------------------------------------------------------------

_station_lock = threading.Lock()
_station_tree = None
_station_list = None


def _load_stations():
    global _station_tree, _station_list
    with _station_lock:
        if _station_tree is not None:
            return _station_tree, _station_list
        csv_path = settings.FUEL_PRICES_CSV
        stations = []
        with open(csv_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    # Support both old format (state full name) and new format (state_code only)
                    state_code = row.get("state_code", row.get("state", "")).strip()
                    state_name = row.get("state", state_code).strip()
                    stations.append({
                        "state": state_name,
                        "state_code": state_code,
                        "city": row["city"].strip(),
                        "lat": float(row["lat"]),
                        "lon": float(row["lon"]),
                        "price": float(row["price_per_gallon"]),
                        "name": row.get("name", "").strip(),
                        "address": row.get("address", "").strip(),
                        "opis_id": row.get("opis_id", "").strip(),
                    })
                except (KeyError, ValueError):
                    continue
        _station_list = stations
        _station_tree = _build_kd(list(stations))  # copy so sort doesn't mutate original
        return _station_tree, _station_list


# ---------------------------------------------------------------------------
# Polyline decoder (Google Encoded Polyline Algorithm)
# ---------------------------------------------------------------------------

def decode_polyline(encoded: str):
    """Decode an encoded polyline string to list of (lat, lon) tuples."""
    coords = []
    index = 0
    lat = 0
    lon = 0
    while index < len(encoded):
        for is_lon in (False, True):
            shift = 0
            result = 0
            while True:
                b = ord(encoded[index]) - 63
                index += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            delta = ~(result >> 1) if (result & 1) else (result >> 1)
            if is_lon:
                lon += delta
            else:
                lat += delta
        coords.append((lat / 1e5, lon / 1e5))
    return coords


# ---------------------------------------------------------------------------
# Cumulative distance along a polyline
# ---------------------------------------------------------------------------

def _cumulative_distances(coords):
    """Return list of cumulative distances (miles) for each coord."""
    cum = [0.0]
    for i in range(1, len(coords)):
        d = haversine(coords[i - 1][0], coords[i - 1][1], coords[i][0], coords[i][1])
        cum.append(cum[-1] + d)
    return cum


def _interpolate_at_distance(coords, cum_dists, target_miles):
    """Return (lat, lon) at exactly target_miles along the polyline."""
    if target_miles <= 0:
        return coords[0]
    if target_miles >= cum_dists[-1]:
        return coords[-1]
    lo, hi = 0, len(cum_dists) - 1
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if cum_dists[mid] < target_miles:
            lo = mid
        else:
            hi = mid
    seg_len = cum_dists[hi] - cum_dists[lo]
    if seg_len == 0:
        return coords[lo]
    frac = (target_miles - cum_dists[lo]) / seg_len
    lat = coords[lo][0] + frac * (coords[hi][0] - coords[lo][0])
    lon = coords[lo][1] + frac * (coords[hi][1] - coords[lo][1])
    return (lat, lon)


# ---------------------------------------------------------------------------
# Geocoding via Nominatim (free, no key required) — used only for start/end
# ---------------------------------------------------------------------------

# Static fallback: major US cities for when Nominatim is unavailable
_CITY_FALLBACK = {
    "new york": (40.7128, -74.0060), "new york city": (40.7128, -74.0060),
    "nyc": (40.7128, -74.0060), "los angeles": (34.0522, -118.2437),
    "la": (34.0522, -118.2437), "chicago": (41.8781, -87.6298),
    "houston": (29.7604, -95.3698), "phoenix": (33.4484, -112.0740),
    "philadelphia": (39.9526, -75.1652), "san antonio": (29.4241, -98.4936),
    "san diego": (32.7157, -117.1611), "dallas": (32.7767, -96.7970),
    "san jose": (37.3382, -121.8863), "austin": (30.2672, -97.7431),
    "jacksonville": (30.3322, -81.6557), "fort worth": (32.7555, -97.3308),
    "columbus": (39.9612, -82.9988), "charlotte": (35.2271, -80.8431),
    "indianapolis": (39.7684, -86.1581), "san francisco": (37.7749, -122.4194),
    "seattle": (47.6062, -122.3321), "denver": (39.7392, -104.9903),
    "nashville": (36.1627, -86.7816), "oklahoma city": (35.4676, -97.5164),
    "el paso": (31.7619, -106.4850), "washington": (38.9072, -77.0369),
    "washington dc": (38.9072, -77.0369), "dc": (38.9072, -77.0369),
    "las vegas": (36.1699, -115.1398), "louisville": (38.2527, -85.7585),
    "memphis": (35.1495, -90.0490), "portland": (45.5051, -122.6750),
    "baltimore": (39.2904, -76.6122), "milwaukee": (43.0389, -87.9065),
    "albuquerque": (35.0844, -106.6504), "tucson": (32.2226, -110.9747),
    "fresno": (36.7378, -119.7871), "sacramento": (38.5816, -121.4944),
    "kansas city": (39.0997, -94.5786), "mesa": (33.4152, -111.8315),
    "atlanta": (33.7490, -84.3880), "omaha": (41.2565, -95.9345),
    "colorado springs": (38.8339, -104.8214), "raleigh": (35.7796, -78.6382),
    "long beach": (33.7701, -118.1937), "virginia beach": (36.8529, -75.9780),
    "minneapolis": (44.9778, -93.2650), "tampa": (27.9506, -82.4572),
    "new orleans": (29.9511, -90.0715), "cleveland": (41.4993, -81.6944),
    "honolulu": (21.3069, -157.8583), "wichita": (37.6872, -97.3301),
    "arlington": (32.7357, -97.1081), "bakersfield": (35.3733, -119.0187),
    "aurora": (39.7294, -104.8319), "anaheim": (33.8366, -117.9143),
    "santa ana": (33.7455, -117.8677), "corpus christi": (27.8006, -97.3964),
    "riverside": (33.9806, -117.3755), "st. louis": (38.6270, -90.1994),
    "saint louis": (38.6270, -90.1994), "lexington": (38.0406, -84.5037),
    "pittsburgh": (40.4406, -79.9959), "stockton": (37.9577, -121.2908),
    "anchorage": (61.2181, -149.9003), "cincinnati": (39.1031, -84.5120),
    "st paul": (44.9537, -93.0900), "saint paul": (44.9537, -93.0900),
    "toledo": (41.6639, -83.5552), "greensboro": (36.0726, -79.7920),
    "newark": (40.7357, -74.1724), "plano": (33.0198, -96.6989),
    "henderson": (36.0395, -114.9817), "lincoln": (40.8136, -96.7026),
    "buffalo": (42.8864, -78.8784), "fort wayne": (41.0793, -85.1394),
    "jersey city": (40.7178, -74.0431), "chula vista": (32.6401, -117.0842),
    "orlando": (28.5383, -81.3792), "st. petersburg": (27.7676, -82.6403),
    "norfolk": (36.8508, -76.2859), "chandler": (33.3062, -111.8413),
    "laredo": (27.5306, -99.4803), "madison": (43.0731, -89.4012),
    "durham": (35.9940, -78.8986), "lubbock": (33.5779, -101.8552),
    "winston-salem": (36.0999, -80.2442), "garland": (32.9126, -96.6389),
    "glendale": (33.5387, -112.1860), "hialeah": (25.8576, -80.2781),
    "reno": (39.5296, -119.8138), "baton rouge": (30.4515, -91.1871),
    "irvine": (33.6846, -117.8265), "chesapeake": (36.7682, -76.2875),
    "scottsdale": (33.4942, -111.9261), "north las vegas": (36.1989, -115.1175),
    "fremont": (37.5483, -121.9886), "gilbert": (33.3528, -111.7890),
    "san bernardino": (34.1083, -117.2898), "boise": (43.6150, -116.2023),
    "birmingham": (33.5186, -86.8104), "rochester": (43.1566, -77.6088),
    "richmond": (37.5407, -77.4360), "spokane": (47.6588, -117.4260),
    "des moines": (41.5868, -93.6250), "montgomery": (32.3668, -86.2999),
    "modesto": (37.6391, -120.9969), "fayetteville": (36.0626, -94.1574),
    "tacoma": (47.2529, -122.4443), "shreveport": (32.5252, -93.7502),
    "akron": (41.0814, -81.5190), "aurora co": (39.7294, -104.8319),
    "oxnard": (34.1975, -119.1771), "fontana": (34.0922, -117.4350),
    "moreno valley": (33.9425, -117.2297), "glendale ca": (34.1425, -118.2551),
    "huntington beach": (33.6595, -117.9988), "salt lake city": (40.7608, -111.8910),
    "slc": (40.7608, -111.8910), "grand rapids": (42.9634, -85.6681),
    "knoxville": (35.9606, -83.9207), "worcester": (42.2626, -71.8023),
    "newport news": (37.0871, -76.4730), "brownsville": (25.9017, -97.4975),
    "santa clarita": (34.3917, -118.5426), "providence": (41.8240, -71.4128),
    "garden grove": (33.7739, -117.9414), "oceanside": (33.1959, -117.3795),
    "chattanooga": (35.0456, -85.3097), "fort lauderdale": (26.1224, -80.1373),
    "rancho cucamonga": (34.1064, -117.5931), "santa rosa": (38.4404, -122.7141),
    "ontario": (34.0633, -117.6509), "elk grove": (38.4088, -121.3716),
    "corona": (33.8753, -117.5664), "clarksville": (36.5298, -87.3595),
    "eugene": (44.0521, -123.0868), "peoria": (40.6936, -89.5889),
    "springfield": (39.7817, -89.6501), "columbia": (34.0007, -81.0348),
    "little rock": (34.7465, -92.2896), "jackson": (32.2988, -90.1848),
    "hartford": (41.7658, -72.6851), "boston": (42.3601, -71.0589),
    "miami": (25.7617, -80.1918), "detroit": (42.3314, -83.0458),
}

def _fuzzy_lookup(place: str):
    """Try to find a city from our static fallback dict."""
    # Normalize: strip state suffix (e.g. "Chicago, IL" -> "chicago")
    key = place.lower().strip()
    # Try exact match
    if key in _CITY_FALLBACK:
        return _CITY_FALLBACK[key]
    # Strip state code: "Chicago, IL" -> "chicago"
    if "," in key:
        city_part = key.split(",")[0].strip()
        if city_part in _CITY_FALLBACK:
            return _CITY_FALLBACK[city_part]
    # Partial match
    for known_city, coords in _CITY_FALLBACK.items():
        if known_city in key or key in known_city:
            return coords
    return None


def geocode(place: str):
    """Return (lat, lon) for a place name. Raises ValueError if not found.
    
    Tries Nominatim (OSM) first; falls back to built-in city database if the
    external request fails (e.g. network restrictions in some environments).
    """
    # 1. Try Nominatim
    try:
        url = "https://nominatim.openstreetmap.org/search"
        params = {
            "q": place + ", USA",
            "format": "json",
            "limit": 1,
            "countrycodes": "us",
        }
        headers = {"User-Agent": "FuelRouteAPI/1.0 (educational-project)"}
        resp = requests.get(url, params=params, headers=headers, timeout=8)
        if resp.status_code == 200:
            results = resp.json()
            if results:
                r = results[0]
                return float(r["lat"]), float(r["lon"])
    except Exception:
        pass  # fall through to static lookup

    # 2. Fall back to static city database
    coords = _fuzzy_lookup(place)
    if coords:
        return coords

    raise ValueError(
        f"Could not geocode '{place}' within the USA. "
        "Please use a well-known US city name (e.g. 'Chicago, IL')."
    )


# ---------------------------------------------------------------------------
# OSRM route fetch (single call)
# ---------------------------------------------------------------------------

def fetch_osrm_route(start_lat, start_lon, end_lat, end_lon):
    """
    Call OSRM once and return route geometry + metadata.
    Falls back to a straight-line approximation if OSRM is unreachable
    (e.g. in network-restricted environments or self-hosted setups not yet running).

    Return structure:
      {
        "geometry": [(lat, lon), ...],
        "distance_meters": float,
        "duration_seconds": float,
        "polyline_encoded": str,
        "_fallback": bool,   # True only when OSRM was unavailable
      }
    """
    base = settings.OSRM_BASE_URL.rstrip("/")
    url = (
        f"{base}/route/v1/driving/"
        f"{start_lon},{start_lat};{end_lon},{end_lat}"
        f"?overview=full&geometries=polyline&steps=false"
    )
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != "Ok" or not data.get("routes"):
            raise ValueError("OSRM returned no routes.")
        route = data["routes"][0]
        encoded = route["geometry"]
        coords = decode_polyline(encoded)
        return {
            "geometry": coords,
            "distance_meters": route["distance"],
            "duration_seconds": route["duration"],
            "polyline_encoded": encoded,
            "_fallback": False,
        }
    except Exception:
        # Graceful degradation: use straight-line approximation
        return _great_circle_route(start_lat, start_lon, end_lat, end_lon)


# ---------------------------------------------------------------------------
# Fuel stop optimizer
# ---------------------------------------------------------------------------

SEARCH_RADIUS_MILES = 80   # how far off-route we'll look for a station
MAX_RANGE = settings.VEHICLE_MAX_RANGE_MILES          # 500
SAFE_RANGE = int(MAX_RANGE * 0.85)                    # 425 — refuel before empty
OPTIMAL_FILL_WINDOW = (int(MAX_RANGE * 0.55), int(MAX_RANGE * 0.80))  # 275–400 mi


def find_optimal_fuel_stops(coords, total_miles):
    """
    Given the route polyline and total distance:
      - Determine mandatory refuel checkpoints (every SAFE_RANGE miles).
      - For each checkpoint window, find the cheapest station nearby.
      - Return an ordered list of stop dicts.
    """
    tree, _ = _load_stations()
    cum = _cumulative_distances(coords)

    stops = []
    miles_since_last_fill = 0.0
    current_mile = 0.0
    station_ids_used = set()  # avoid duplicate stops

    while current_mile < total_miles:
        # When do we NEED to refuel?
        must_refuel_by = current_mile + SAFE_RANGE
        # Optimal window: start looking a bit before we must
        ideal_start = current_mile + OPTIMAL_FILL_WINDOW[0]
        ideal_end = current_mile + OPTIMAL_FILL_WINDOW[1]

        if must_refuel_by >= total_miles:
            break  # we can make it to the destination

        # Sample waypoints every 25 miles in the search window
        search_start = max(current_mile + 50, ideal_start - 50)
        search_end = min(ideal_end + 50, must_refuel_by, total_miles)

        candidates = []
        sample_step = 25
        m = search_start
        while m <= search_end:
            pt = _interpolate_at_distance(coords, cum, m)
            nearby = _kd_within(tree, pt[0], pt[1], SEARCH_RADIUS_MILES)
            for dist_off_route, station in nearby:
                sid = f"{station['city']}-{station['state_code']}"
                if sid not in station_ids_used:
                    candidates.append((station["price"], dist_off_route, m, station))
            m += sample_step

        if not candidates:
            # Fallback: nearest station to the must-refuel point
            pt = _interpolate_at_distance(coords, cum, must_refuel_by - 30)
            result = _kd_nearest(tree, pt[0], pt[1])
            if result:
                _, station = result
                candidates.append((station["price"], result[0], must_refuel_by - 30, station))

        if not candidates:
            raise ValueError("No fuel stations found along this route segment.")

        # Pick cheapest station; break ties by distance off-route
        candidates.sort(key=lambda x: (x[0], x[1]))
        best_price, best_off, best_at_mile, best_station = candidates[0]

        sid = f"{best_station['city']}-{best_station['state_code']}"
        station_ids_used.add(sid)

        # How many gallons do we need at this stop?
        # We fill up enough to reach the next cheapest window or destination
        fill_miles = min(MAX_RANGE, total_miles - best_at_mile)
        gallons = fill_miles / settings.VEHICLE_MPG
        cost = gallons * best_price

        stops.append({
            "stop_number": len(stops) + 1,
            "truckstop_name": best_station.get("name", ""),
            "address": best_station.get("address", ""),
            "city": best_station["city"],
            "state": best_station["state"],
            "state_code": best_station["state_code"],
            "opis_id": best_station.get("opis_id", ""),
            "lat": best_station["lat"],
            "lon": best_station["lon"],
            "price_per_gallon": round(best_price, 3),
            "gallons_purchased": round(gallons, 2),
            "cost_at_stop": round(cost, 2),
            "miles_into_route": round(best_at_mile, 1),
            "miles_off_route": round(best_off, 1),
        })

        current_mile = best_at_mile

    return stops


# ---------------------------------------------------------------------------
# Main public function
# ---------------------------------------------------------------------------

def plan_route(start: str, finish: str):
    """
    Full pipeline:
      1. Geocode start & finish (2 Nominatim calls, lightweight).
      2. Fetch OSRM route (1 routing API call).
      3. Compute fuel stops from cached station data (no extra API calls).
      4. Return structured response dict.
    """
    start_lat, start_lon = geocode(start)
    end_lat, end_lon = geocode(finish)

    route_data = fetch_osrm_route(start_lat, start_lon, end_lat, end_lon)
    coords = route_data["geometry"]
    total_miles = route_data["distance_meters"] / 1609.344

    mpg = settings.VEHICLE_MPG
    fuel_stops = find_optimal_fuel_stops(coords, total_miles)

    # Total fuel cost = cost of all stops + initial tank at start
    # We assume the driver starts with an empty tank.
    total_gallons = total_miles / mpg
    total_cost = sum(s["cost_at_stop"] for s in fuel_stops)

    # If no stops were needed (short trip), still cost fuel
    if not fuel_stops:
        # Just need to know rough price at origin
        tree, _ = _load_stations()
        result = _kd_nearest(tree, start_lat, start_lon)
        if result:
            _, nearest = result
            start_price = nearest["price"]
        else:
            start_price = 3.20  # national average fallback
        gallons_needed = total_miles / mpg
        total_cost = gallons_needed * start_price
        total_gallons = gallons_needed

    hours = int(route_data["duration_seconds"] // 3600)
    minutes = int((route_data["duration_seconds"] % 3600) // 60)

    # Build a simplified polyline for mapping: sample ~200 points
    coords_count = len(coords)
    step = max(1, coords_count // 200)
    sampled_coords = coords[::step]
    if coords[-1] not in sampled_coords:
        sampled_coords.append(coords[-1])

    routing_mode = "approximate" if route_data.get("_fallback") else "osrm"

    return {
        "summary": {
            "start": start,
            "finish": finish,
            "start_coords": {"lat": start_lat, "lon": start_lon},
            "finish_coords": {"lat": end_lat, "lon": end_lon},
            "total_distance_miles": round(total_miles, 1),
            "estimated_drive_time": f"{hours}h {minutes}m",
            "total_gallons_needed": round(total_gallons, 2),
            "total_fuel_cost_usd": round(total_cost, 2),
            "fuel_stops_count": len(fuel_stops),
            "vehicle_mpg": mpg,
            "vehicle_max_range_miles": settings.VEHICLE_MAX_RANGE_MILES,
            "routing_mode": routing_mode,
        },
        "fuel_stops": fuel_stops,
        "route": {
            "polyline_encoded": route_data["polyline_encoded"],
            "coordinates": [{"lat": c[0], "lon": c[1]} for c in sampled_coords],
            "routing_mode": routing_mode,
        },
    }


# ---------------------------------------------------------------------------
# Mock OSRM for offline / network-restricted environments
# ---------------------------------------------------------------------------

def _great_circle_route(start_lat, start_lon, end_lat, end_lon, n_points=120):
    """
    Generate a synthetic route between two points via linear interpolation.
    Used as fallback when OSRM is unreachable.
    Returns the same structure as fetch_osrm_route().
    """
    coords = [
        (
            start_lat + (end_lat - start_lat) * i / (n_points - 1),
            start_lon + (end_lon - start_lon) * i / (n_points - 1),
        )
        for i in range(n_points)
    ]
    # Estimate distance (straight-line, ~15% road penalty)
    dist_miles = haversine(start_lat, start_lon, end_lat, end_lon) * 1.15
    dist_meters = dist_miles * 1609.344
    # Estimate duration at ~55 mph average
    duration_seconds = (dist_miles / 55) * 3600

    # Build a simple encoded polyline (plain lat/lon pairs stored as JSON-safe list)
    # We'll encode it with the standard algorithm
    encoded = _encode_polyline(coords)

    return {
        "geometry": coords,
        "distance_meters": dist_meters,
        "duration_seconds": duration_seconds,
        "polyline_encoded": encoded,
        "_fallback": True,
    }


def _encode_polyline(coords):
    """Encode a list of (lat, lon) tuples to Google Encoded Polyline format."""
    output = []
    prev_lat = prev_lon = 0

    def _encode_value(val):
        val = int(round(val * 1e5))
        val = val << 1
        if val < 0:
            val = ~val
        chunks = []
        while val >= 0x20:
            chunks.append(chr((0x20 | (val & 0x1f)) + 63))
            val >>= 5
        chunks.append(chr(val + 63))
        return ''.join(chunks)

    for lat, lon in coords:
        output.append(_encode_value(lat - prev_lat))
        output.append(_encode_value(lon - prev_lon))
        prev_lat, prev_lon = lat, lon

    return ''.join(output)
