# IPTV Player

A lightweight IPTV player that runs in your browser: Live TV, movies and series from your own IPTV subscription.
It has no accounts, no build step and no dependencies to install. You only need Python and a browser.

> This is only a player. It contains no channels or content. You need your own IPTV subscription.

## Features

- **Live TV**: browse categories and channels, see what's on now and next, and zap with the ↑ and ↓ keys
- **Movies**: a poster grid with ratings, plots and cast, sortable by newest, A–Z or rating
- **Series**: seasons and episodes; the next episode starts automatically
- **Continue watching**: remembers where you stopped in a movie or episode
- **Favorites** for channels, movies and series
- **Adjustable live buffer**: trade a few seconds of delay for smoother playback
- **Three ways to sign in**: Xtream Codes login, an M3U link, or a local `.m3u` file

## Requirements

- Python 3.8 or newer. Only the standard library is used, so there's nothing to `pip install`.
- A modern browser: Chrome, Edge, Firefox or Safari

The app's interface is in Dutch.

## Getting started

```bash
git clone https://github.com/<your-username>/IPTV-Player.git
cd IPTV-Player
python server.py
```

Your browser opens at **http://localhost:8000**. Sign in on the start screen:

| Method | What to enter |
| --- | --- |
| **Xtream login** (recommended) | Server address (e.g. `http://server.com:8080`), username and password |
| **M3U link** | Your playlist URL. A `get.php?username=…&password=…` link is turned into an Xtream login automatically. |
| **File** | A `.m3u` / `.m3u8` file on your computer |

Use Xtream login if your provider supports it. It gives you categories, the TV guide, posters and series details. With a plain M3U playlist, the app guesses movies and series from the stream URLs and names.

Stop the server with `Ctrl+C`.

## Why is there a local server?

Browsers block most IPTV streams when a web page loads them directly: providers serve plain `http` streams without CORS headers. `server.py` is a small proxy that fetches playlists and streams for the page.

- It listens on `127.0.0.1` only, so it can't be reached from your network.
- It doesn't log URLs. In error messages, usernames and passwords are masked.

## Options

| Option | Description |
| --- | --- |
| `python server.py --no-browser` | Don't open the browser automatically |
| `PORT=9000 python server.py` | Use a different port (default `8000`) |
| `IPTV_UA="MyApp/1.0" python server.py` | Try a custom User-Agent first when connecting to the provider |
| `python server.py --test "<url>"` | Check a playlist link step by step: DNS, connection and provider response |

On Windows PowerShell, set variables like this: `$env:PORT=9000; python server.py`

## Troubleshooting

**A channel doesn't play.**
The message under the player says why:
- `404`: the stream is offline or has been removed.
- `401` / `403`: the provider refused the connection, usually because you've hit your connection limit. Close other apps or TVs that use the same subscription.

**Live TV keeps buffering.**
Go to **Instellingen → Live TV: buffer** (Settings) and choose 10 or 20 seconds. The app also builds up extra buffer on its own when a channel stalls repeatedly.

**A movie won't play (often `.mkv`).**
Some formats or codecs aren't supported by browsers. Click **Kopieer link voor VLC** (Copy link for VLC) and open the stream in [VLC](https://www.videolan.org/).

**The playlist doesn't load.**
Run `python server.py --test "<your link>"` to see where it fails. Check that the link matches the one in your other IPTV app. Also close that app first: many subscriptions allow only one connection at a time.

**"Je hebt dit bestand direct geopend" (You opened this file directly).**
Don't open `index.html` by double-clicking it. Start `server.py` and go to http://localhost:8000.

## Privacy

- Everything runs on your own computer; no data is sent anywhere except to your IPTV provider.
- If you tick "Onthoud op deze computer" (Remember on this computer), your login, favorites and watch progress are stored in your browser's `localStorage`. They are never written to the project folder, so they can't end up in git.

## Project structure

```
index.html   The complete app (HTML, CSS and JavaScript)
server.py    Local server and stream proxy
```

Playback uses [hls.js](https://github.com/video-dev/hls.js) for HLS streams and [mpegts.js](https://github.com/xqq/mpegts.js) for MPEG-TS streams, both loaded from a CDN.

## Disclaimer

This software is a player only and does not provide any content. Use it only with subscriptions and streams you are legally allowed to access.
