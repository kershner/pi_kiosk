# pi_kiosk

![pi_kiosk](https://djfdm802jwooz.cloudfront.net/static/project_images/939e9a4e86184c19a8b74dcb3da54e20.png)

A YouTube kiosk for Raspberry Pi. Boots straight into fullscreen video, shuffles through curated playlists, and lets anyone on the local network scan a QR code to send a video to the screen.

Built after a YouTube frontend update made the iframe embed too heavy for aging Pi 2B hardware.

## How it works

Instead of the YouTube embed, two local servers handle everything:

**`app.py`** (Flask, port 5020) serves the UI, QR codes, YouTube search, and watches for incoming video submissions. Playlists are fetched from [kershner.org](https://kershner.org) on startup and refreshed hourly, so the playlist library is managed remotely without touching the Pi.

**`player_server.py`** (port 8765) uses yt-dlp to resolve YouTube IDs into direct MP4 stream URLs, which the native `<video>` element plays directly. Playlist video lists are cached for an hour, and one deduplicated background job pre-fetches the video that will actually play next so transitions are seamless without overloading the Pi. For videos longer than 5 hours, the stream URL is automatically refreshed before YouTube's ~6 hour expiry.

YouTube now requires Proof-of-Origin (PO) tokens for most direct media requests. The kiosk runs the maintained `bgutil-ytdlp-pot-provider` locally on port 4416 and uses its video-specific tokens when yt-dlp resolves each stream. Resolved URLs are probed before they reach Chromium so rejected URLs can be retried automatically.

## Raspberry Pi YouTube setup

The Pi 2B uses 32-bit ARMv7, so this project uses the official Node.js 22 ARMv7 build instead of Deno. From the Pi, update the checkout and run the installer once:

```sh
cd /home/pi/pi_kiosk
chmod +x misc/install_youtube_support.sh
./misc/install_youtube_support.sh
```

The installer:

- installs Node.js 22 for ARMv7;
- updates yt-dlp and installs its EJS and PO-token plugin dependencies;
- clones and builds the token provider in `/home/pi/bgutil-ytdlp-pot-provider`.

The first installation can take several minutes on a Pi 2B because the provider's small native canvas dependency may need to compile locally.

Install the updated X startup script if `/home/pi/.xinitrc` is not already linked to the repository copy, then reboot:

```sh
cp /home/pi/pi_kiosk/misc/xinitrc /home/pi/.xinitrc
sudo reboot
```

After reboot, verify all three local services and inspect their logs:

```sh
curl -s http://127.0.0.1:5020/ping
curl -s http://127.0.0.1:8765/ping
curl -s -o /dev/null -w 'PO provider HTTP %{http_code}\n' http://127.0.0.1:4416/
tail -n 100 /home/pi/logs/pot_provider.log
tail -n 100 /home/pi/logs/player_server.log
```

Successful stream resolution now logs `Validated VIDEO_ID format FORMAT_ID`, followed by the selected subtitle language when captions are available. A rejected CDN URL logs its HTTP status and is not sent to Chromium. Subtitle requests are proxied with the same mweb identity used during resolution so YouTube accepts the tokenized caption URL.

## Touch controls

Tap left/right thirds of the screen to control playback. Tap the top-left corner to open the menu.

| Gesture | Left third | Right third |
|---|---|---|
| Single tap | Pause / Resume | Pause / Resume |
| Double tap | Seek back 20s | Seek forward 20s |
| Triple tap | Previous video | Next video |

## QR code / remote play

"Show QR" in the menu displays a scannable code. Anyone on the network can scan it to open a submit form where they can search YouTube or paste a URL to send a video to the screen. Tokens are short-lived, so stale QR codes stop working on their own.

## Files

| File | Purpose |
|---|---|
| [`app.py`](https://github.com/kershner/pi_kiosk/blob/master/app.py) | Flask routes — UI, QR, tokens, search, polling |
| [`config.py`](https://github.com/kershner/pi_kiosk/blob/master/config.py) | Ports, TTLs, API URLs |
| [`utils.py`](https://github.com/kershner/pi_kiosk/blob/master/utils.py) | QR generation, token auth, categories cache, YouTube URL parsing |
| [`player_server.py`](https://github.com/kershner/pi_kiosk/blob/master/player_server.py) | yt-dlp stream resolver with prefetch and playlist caching |
| [`misc/xinitrc`](https://github.com/kershner/pi_kiosk/blob/master/misc/xinitrc) | Starts both servers then launches Chromium on boot |
| [`static/js/piStuff.js`](https://github.com/kershner/pi_kiosk/blob/master/static/js/piStuff.js) | Playback, menu, shuffle, touch controls |
| [`static/js/deviceManager.js`](https://github.com/kershner/pi_kiosk/blob/master/static/js/deviceManager.js) | Generates and persists a device ID |
| [`static/js/submitForm.js`](https://github.com/kershner/pi_kiosk/blob/master/static/js/submitForm.js) | QR submit form logic |
| [`static/js/youtubeSearch.js`](https://github.com/kershner/pi_kiosk/blob/master/static/js/youtubeSearch.js) | YouTube search with keyboard navigation |
| [`static/css/pi_stuff.css`](https://github.com/kershner/pi_kiosk/blob/master/static/css/pi_stuff.css) | All styles |
| [`templates/home.html`](https://github.com/kershner/pi_kiosk/blob/master/templates/home.html) | Kiosk UI |
| [`templates/submit.html`](https://github.com/kershner/pi_kiosk/blob/master/templates/submit.html) | QR submit form |
| [`templates/search_results.html`](https://github.com/kershner/pi_kiosk/blob/master/templates/search_results.html) | Search results partial |

## kershner.org dependency

Playlists are managed via the Django admin on [kershner.org](https://kershner.org) and served at:

```
GET https://kershner.org/pi/categories.json
```
