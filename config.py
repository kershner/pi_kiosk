import os

FLASK_PORT = 5020

TOKEN_TTL = 60 * 20          # 20 minutes
SEARCH_CACHE_TTL = 60 * 5    # 5 minutes
CATEGORIES_REFRESH_INTERVAL = 60 * 60  # 1 hour

YOUTUBE_BASE_API_URL = 'https://www.googleapis.com/youtube/v3'
CATEGORIES_URL = 'https://kershner.org/pi/categories.json'

YOUTUBE_API_KEY = os.environ.get('YOUTUBE_API_KEY', '')