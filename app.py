from flask import Flask, request, jsonify, render_template, Response
from config import FLASK_PORT, YOUTUBE_BASE_API_URL, YOUTUBE_API_KEY, SEARCH_CACHE_TTL
from remote import (
    get_or_create_qr_code, invalidate_qr_cache,
    extract_youtube_id,
    validate_token,
    set_latest_play, get_latest_play,
    get_local_ip,
)
from catalog import get_categories, start_categories_refresh_thread
from html import unescape
import requests as http
import logging
import json
import time
import resolver

app = Flask(__name__)
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0
logging.basicConfig(level=logging.INFO, format='[pi_server] %(message)s')
_search_cache = {}

MWEB_USER_AGENT = (
    'Mozilla/5.0 (iPad; CPU OS 16_7_10 like Mac OS X) '
    'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 '
    'Mobile/15E148 Safari/604.1'
)
SUBTITLE_REQUEST_HEADERS = {
    'User-Agent': MWEB_USER_AGENT,
    'Referer': 'https://m.youtube.com/',
    'Accept': 'text/vtt,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
    'Accept-Encoding': 'identity',
}


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
    )


@app.route('/submit')
def submit_form():
    """Display the submit form page."""
    return render_template(
        'submit.html',
        token=request.args.get('token', ''),
        device_id=request.args.get('device_id', ''),
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


# ─── Native player API ────────────────────────────────────────────────────────

@app.route('/api/player/next')
def player_next():
    playlist_id = request.args.get('playlist_id')
    if not playlist_id:
        return jsonify({'error': 'missing playlist_id'}), 400

    exclude_id = request.args.get('exclude')
    should_prefetch = request.args.get('prefetch', '1') != '0'
    try:
        result = resolver.get_prefetched(playlist_id) or resolver.wait_for_prefetch(playlist_id)
        if not result:
            result = resolver.pick_and_resolve(playlist_id, exclude_id)
        if should_prefetch:
            resolver.schedule_prefetch(playlist_id, result['video_id'])
        return jsonify(result)
    except Exception as e:
        app.logger.warning('Could not resolve next playlist video: %s', e)
        return jsonify({'error': str(e)}), 500


@app.route('/api/player/prefetch')
def player_prefetch():
    playlist_id = request.args.get('playlist_id')
    if not playlist_id:
        return jsonify({'error': 'missing playlist_id'}), 400
    scheduled = resolver.schedule_prefetch(playlist_id, request.args.get('exclude'))
    return jsonify({'ok': True, 'scheduled': scheduled})


@app.route('/api/player/ready')
def player_ready():
    result = resolver.get_any_prefetched()
    return (jsonify(result), 200) if result else ('', 204)


@app.route('/api/player/resolve')
def player_resolve():
    video_id = request.args.get('video_id')
    if not video_id:
        return jsonify({'error': 'missing video_id'}), 400
    try:
        url, title, subtitle_url = resolver.resolve_stream_url(video_id)
        return jsonify({
            'url': url,
            'video_id': video_id,
            'title': title,
            'subtitle_url': subtitle_url,
        })
    except Exception as e:
        app.logger.warning('Could not resolve video %s: %s', video_id, e)
        return jsonify({'error': str(e)}), 500


@app.route('/proxy-subtitle')
def proxy_subtitle():
    url = request.args.get('url')
    if not url:
        return '', 400
    try:
        r = http.get(url, headers=SUBTITLE_REQUEST_HEADERS, timeout=10)
        r.raise_for_status()
        if not r.content.lstrip(b'\xef\xbb\xbf').startswith(b'WEBVTT'):
            raise ValueError('YouTube returned a non-VTT subtitle response')
        return r.content, 200, {
            'Content-Type': 'text/vtt; charset=utf-8',
            'Cache-Control': 'private, max-age=300',
        }
    except Exception as e:
        app.logger.warning('Subtitle proxy failed: %s', e)
        return '', 502


@app.route('/ping')
def ping():
    return jsonify({'ok': True})


# ─── Entry point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    resolver.initialize()
    start_categories_refresh_thread()
    app.run(host='0.0.0.0', port=FLASK_PORT, debug=False, threaded=True)
