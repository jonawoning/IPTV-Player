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

- It listens on `127.0.0.1` only, so it can't be reached from your network. Hosting it online goes through a web server such as nginx; see below.
- It doesn't log URLs. In error messages, usernames and passwords are masked.

## Running on your own server (e.g. with Ploi)

You can host the player on a VPS so you can watch from anywhere. On a server the app **requires a password**: it refuses requests that come through a web server until you've set one.

What protects it:
- A login page, with the password stored as a salted PBKDF2 hash (600,000 iterations), never in plain text
- Secure session cookies (`HttpOnly`, `SameSite=Lax`, and `Secure` over HTTPS)
- Brute-force protection: after 5 wrong passwords an IP address is blocked for 15 minutes, and after 50 failures in total all logins pause for 15 minutes
- The proxy never connects to internal addresses (localhost, private networks, cloud metadata), not even after a redirect
- Only `index.html` is served; source files, `.git` and the password file are never reachable
- Security headers (CSP, HSTS, no framing), and responses from IPTV providers can never run as a page on your domain

### 1. Create the site
In Ploi, create a site for your (sub)domain, deploy this repository to it, and enable **SSL** (Let's Encrypt).

### 2. Set a password
SSH into the server, go to the site folder and run:

```bash
python3 server.py --set-password
```

Use at least 12 characters; a sentence of a few words works well. The hash is stored in `.iptv-auth` (readable only by its owner and ignored by git). To change the password, run the command again and restart the daemon.

### 3. Add a daemon
In Ploi go to **Server → Daemons** and add:

| Field | Value |
| --- | --- |
| Command | `python3 server.py --no-browser --require-auth` |
| Directory | `/home/ploi/your-domain.com` |
| User | `ploi` |

`--require-auth` stops the server from starting if no password is set.

### 4. Configure nginx
In Ploi go to **Site → Manage → Nginx configuration** and replace the `location / { … }` block with:

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;

    # Stream video straight through instead of buffering it
    proxy_buffering off;
    proxy_request_buffering off;
    proxy_read_timeout 1h;
    proxy_send_timeout 1h;

    # Proxy URLs contain your IPTV username and password: keep them out of the logs
    access_log off;
}
```

Leave out anything that serves files directly from the site folder (such as a `root` with `try_files`): all traffic must go through `server.py`.

### Good to know
- **Bandwidth**: all video passes through your server (about 3–8 Mbit/s per HD stream). Check your VPS traffic allowance.
- **Provider rules**: some IPTV providers block data-center IP addresses, or allow only one connection at a time. Watching on the server and in another app at the same time may not work.
- **Sessions** are kept in memory. After a restart or a new password, everyone has to log in again.
- **Personal use only.** Don't share access: that would make you a redistributor of the streams.

## Options

| Option | Description |
| --- | --- |
| `python server.py --no-browser` | Don't open the browser automatically |
| `PORT=9000 python server.py` | Use a different port (default `8000`) |
| `IPTV_UA="MyApp/1.0" python server.py` | Try a custom User-Agent first when connecting to the provider |
| `python server.py --test "<url>"` | Check a playlist link step by step: DNS, connection and provider response |
| `python server.py --set-password` | Set or change the login password (required when hosting on a server) |
| `python server.py --require-auth` | Refuse to start without a password |
| `IPTV_SESSION_DAYS=7` | How long a login stays valid (default 30 days) |
| `IPTV_ALLOW_PRIVATE=1` | Allow the proxy to reach private network addresses (only for an IPTV server on your own network) |

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
