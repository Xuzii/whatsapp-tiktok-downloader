import json
import os
import subprocess
import time
from pathlib import Path

from google import genai

# ============================================================
# CONFIGURATION
# ============================================================
MODEL = 'gemini-2.5-flash'

# ============================================================
# RESTAURANT ANALYSIS PROMPT
# ============================================================
RESTAURANT_PROMPT = """You are a food content analyst specializing in extracting restaurant information from TikTok videos.
You will receive a TikTok video (with frames/screenshots) and its associated metadata. Your job is to identify EVERY restaurant featured and its location by analyzing ALL available signals. A single TikTok may feature multiple restaurants (e.g. "Top 5 spots in Austin" or "food crawl" style videos). You must identify and return data for EACH restaurant separately.
## What to Analyze
**Visual Signals:**
- Restaurant signage, logos, or branding visible in any frame
- Menu boards, receipts, or branded packaging
- Interior/exterior design cues (recognizable decor, chains, unique features)
- Plating style, dish presentation, and cuisine type
- Street signs, landmarks, or surroundings visible outside
- Scene transitions that indicate a change to a different restaurant
**Audio/Transcript Signals:**
- Any spoken mention of restaurant names, neighborhoods, cities, or addresses
- Creator saying "we're at..." or "this place is in..." or "next spot..."
- Numbered lists or rankings spoken aloud ("number 3 is...")
- Background audio that might include location cues
**Text Overlay Signals:**
- On-screen text, captions, or hashtags naming restaurants or locations
- Numbered rankings or lists displayed on screen
- Tagged locations or geotags in metadata
- Watermarks or creator handles that might indicate city/region
**Metadata Signals:**
- Video description, hashtags, tagged location
- Creator profile info (often city-based food bloggers)
- Comments or engagement context if provided
## How to Handle Multiple Restaurants
Pay close attention to:
- Scene changes, cuts, or transitions that signal a new location
- Numbered lists or rankings (verbal or on-screen)
- Changes in interior/exterior environment, lighting, or tableware
- Phrases like "next up", "another spot", "stop number 2", etc.
Each distinct restaurant gets its own entry in the output array.
## Output Format
ALWAYS return a JSON array, even if only one restaurant is found:
[
  {
    "order_in_video": 1,
    "restaurant_name": "Name or 'Unknown' if not identifiable",
    "confidence": "high | medium | low",
    "cuisine_type": "e.g. Korean BBQ, Mexican, Italian",
    "location": {
      "city": "",
      "state_or_region": "",
      "country": "",
      "neighborhood": "",
      "specific_address": ""
    },
    "dishes_shown": ["list of identifiable dishes"],
    "food_shot_timestamps": ["MM:SS", "MM:SS"],
    "creator_rating_or_opinion": "What the creator said about this spot, if anything",
    "evidence": [
      "Brief explanation of each signal that led to your conclusion"
    ],
    "notes": "Any caveats, ambiguities, or suggestions for verification"
  }
]
## Rules
- Return one object per restaurant. Never combine multiple restaurants into one entry.
- Fill in as much as you can confidently determine. Leave fields empty rather than guess.
- If you detect a scene change but cannot identify the restaurant, still include an entry with "Unknown" and whatever partial info you gathered.
- order_in_video should reflect the sequence restaurants appear in the video.
- Include ANY restaurant, cafe, bar, food stall, or food establishment that appears in the video — even if the video's primary topic is something else (e.g. a travel vlog that visits a restaurant, a "day in my life" that includes a cafe scene, a group outing that includes dinner at a restaurant).
- Only return an empty array [] if there is genuinely ZERO restaurant or food establishment content in the video (e.g. pure gaming, makeup tutorial, home cooking with no restaurant involved, fitness content).
- For each restaurant, identify 1-3 timestamps (MM:SS format) when the food is most clearly and appetizingly visible on screen. Pick the best food shots — close-ups of dishes, plated food, or food being served. If no clear food shot exists, return an empty array for that restaurant."""


def extract_food_frames(video_path: Path, video_id: str, restaurants: list) -> list:
    """
    Extract food shot frames from the video using ffmpeg at Gemini-provided timestamps.
    Writes JPEGs to /tmp, uploads to GCS, deletes local copy.
    Adds 'food_images' list to each restaurant entry.
    """
    from gcs_utils import upload_file

    for i, restaurant in enumerate(restaurants):
        timestamps = restaurant.get('food_shot_timestamps', [])
        food_images = []

        for j, ts in enumerate(timestamps):
            # Validate MM:SS format
            parts = ts.split(':')
            if len(parts) != 2:
                continue
            try:
                mins, secs = int(parts[0]), int(parts[1])
            except ValueError:
                continue

            # Convert to HH:MM:SS for ffmpeg
            ffmpeg_ts = f'00:{mins:02d}:{secs:02d}'
            out_name = f'{video_id}_{i + 1}_{j + 1}.jpg'
            out_path = Path('/tmp') / out_name

            try:
                subprocess.run(
                    [
                        'ffmpeg', '-ss', ffmpeg_ts, '-i', str(video_path),
                        '-vframes', '1', '-q:v', '2', '-y', str(out_path),
                    ],
                    capture_output=True, timeout=15,
                )
                if out_path.exists() and out_path.stat().st_size > 0:
                    upload_file(str(out_path), f'analysis_results/{out_name}', content_type='image/jpeg')
                    out_path.unlink()  # Remove local copy
                    food_images.append(out_name)
                    print(f'[ffmpeg] Extracted frame → {out_name}')
                else:
                    print(f'[ffmpeg] No frame produced for {ts}')
            except Exception as e:
                print(f'[ffmpeg] Error extracting frame at {ts}: {e}')

        restaurant['food_images'] = food_images

    return restaurants


def _get_client():
    """Initialize Gemini client with API key from env."""
    api_key = os.environ.get('GEMINI_API_KEY', '')
    if not api_key:
        # Try loading from .env file
        env_path = Path(__file__).parent.parent / '.env'
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith('GEMINI_API_KEY='):
                    api_key = line.split('=', 1)[1].strip()
                    break
    if not api_key:
        raise RuntimeError('GEMINI_API_KEY not set. Add it to .env or environment.')
    return genai.Client(api_key=api_key)


def analyze_video(video_path: str, url: str = '', metadata: dict = None) -> list:
    """
    Analyze a TikTok video for restaurant content using Gemini.

    Args:
        video_path: Path to the .mp4 file
        url: Original TikTok URL (included in saved output)
        metadata: yt-dlp metadata dict (title, description, uploader, tags) — sent to Gemini for extra context

    Returns:
        List of restaurant dicts (empty list if not restaurant-related)
    """
    video_path = Path(video_path)
    if not video_path.exists():
        raise FileNotFoundError(f'Video not found: {video_path}')

    video_id = video_path.stem  # e.g. "7607882255670840590"

    client = _get_client()

    # Upload video to Gemini Files API
    print(f'[Gemini] Uploading {video_path.name}...')
    uploaded_file = client.files.upload(file=str(video_path))

    # Wait for file to become ACTIVE (video processing takes a few seconds)
    while uploaded_file.state == 'PROCESSING':
        print(f'[Gemini] File still processing, waiting...')
        time.sleep(3)
        uploaded_file = client.files.get(name=uploaded_file.name)

    if uploaded_file.state != 'ACTIVE':
        raise RuntimeError(f'File upload failed with state: {uploaded_file.state}')

    # Build prompt with metadata context if available
    prompt = RESTAURANT_PROMPT
    if metadata:
        meta_text = f"\n\n## Video Metadata\n- Title: {metadata.get('title', '')}\n- Description: {metadata.get('description', '')}\n- Uploader: {metadata.get('uploader', '')}\n- Tags: {', '.join(metadata.get('tags') or [])}"
        prompt = prompt + meta_text

    # Send to model with restaurant analysis prompt
    print(f'[Gemini] Analyzing with {MODEL}...')
    response = client.models.generate_content(
        model=MODEL,
        contents=[uploaded_file, prompt],
        config={
            'response_mime_type': 'application/json',
        },
    )

    # Parse JSON response
    try:
        result = json.loads(response.text)
    except json.JSONDecodeError:
        print(f'[Gemini] WARNING: Could not parse JSON response: {response.text[:200]}')
        result = []

    # Ensure it's a list
    if not isinstance(result, list):
        result = [result] if result else []

    # Extract food shot frames from the video
    if result:
        result = extract_food_frames(video_path, video_id, result)

    # Geocode restaurant addresses
    if result:
        try:
            from geocoder import geocode_restaurant
            for restaurant in result:
                location = restaurant.get('location', {})
                coords = geocode_restaurant(location)
                if coords:
                    restaurant['lat'] = coords['lat']
                    restaurant['lng'] = coords['lng']
                else:
                    restaurant['lat'] = None
                    restaurant['lng'] = None
        except Exception as e:
            print(f'[Geocoder] Error: {e}')

    # Wrap in an envelope with TikTok URL and metadata
    output = {
        'tiktok_url': url,
        'video_file': video_path.name,
        'title': (metadata or {}).get('title', ''),
        'uploader': (metadata or {}).get('uploader', ''),
        'restaurants_found': len(result),
        'restaurants': result,
    }

    # Only save analysis JSON if restaurants were found
    if len(result) > 0:
        from gcs_utils import upload_json
        gcs_path = f'analysis_results/{video_id}_analysis.json'
        upload_json(output, gcs_path)
        print(f'[Gemini] Saved analysis → {gcs_path} ({len(result)} restaurant(s) found)')
    else:
        print(f'[Gemini] No restaurants found, skipping JSON save')

    return result


def is_restaurant_video(video_path: str) -> bool:
    """Quick check: does this video contain restaurant content?"""
    result = analyze_video(video_path)
    return len(result) > 0


if __name__ == '__main__':
    import sys
    if len(sys.argv) < 2:
        print('Usage: python3 analyzer.py <video_path>')
        sys.exit(1)
    result = analyze_video(sys.argv[1])
    print(json.dumps(result, indent=2))
