# pi_kiosk

![pi_kiosk](https://djfdm802jwooz.cloudfront.net/static/project_images/939e9a4e86184c19a8b74dcb3da54e20.png)

A lightweight YouTube kiosk built for a Raspberry Pi 2B. It boots into a fullscreen native video player, shuffles through remotely managed playlists, and lets people on the local network send videos to the display through a QR code.

## Highlights

- Native `<video>` playback using direct streams resolved by yt-dlp
- Curated playlist browsing and category-aware shuffle
- Background prefetching and persistent warm caches for quicker transitions
- Touch gestures for pause, seek, previous, and next
- Titles, playback progress, duration, and optional English captions
- QR-based remote video and playlist submission with YouTube search
- Fullscreen Chromium interface designed for low-powered hardware

## Architecture

Flask serves the interface, remote-control routes, and native-player API on one local port. The in-process resolver runs yt-dlp in subprocesses, validates media URLs, and limits background prefetching to one low-priority worker.

YouTube stream resolution uses a local `bgutil-ytdlp-pot-provider` process for Proof-of-Origin tokens. The browser receives direct media URLs and plays them without loading the YouTube website or iframe player.

## kershner.org integration

Playlist categories and YouTube playlist IDs are managed through the Django admin on [kershner.org](https://kershner.org) and exposed through its `/pi/categories.json` endpoint. The kiosk refreshes that catalog periodically, so its content can change without modifying or redeploying the Pi code.

## Code map

| File | Purpose |
|---|---|
| `app.py` | Flask UI, search, remote-control, subtitle, and player routes |
| `resolver.py` | yt-dlp resolution, media validation, caching, and prefetching |
| `remote.py` | QR tokens, remote-play state, and YouTube URL parsing |
| `catalog.py` | Remotely managed playlist catalog cache |
| `static/js/piStuff.js` | Kiosk playback, shuffle, menu, and touch controls |
| `static/js/submitForm.js` | Remote submission and YouTube search |
| `static/css/pi_stuff.css` | Kiosk, menu, player, and submission styles |
| `misc/xinitrc` | Pi display-session startup |
| `misc/install_youtube_support.sh` | yt-dlp, Node.js, and PO-token provider setup |

## Tests

The core resolver, caching, URL parsing, templates, and player API are covered with standard-library `unittest` tests:

```sh
venv/bin/python3 -m unittest discover -s tests -v
```
