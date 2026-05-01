"""
Usage:
    python manage.py import_fuel_prices path/to/fuel_prices.csv

Accepts the OPIS truckstop format:
    OPIS Truckstop ID, Truckstop Name, Address, City, State, Rack ID, Retail Price

Or the pre-processed format:
    state_code, city, lat, lon, price_per_gallon, name, address, opis_id

The command auto-detects the format, processes it, and replaces the bundled file.
"""
import csv, collections, shutil, math
from pathlib import Path
from django.core.management.base import BaseCommand, CommandError
from django.conf import settings

US_STATES = {
    'AL','AK','AZ','AR','CA','CO','CT','DE','FL','GA','HI','ID','IL','IN',
    'IA','KS','KY','LA','ME','MD','MA','MI','MN','MS','MO','MT','NE','NV',
    'NH','NJ','NM','NY','NC','ND','OH','OK','OR','PA','RI','SC','SD','TN',
    'TX','UT','VT','VA','WA','WV','WI','WY','DC'
}

STATE_CENTROIDS = {
    'AL':(32.806671,-86.79113),'AK':(61.370716,-152.404419),'AZ':(33.729759,-111.431221),
    'AR':(34.969704,-92.373123),'CA':(36.116203,-119.681564),'CO':(39.059811,-105.311104),
    'CT':(41.597782,-72.755371),'DE':(39.318523,-75.507141),'FL':(27.766279,-81.686783),
    'GA':(33.040619,-83.643074),'HI':(21.094318,-157.498337),'ID':(44.240459,-114.478828),
    'IL':(40.349457,-88.986137),'IN':(39.849426,-86.258278),'IA':(42.011539,-93.210526),
    'KS':(38.5266,-96.726486),'KY':(37.66814,-84.670067),'LA':(31.16996,-91.867805),
    'ME':(44.693947,-69.381927),'MD':(39.063946,-76.802101),'MA':(42.230171,-71.530106),
    'MI':(43.326618,-84.536095),'MN':(45.694454,-93.900192),'MS':(32.741646,-89.678696),
    'MO':(38.456085,-92.288368),'MT':(46.921925,-110.454353),'NE':(41.12537,-98.268082),
    'NV':(38.313515,-117.055374),'NH':(43.452492,-71.563896),'NJ':(40.298904,-74.521011),
    'NM':(34.840515,-106.248482),'NY':(42.165726,-74.948051),'NC':(35.630066,-79.806419),
    'ND':(47.528912,-99.784012),'OH':(40.388783,-82.764915),'OK':(35.565342,-96.928917),
    'OR':(44.572021,-122.070938),'PA':(40.590752,-77.209755),'RI':(41.680893,-71.51178),
    'SC':(33.856892,-80.945007),'SD':(44.299782,-99.438828),'TN':(35.747845,-86.692345),
    'TX':(31.054487,-97.563461),'UT':(40.150032,-111.862434),'VT':(44.045876,-72.710686),
    'VA':(37.769337,-78.169968),'WA':(47.400902,-121.490494),'WV':(38.491226,-80.954453),
    'WI':(44.268543,-89.616508),'WY':(42.755966,-107.30249),'DC':(38.897438,-77.026817),
}


def _state_spread_geocode(city, state):
    sc = STATE_CENTROIDS[state]
    h = hash(city + state) & 0xFFFF
    lat = sc[0] + ((h & 0xFF) / 255.0 - 0.5) * 3.0
    lon = sc[1] + (((h >> 8) & 0xFF) / 255.0 - 0.5) * 4.5
    return round(lat, 6), round(lon, 6)


def _detect_format(fieldnames):
    """Return 'opis' for the raw OPIS format, 'processed' for pre-geocoded."""
    fnames = {f.lower().strip() for f in fieldnames}
    if 'retail price' in fnames or 'opis truckstop id' in fnames:
        return 'opis'
    if 'price_per_gallon' in fnames and 'lat' in fnames:
        return 'processed'
    return 'unknown'


class Command(BaseCommand):
    help = "Import a fuel prices CSV (OPIS raw format or pre-processed) to replace the bundled dataset."

    def add_arguments(self, parser):
        parser.add_argument("csv_path", type=str, help="Path to your fuel prices CSV file.")
        parser.add_argument("--validate-only", action="store_true",
                            help="Validate the CSV without replacing the bundled file.")

    def handle(self, *args, **options):
        src = Path(options["csv_path"])
        if not src.exists():
            raise CommandError(f"File not found: {src}")

        with open(src, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            fmt = _detect_format(reader.fieldnames or [])
            if fmt == 'unknown':
                raise CommandError(
                    f"Unrecognized CSV format. Columns found: {reader.fieldnames}\n"
                    "Expected either OPIS format (OPIS Truckstop ID, Truckstop Name, Address, City, State, Rack ID, Retail Price)\n"
                    "or pre-processed format (state_code, city, lat, lon, price_per_gallon)."
                )
            raw_rows = list(reader)

        self.stdout.write(f"Detected format: {fmt} | Raw rows: {len(raw_rows)}")

        if fmt == 'opis':
            stations = self._process_opis(raw_rows)
        else:
            stations = self._process_prebuilt(raw_rows)

        self.stdout.write(f"✓ Processed {len(stations)} unique US truckstops/stations.")

        if not options["validate_only"]:
            dest = settings.FUEL_PRICES_CSV
            with open(dest, 'w', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=[
                    'state_code', 'city', 'lat', 'lon', 'price_per_gallon',
                    'name', 'address', 'opis_id'
                ])
                writer.writeheader()
                writer.writerows(stations)
            self.stdout.write(self.style.SUCCESS(f"✓ Installed {len(stations)} stations to {dest}"))
            self.stdout.write("Restart the server to reload station data.")

    def _process_opis(self, raw_rows):
        us_rows = []
        for row in raw_rows:
            state = row.get('State', '').strip()
            if state not in US_STATES:
                continue
            try:
                price = float(row['Retail Price'])
            except (ValueError, KeyError):
                continue
            us_rows.append({
                'opis_id': row.get('OPIS Truckstop ID', '').strip(),
                'name': row.get('Truckstop Name', '').strip(),
                'address': row.get('Address', '').strip(),
                'city': row.get('City', '').strip(),
                'state': state,
                'price': price,
            })

        # Deduplicate by OPIS ID, average price
        by_id = collections.defaultdict(list)
        for r in us_rows:
            by_id[r['opis_id']].append(r)

        stations = []
        for tid, entries in by_id.items():
            avg = sum(e['price'] for e in entries) / len(entries)
            e = entries[0]
            lat, lon = _state_spread_geocode(e['city'], e['state'])
            stations.append({
                'state_code': e['state'], 'city': e['city'],
                'lat': lat, 'lon': lon,
                'price_per_gallon': round(avg, 4),
                'name': e['name'], 'address': e['address'], 'opis_id': tid,
            })
        return stations

    def _process_prebuilt(self, raw_rows):
        stations = []
        for row in raw_rows:
            try:
                stations.append({
                    'state_code': row['state_code'].strip(),
                    'city': row['city'].strip(),
                    'lat': float(row['lat']),
                    'lon': float(row['lon']),
                    'price_per_gallon': float(row['price_per_gallon']),
                    'name': row.get('name', '').strip(),
                    'address': row.get('address', '').strip(),
                    'opis_id': row.get('opis_id', '').strip(),
                })
            except (KeyError, ValueError):
                continue
        return stations
