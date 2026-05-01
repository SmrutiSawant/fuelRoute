# FuelRoute — Smart USA Road Trip Planner

A Django REST API that plans cost-optimised fuel stops along any US road trip.

## How It Works

```
Start/Finish addresses
        │
        ▼
  Nominatim geocoding (2 lightweight calls)
        │
        ▼
  OSRM routing API (1 call — full polyline returned)
        │
        ▼
  In-memory KD-tree search over ~120 fuel stations
  (no extra API calls — pure Python spatial index)
        │
        ▼
  JSON response + interactive Leaflet map
```

### API Call Budget
| Step | Service | Calls |
|------|---------|-------|
| Geocode start | Nominatim (free) | 1 |
| Geocode finish | Nominatim (free) | 1 |
| Get route | OSRM public demo (free) | 1 |
| Find fuel stops | Local CSV + KD-tree | 0 |
| **Total external calls** | | **3** |

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Run the server

```bash
python manage.py migrate   # only needed once
python manage.py runserver
```

Open http://localhost:8000 for the interactive map UI.

---

## API Endpoints

### `POST /api/route/`

Plan a route with optimal fuel stops.

**Request body:**
```json
{
  "start": "Chicago, IL",
  "finish": "Los Angeles, CA"
}
```

**Response:**
```json
{
  "summary": {
    "start": "Chicago, IL",
    "finish": "Los Angeles, CA",
    "start_coords": { "lat": 41.88, "lon": -87.63 },
    "finish_coords": { "lat": 34.05, "lon": -118.24 },
    "total_distance_miles": 2017.4,
    "estimated_drive_time": "28h 45m",
    "total_gallons_needed": 201.74,
    "total_fuel_cost_usd": 624.32,
    "fuel_stops_count": 4,
    "vehicle_mpg": 10,
    "vehicle_max_range_miles": 500
  },
  "fuel_stops": [
    {
      "stop_number": 1,
      "city": "Kansas City",
      "state": "Missouri",
      "state_code": "MO",
      "lat": 39.09,
      "lon": -94.57,
      "price_per_gallon": 3.12,
      "gallons_purchased": 42.5,
      "cost_at_stop": 132.60,
      "miles_into_route": 492.3,
      "miles_off_route": 3.2
    }
  ],
  "route": {
    "polyline_encoded": "...",
    "coordinates": [
      { "lat": 41.88, "lon": -87.63 }
    ]
  }
}
```

### `GET /api/route/?start=Chicago,IL&finish=Los+Angeles,CA`

Same as POST but via query parameters.

### `GET /api/stations/`

Returns all fuel stations in the dataset.

Optional filter: `?state=TX`

### `GET /api/health/`

Liveness check — confirms the station data is loaded.

---

## Vehicle Assumptions

| Parameter | Value |
|-----------|-------|
| Max range | 500 miles |
| Fuel efficiency | 10 MPG |
| Refuel trigger | ~425 miles (85% of max range) |
| Optimal refuel window | 275–400 miles into each leg |

These can be changed in `fuel_route/settings.py`:
```python
VEHICLE_MAX_RANGE_MILES = 500
VEHICLE_MPG = 10
```

---

## Fuel Stop Selection Strategy

For each ~500-mile leg the algorithm:

1. **Samples waypoints** every 25 miles in the optimal refuel window (275–400 mi)
2. **Searches nearby stations** within an 80-mile radius of each waypoint using a KD-tree
3. **Picks the cheapest** station, breaking ties by proximity to the route
4. **Repeats** until the destination is reachable on remaining fuel

---

## Importing Your Own Fuel Prices

Replace the bundled CSV with your real data:

```bash
python manage.py import_fuel_prices /path/to/your_prices.csv
```

Required CSV columns:
```
state, state_code, city, lat, lon, price_per_gallon
```

Validate without replacing:
```bash
python manage.py import_fuel_prices prices.csv --validate-only
```

---

## Routing API

The project uses the **OSRM public demo server** (https://router.project-osrm.org) — completely free with no API key.

For production, self-host OSRM:
```bash
docker run -t -v "${PWD}:/data" ghcr.io/project-osrm/osrm-backend \
  osrm-extract -p /opt/car.lua /data/us-latest.osm.pbf
```

Then set the environment variable:
```bash
OSRM_BASE_URL=http://your-osrm-server python manage.py runserver
```

---

## Project Structure

```
fuel_route/          Django project config
routing/
  fuel_optimizer.py  Core algorithm: geocoding, OSRM, KD-tree, stop selection
  views.py           REST API views
  urls.py            URL routing
  fuel_prices.csv    Station dataset (replace with your data)
  management/
    commands/
      import_fuel_prices.py  CLI tool for swapping CSV data
templates/
  routing/index.html  Interactive Leaflet map UI
```
