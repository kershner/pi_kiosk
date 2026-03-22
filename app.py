from flask import Flask, request, jsonify, render_template, Response
from config import FLASK_PORT, YOUTUBE_BASE_API_URL, YOUTUBE_API_KEY, SEARCH_CACHE_TTL
from utils import (
    get_or_create_qr_code, invalidate_qr_cache,
    extract_youtube_id,
    validate_token,
    set_latest_play, get_latest_play,
    get_categories, start_categories_refresh_thread,
    _search_cache,
    get_local_ip,
)
from html import unescape
import requests as http
import logging
import json
import time

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format='[pi_server] %(message)s')


def get_base_url():
    """Build the Pi's local base URL for QR code generation."""
    return f'http://{get_local_ip()}:{FLASK_PORT}'


# ─── Views ─────────────────────────────────────────────────────────────────────

@app.route('/')
def home():
    device_id = request.args.get('device_id', '')
    categories = get_categories()

    qr_code_b64 = None
    if device_id:
        qr_code_b64 = get_or_create_qr_code(device_id, get_base_url())

    return render_template(
        'home.html',
        categories=categories,
        categories_json=json.dumps(categories),
        qr_code_b64=qr_code_b64,
        api_play_url='/api/play',
        latest_url='/latest',
        regenerate_qr_url='/regenerate-qr',
    )


@app.route('/submit')
def submit_form():
    """Display the submit form page."""
    return render_template(
        'submit.html',
        token=request.args.get('token', ''),
        device_id=request.args.get('device_id', ''),
        api_play_url='/api/play',
        youtube_search_url='/api/youtube-search',
    )


@app.route('/api/play', methods=['POST'])
def api_play():
    """Handle video/playlist play requests with token validation."""
    token = request.form.get('token')
    device_id = request.form.get('device_id')
    url = (request.form.get('url') or '').strip()

    if not token:
        return jsonify({'error': 'missing_token', 'message': 'No token provided'}), 400

    if not device_id:
        return jsonify({'error': 'missing_device', 'message': 'No device ID provided'}), 400

    if not validate_token(token, device_id):
        return jsonify({'error': 'invalid_or_expired', 'message': 'Invalid or expired QR code'}), 400

    result = extract_youtube_id(url)
    if not result:
        return jsonify({'error': 'not_youtube', 'message': 'Please provide a valid YouTube URL'}), 400

    content_type, content_id = result
    set_latest_play(device_id, content_type, content_id)

    return jsonify({'ok': True})


@app.route('/latest')
def latest():
    """Get the latest video play request for a device."""
    device_id = request.args.get('device')

    if not device_id:
        return jsonify({'error': 'missing_device'}), 400

    play = get_latest_play(device_id)
    if not play or not play.get('youtube_id'):
        return Response(status=204)

    return jsonify(play)


@app.route('/regenerate-qr', methods=['POST'])
def regenerate_qr():
    """Generate a new QR code with a fresh token."""
    device_id = request.form.get('device_id', '')

    if not device_id:
        return jsonify({'error': 'missing_device', 'message': 'No device ID provided'}), 400

    invalidate_qr_cache(device_id)
    qr_code_b64 = get_or_create_qr_code(device_id, get_base_url())

    return jsonify({'qr_code_b64': qr_code_b64, 'regenerated': True})


@app.route('/api/youtube-search')
def youtube_search():
    """Server-side YouTube search endpoint with caching - supports videos and playlists."""
    template = 'search_results.html'
    query = (request.args.get('q') or '').strip()

    if not query or len(query) < 3:
        return render_template(template, videos=[], playlists=[], error=None)

    cache_key = query.lower()
    cached = _search_cache.get(cache_key)
    if cached and time.time() - cached['ts'] < SEARCH_CACHE_TTL:
        return render_template(template, **cached['result'])

    if not YOUTUBE_API_KEY:
        return render_template(template, videos=[], playlists=[], error='YouTube API key not configured')

    try:
        video_response = http.get(
            f'{YOUTUBE_BASE_API_URL}/search',
            params={
                'part': 'snippet', 'q': query, 'type': 'video',
                'maxResults': 10, 'key': YOUTUBE_API_KEY,
                'videoEmbeddable': 'true', 'safeSearch': 'moderate',
            },
            timeout=5,
        )

        playlist_response = http.get(
            f'{YOUTUBE_BASE_API_URL}/search',
            params={
                'part': 'snippet', 'q': query, 'type': 'playlist',
                'maxResults': 5, 'key': YOUTUBE_API_KEY, 'safeSearch': 'moderate',
            },
            timeout=5,
        )

        if not video_response.ok:
            error_msg = video_response.json().get('error', {}).get('message', 'Search failed')
            return render_template(template, videos=[], playlists=[], error=error_msg)

        videos = [
            {
                'video_id': item['id']['videoId'],
                'title': unescape(item['snippet']['title']),
                'author': unescape(item['snippet']['channelTitle']),
                'thumbnail': item['snippet']['thumbnails']['medium']['url'],
                'published_at': item['snippet'].get('publishedAt', '')[:10],
                'type': 'video',
            }
            for item in video_response.json().get('items', [])
        ]

        playlist_data = playlist_response.json() if playlist_response.ok else {'items': []}
        playlist_ids = [item['id']['playlistId'] for item in playlist_data.get('items', [])]

        playlist_details = {}
        if playlist_ids:
            details_response = http.get(
                f'{YOUTUBE_BASE_API_URL}/playlists',
                params={'part': 'contentDetails', 'id': ','.join(playlist_ids), 'key': YOUTUBE_API_KEY},
                timeout=5,
            )
            if details_response.ok:
                for item in details_response.json().get('items', []):
                    playlist_details[item['id']] = item.get('contentDetails', {}).get('itemCount', 0)

        playlists = [
            {
                'playlist_id': item['id']['playlistId'],
                'title': unescape(item['snippet']['title']),
                'author': unescape(item['snippet']['channelTitle']),
                'thumbnail': item['snippet']['thumbnails']['medium']['url'],
                'published_at': item['snippet'].get('publishedAt', '')[:10],
                'video_count': playlist_details.get(item['id']['playlistId'], 0),
                'type': 'playlist',
            }
            for item in playlist_data.get('items', [])
        ]

        result_data = {'videos': videos, 'playlists': playlists, 'error': None}
        _search_cache[cache_key] = {'result': result_data, 'ts': time.time()}
        return render_template(template, **result_data)

    except http.exceptions.Timeout:
        return render_template(template, videos=[], playlists=[], error='Search timed out. Please try again.')
    except Exception as e:
        return render_template(template, videos=[], playlists=[], error=f'Search failed: {str(e)}')
    

@app.route('/proxy-subtitle')
def proxy_subtitle():
    import requests as req
    url = request.args.get('url')
    if not url:
        return '', 400
    try:
        r = req.get(url, timeout=10)
        return r.content, 200, {'Content-Type': 'text/vtt; charset=utf-8'}
    except Exception:
        return '', 502


@app.route('/ping')
def ping():
    return jsonify({'ok': True})


# ─── Entry point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    start_categories_refresh_thread()
    app.run(host='0.0.0.0', port=FLASK_PORT, debug=False, threaded=True)
