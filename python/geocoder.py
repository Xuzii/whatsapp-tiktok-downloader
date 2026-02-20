import json
import time
import urllib.parse
import urllib.request
from typing import Optional

NOMINATIM_URL = 'https://nominatim.openstreetmap.org/search'
USER_AGENT = 'whatsapp-tiktok-restaurant-mapper/1.0'

# Rate limit: 1 request per second (Nominatim policy)
_last_request_time = 0


def geocode_restaurant(location: dict) -> Optional[dict]:
    """
    Geocode a restaurant location dict to lat/lng using OpenStreetMap Nominatim.

    Args:
        location: dict with keys like city, state_or_region, country,
                  neighborhood, specific_address

    Returns:
        {"lat": float, "lng": float} or None if geocoding fails
    """
    global _last_request_time

    if not location:
        return None

    # Build address string from available fields (most specific → least)
    parts = []
    if location.get('specific_address'):
        parts.append(location['specific_address'])
    if location.get('neighborhood'):
        parts.append(location['neighborhood'])
    if location.get('city'):
        parts.append(location['city'])
    if location.get('state_or_region'):
        parts.append(location['state_or_region'])
    if location.get('country'):
        parts.append(location['country'])

    if not parts:
        return None

    query = ', '.join(parts)

    # Respect rate limit
    elapsed = time.time() - _last_request_time
    if elapsed < 1.0:
        time.sleep(1.0 - elapsed)

    params = urllib.parse.urlencode({
        'q': query,
        'format': 'json',
        'limit': 1,
    })
    url = f'{NOMINATIM_URL}?{params}'

    req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})

    try:
        _last_request_time = time.time()
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())

        if data and len(data) > 0:
            lat = float(data[0]['lat'])
            lng = float(data[0]['lon'])
            print(f'[Geocoder] "{query}" → ({lat}, {lng})')
            return {'lat': lat, 'lng': lng}
        else:
            # Try a broader query (just city + country)
            fallback_parts = []
            if location.get('city'):
                fallback_parts.append(location['city'])
            if location.get('country'):
                fallback_parts.append(location['country'])

            if fallback_parts and fallback_parts != parts:
                fallback_query = ', '.join(fallback_parts)
                print(f'[Geocoder] No result for "{query}", trying "{fallback_query}"...')

                time.sleep(1.0)  # Rate limit
                params = urllib.parse.urlencode({
                    'q': fallback_query,
                    'format': 'json',
                    'limit': 1,
                })
                fallback_url = f'{NOMINATIM_URL}?{params}'
                req = urllib.request.Request(fallback_url, headers={'User-Agent': USER_AGENT})

                _last_request_time = time.time()
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read().decode())

                if data and len(data) > 0:
                    lat = float(data[0]['lat'])
                    lng = float(data[0]['lon'])
                    print(f'[Geocoder] Fallback "{fallback_query}" → ({lat}, {lng})')
                    return {'lat': lat, 'lng': lng}

            print(f'[Geocoder] No results for "{query}"')
            return None

    except Exception as e:
        print(f'[Geocoder] Error geocoding "{query}": {e}')
        return None
