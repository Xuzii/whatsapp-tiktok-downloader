import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import yt_dlp
from flask import Flask, jsonify, request

from analyzer import analyze_video
from build_web_data import build as rebuild_web_data
from metadata_filter import is_likely_restaurant

app = Flask(__name__)

# ============================================================
# CONFIGURATION
# ============================================================
DOWNLOADS_DIR = Path('/tmp/tiktok_videos')
TRACKER_FILE = Path(__file__).parent / 'url_tracker.json'
MAX_RETRIES = 3
# ============================================================

TIKTOK_URL_REGEX = re.compile(
    r'https?://(www\.)?(vm\.tiktok\.com|vt\.tiktok\.com|tiktok\.com/(t|@[\w.-]+/video))/[\w.-]+'
)

# Ensure download directory exists
DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)


def load_tracker():
    if TRACKER_FILE.exists():
        with open(TRACKER_FILE, 'r') as f:
            return json.load(f)
    # Fallback: restore from GCS on cold start
    try:
        from gcs_utils import download_json
        data = download_json('state/url_tracker.json')
        if data:
            print('[Tracker] Restored from GCS backup')
            with open(TRACKER_FILE, 'w') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            return data
    except Exception as e:
        print(f'[Tracker] GCS restore failed: {e}')
    return {}


def save_tracker(tracker):
    with open(TRACKER_FILE, 'w') as f:
        json.dump(tracker, f, indent=2, ensure_ascii=False)
    # Backup to GCS
    try:
        from gcs_utils import upload_json
        upload_json(tracker, 'state/url_tracker.json')
    except Exception as e:
        print(f'[Tracker] GCS backup failed: {e}')


def normalize_url(url):
    """Strip query params and trailing slashes for dedup comparison."""
    url = url.split('?')[0].split('#')[0].rstrip('/')
    return url


def is_duplicate(url, tracker):
    normalized = normalize_url(url)
    for tracked_url in tracker:
        if normalize_url(tracked_url) == normalized:
            return True
    return False


def extract_metadata(url):
    """Extract metadata without downloading the video."""
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'socket_timeout': 30,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        return {
            'id': info.get('id', 'unknown'),
            'title': info.get('title', ''),
            'description': info.get('description', ''),
            'uploader': info.get('uploader', ''),
            'duration': info.get('duration', 0),
            'tags': info.get('tags', []),
        }


def download_video(url):
    """Download a TikTok video using yt-dlp. Returns dict with result info."""
    outtmpl = str(DOWNLOADS_DIR / '%(id)s.%(ext)s')

    ydl_opts = {
        'format': 'best',
        'outtmpl': outtmpl,
        'writeinfojson': True,
        'quiet': False,
        'no_warnings': False,
        'socket_timeout': 30,
        'retries': MAX_RETRIES,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        return {
            'filename': os.path.basename(filename),
            'id': info.get('id', 'unknown'),
            'title': info.get('title', ''),
            'uploader': info.get('uploader', ''),
        }


def load_rich_metadata(video_id, fallback_meta):
    """Load full metadata from .info.json sidecar (saved by yt-dlp)."""
    info_json_path = DOWNLOADS_DIR / f'{video_id}.info.json'
    if info_json_path.exists():
        with open(info_json_path, 'r') as f:
            info = json.load(f)
        return {
            'title': info.get('title', '') or info.get('fulltitle', ''),
            'description': info.get('description', ''),
            'uploader': info.get('uploader', ''),
            'tags': info.get('tags') or [],
        }
    return fallback_meta


# ============================================================
# ENDPOINTS
# ============================================================

@app.route('/process', methods=['POST'])
def handle_process():
    """
    Full pipeline: dedup check → metadata extract → keyword filter →
    download (if maybe/likely) → Gemini analysis → save results.
    """
    data = request.get_json()
    if not data or 'url' not in data:
        return jsonify({'status': 'error', 'message': 'Missing url field'}), 400

    url = data['url']
    chat_name = data.get('chat_name', '')
    sender = data.get('sender', '')

    # 1. Dedup check
    tracker = load_tracker()
    if is_duplicate(url, tracker):
        return jsonify({'status': 'skipped', 'message': 'Already processed'}), 200

    # 2. Extract metadata (no download)
    try:
        meta = extract_metadata(url)
    except Exception as e:
        print(f'[ERROR] Metadata extraction failed for {url}: {e}')
        return jsonify({'status': 'error', 'message': f'Metadata failed: {e}'}), 500

    # 3. Keyword pre-filter
    filter_result = is_likely_restaurant(meta)
    print(f'[FILTER] {url} → {filter_result} (title: {meta["title"][:60]})')

    if filter_result == 'unlikely':
        tracker[url] = {
            'filename': None,
            'title': meta['title'],
            'uploader': meta['uploader'],
            'sender': sender,
            'chat': chat_name,
            'downloaded_at': datetime.now(timezone.utc).isoformat(),
            'category': 'skipped_not_restaurant',
            'filter_result': filter_result,
        }
        save_tracker(tracker)
        return jsonify({
            'status': 'skipped',
            'message': f'Not restaurant-related (filter: {filter_result})',
            'title': meta['title'],
            'filter_result': filter_result,
        }), 200

    # 4. Download video
    try:
        dl_result = download_video(url)
    except Exception as e:
        print(f'[ERROR] Download failed for {url}: {e}')
        return jsonify({'status': 'error', 'message': f'Download failed: {e}'}), 500

    video_path = str(DOWNLOADS_DIR / dl_result['filename'])

    # 5. Load rich metadata from .info.json sidecar for Gemini
    rich_meta = load_rich_metadata(dl_result['id'], meta)

    # 6. Gemini analysis
    analysis = []
    analysis_error = None
    try:
        analysis = analyze_video(video_path, url=url, metadata=rich_meta)
    except Exception as e:
        print(f'[ERROR] Gemini analysis failed for {url}: {e}')
        analysis_error = str(e)

    # 7. Clean up video files from /tmp
    try:
        video_file = Path(video_path)
        if video_file.exists():
            video_file.unlink()
        info_json = video_file.with_suffix('.info.json')
        if info_json.exists():
            info_json.unlink()
        print(f'[CLEANUP] Deleted {dl_result["filename"]} from /tmp')
    except Exception as e:
        print(f'[CLEANUP] Warning: {e}')

    # 8. Determine category
    if analysis_error:
        category = 'analysis_failed'
    elif len(analysis) > 0:
        category = 'restaurant'
    else:
        category = 'not_restaurant'

    # 9. Save to tracker
    tracker[url] = {
        'filename': dl_result['filename'],
        'title': dl_result['title'],
        'uploader': dl_result['uploader'],
        'sender': sender,
        'chat': chat_name,
        'downloaded_at': datetime.now(timezone.utc).isoformat(),
        'category': category,
        'filter_result': filter_result,
        'restaurants_found': len(analysis),
        'analysis_file': f'{dl_result["id"]}_analysis.json' if analysis else None,
        'analysis_error': analysis_error,
    }
    save_tracker(tracker)

    # 10. Rebuild web data if restaurants were found
    if category == 'restaurant':
        try:
            rebuild_web_data()
        except Exception as e:
            print(f'[WARN] Failed to rebuild web data: {e}')

    restaurant_count = len(analysis)
    print(f'[OK] {url} → {category} ({restaurant_count} restaurant(s))')

    return jsonify({
        'status': 'success',
        'category': category,
        'message': f'{category}: {restaurant_count} restaurant(s) found',
        'filename': dl_result['filename'],
        'title': dl_result['title'],
        'filter_result': filter_result,
        'restaurants_found': restaurant_count,
        'analysis': analysis,
    }), 200


@app.route('/health', methods=['GET'])
def health():
    tracker = load_tracker()
    categories = {}
    for entry in tracker.values():
        cat = entry.get('category', 'unknown')
        categories[cat] = categories.get(cat, 0) + 1
    return jsonify({
        'status': 'ok',
        'total_entries': len(tracker),
        'categories': categories,
    })


if __name__ == '__main__':
    print(f'Download directory: {DOWNLOADS_DIR}')
    print(f'Tracker file: {TRACKER_FILE.resolve()}')
    print('Starting download server on http://localhost:5001')
    app.run(host='127.0.0.1', port=5001)
