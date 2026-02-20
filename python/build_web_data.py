#!/usr/bin/env python3
"""
Combine all analysis JSON files from GCS into a single restaurants.json.
Uploads the result back to GCS for the public website.

Usage: python3 build_web_data.py
"""
from gcs_utils import download_json, list_blobs, upload_json


def build():
    all_restaurants = []

    # List all analysis JSON files in GCS
    analysis_blobs = [
        b for b in list_blobs('analysis_results/')
        if b.endswith('_analysis.json')
    ]

    for blob_name in sorted(analysis_blobs):
        try:
            data = download_json(blob_name)
        except Exception as e:
            print(f'Skipping {blob_name}: {e}')
            continue

        if not data:
            continue

        tiktok_url = data.get('tiktok_url', '')
        title = data.get('title', '')
        uploader = data.get('uploader', '')

        for restaurant in data.get('restaurants', []):
            # Only include restaurants with valid coordinates
            lat = restaurant.get('lat')
            lng = restaurant.get('lng')
            if lat is None or lng is None:
                continue

            all_restaurants.append({
                'name': restaurant.get('restaurant_name', 'Unknown'),
                'cuisine': restaurant.get('cuisine_type', ''),
                'confidence': restaurant.get('confidence', ''),
                'location': restaurant.get('location', {}),
                'lat': lat,
                'lng': lng,
                'dishes': restaurant.get('dishes_shown', []),
                'food_images': restaurant.get('food_images', []),
                'rating': restaurant.get('creator_rating_or_opinion', ''),
                'notes': restaurant.get('notes', ''),
                'tiktok_url': tiktok_url,
                'video_title': title,
                'uploader': uploader,
            })

    upload_json(all_restaurants, 'restaurants.json')
    print(f'Built restaurants.json with {len(all_restaurants)} restaurant(s)')


if __name__ == '__main__':
    build()
