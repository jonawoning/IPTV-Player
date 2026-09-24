#!/usr/bin/env python3
"""
Mijn IPTV - lokale server
Start met:  python server.py      (of: python3 server.py)
Open daarna http://localhost:8000 in je browser.

Werkt een link niet? Test hem met:
    python server.py --test "http://jouw-link"
Dat laat stap voor stap zien waar het misgaat (wachtwoorden worden verborgen).

De server:
  - serveert index.html
  - stuurt playlists en streams door via /proxy?url=...
    zodat de browser geen last heeft van CORS of http/https-blokkades.
Hij luistert alleen op je eigen computer (127.0.0.1), niet op het netwerk.
"""
import functools
import http.server
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

PORT = int(os.environ.get("PORT", "8000"))
HOST = "127.0.0.1"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CHUNK = 64 * 1024

PLAYLIST_TIMEOUT = 180  # grote playlists worden door de provider ter plekke gemaakt
STREAM_TIMEOUT = 45

# Namen waarmee de app zich bij de provider meldt. Werkt de eerste niet (fout 401/403/5xx),
# dan wordt de volgende geprobeerd. De eerste die werkt wordt onthouden.
USER_AGENTS = [
    "IPTVSmartersPlayer",
    "VLC/3.0.20 LibVLC/3.0.20",
    "okhttp/4.12.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
]
if os.environ.get("IPTV_UA"):
    USER_AGENTS.insert(0, os.environ["IPTV_UA"])
working_ua = {}  # per host: de naam die werkte


# ---------- IPv4 eerst proberen (voorkomt lange wachttijden bij kapot IPv6) ----------
_orig_getaddrinfo = socket.getaddrinfo
def _ipv4_first(*args, **kwargs):
    res = _orig_getaddrinfo(*args, **kwargs)
    return sorted(res, key=lambda r: 0 if r[0] == socket.AF_INET else 1)
socket.getaddrinfo = _ipv4_first


def mask(url):
    """Verberg gebruikersnaam en wachtwoord in meldingen."""
    url = re.sub(r"(username|password|user|pass)=([^&]+)", r"\1=***", url, flags=re.I)
    url = re.sub(r"(/(?:live|movie|series|timeshift)/)([^/]+)/([^/]+)/", r"\1***/***/", url)
    # korte Xtream-vorm: http://host/gebruiker/wachtwoord/12345(.ts)
    url = re.sub(r"^(https?://[^/]+/)(?!live/|movie/|series/)[^/?]+/[^/?]+/(\d+(?:\.\w+)?)(?=$|\?)", r"\1***/***/\2", url)
    return re.sub(r"//[^/@]+@", "//***@", url)


def proxied(url):
    return "/proxy?url=" + urllib.parse.quote(url, safe="")


def rewrite_hls(text, base_url):
    """Laat alle segment- en sub-playlist-URL's in een HLS-lijst ook via de proxy lopen."""
    out = []
    for line in text.splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            out.append(proxied(urllib.parse.urljoin(base_url, s)))
        elif 'URI="' in s:
            out.append(re.sub(
                r'URI="([^"]+)"',
                lambda m: 'URI="' + proxied(urllib.parse.urljoin(base_url, m.group(1))) + '"',
                line,
            ))
        else:
            out.append(line)
    return "\n".join(out) + "\n"


def open_upstream(url, timeout, range_header=None):
    """Open de URL en probeer zo nodig andere app-namen. Geeft (resp, None) of (None, (code, melding))."""
    host = urllib.parse.urlparse(url).netloc
    uas = USER_AGENTS[:]
    if host in working_ua:
        uas.remove(working_ua[host]); uas.insert(0, working_ua[host])
    last = (502, "Onbekende fout")
    for ua in uas:
        req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "*/*", "Connection": "keep-alive"})
        if range_header:
            req.add_header("Range", range_header)
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
            working_ua[host] = ua
            return resp, None
        except urllib.error.HTTPError as e:
            last = (e.code, f"Provider gaf fout {e.code}")
            if e.code in (401, 403, 405, 406, 429, 500, 502, 503, 512, 513, 884):
                continue  # misschien accepteert hij een andere app-naam
            return None, last
        except (TimeoutError, socket.timeout):
            return None, (504, f"Provider reageerde niet binnen {timeout} seconden")
        except urllib.error.URLError as e:
            reason = e.reason
            if isinstance(reason, (TimeoutError, socket.timeout)):
                return None, (504, f"Provider reageerde niet binnen {timeout} seconden")
            if isinstance(reason, socket.gaierror):
                return None, (502, "Adres van de provider niet gevonden (DNS)")
            if isinstance(reason, ConnectionRefusedError):
                return None, (502, "Provider weigert de verbinding")
            return None, (502, f"Kan provider niet bereiken: {reason}")
        except Exception as e:
            return None, (502, f"Kan provider niet bereiken: {type(e).__name__}")
    return None, last


class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # stil: URL's bevatten je gebruikersnaam en wachtwoord

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/proxy":
            return self.handle_proxy(parsed)
        if parsed.path in ("/", ""):
            self.path = "/index.html"
        return super().do_GET()

    def send_text_error(self, code, msg):
        body = msg.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_proxy(self, parsed):
        qs = urllib.parse.parse_qs(parsed.query)
        url = qs.get("url", [None])[0]
        is_list_request = qs.get("kind", [""])[0] == "list"
        if not url or not url.lower().startswith(("http://", "https://")):
            return self.send_text_error(400, "Ongeldige URL")

        timeout = PLAYLIST_TIMEOUT if is_list_request else STREAM_TIMEOUT
        started = time.time()
        resp, err = open_upstream(url, timeout, self.headers.get("Range"))
        if err:
            print(f"[fout] {err[1]}  ({mask(url)})")
            return self.send_text_error(err[0], err[1])

        final_url = resp.geturl()
        ctype = resp.headers.get("Content-Type", "") or ""
        path = urllib.parse.urlparse(final_url).path.lower()
        is_playlist = is_list_request or "mpegurl" in ctype.lower() or path.endswith((".m3u8", ".m3u"))

        try:
            if is_playlist:
                body = resp.read()
                text = body.decode("utf-8", "replace")
                if "#EXT-X-" in text:  # HLS-stream, geen zenderlijst
                    body = rewrite_hls(text, final_url).encode("utf-8")
                    ctype = "application/vnd.apple.mpegurl"
                if is_list_request and len(body) > 500_000:  # kleine API-antwoorden (tv-gids e.d.) niet melden
                    print(f"[ok] Lijst geladen: {len(body)/1e6:.1f} MB in {time.time()-started:.0f} s")
                self.send_response(200)
                self.send_header("Content-Type", ctype or "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(resp.status)
                for h in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
                    if resp.headers.get(h):
                        self.send_header(h, resp.headers[h])
                self.end_headers()
                try:  # kleine stukjes direct doorsturen, niet wachten tot een blok vol is
                    self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                except OSError:
                    pass
                read = getattr(resp, "read1", resp.read)  # read1: geef wat er nu binnen is
                while True:
                    chunk = read(CHUNK)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass  # speler gestopt of zender gewisseld
        except (TimeoutError, socket.timeout):
            print(f"[fout] Provider stopte met sturen  ({mask(url)})")
        finally:
            resp.close()


# ---------- testmodus ----------
def run_test(url):
    print(f"\nTest van: {mask(url)}\n")
    p = urllib.parse.urlparse(url)
    host, port = p.hostname, p.port or (443 if p.scheme == "https" else 80)

    print("1. Adres opzoeken…", end=" ", flush=True)
    try:
        ips = sorted({a[4][0] for a in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})
        print("ok:", ", ".join(ips))
    except Exception as e:
        print("MISLUKT:", e); print("   → De naam van de server klopt niet, of je DNS blokkeert hem."); return

    print(f"2. Verbinden met poort {port}…", end=" ", flush=True)
    try:
        t = time.time(); s = socket.create_connection((host, port), timeout=15); s.close()
        print(f"ok ({(time.time()-t)*1000:.0f} ms)")
    except Exception as e:
        print("MISLUKT:", type(e).__name__)
        print("   → Je netwerk, firewall, VPN of provider blokkeert deze poort vanaf deze computer.")
        print("     Werkt het op je telefoon via 4G wel? Dan ligt het aan dit netwerk.")
        return

    print("3. Provider vragen om de playlist (per app-naam, max. 60 s)…")
    for ua in USER_AGENTS:
        print(f"   - als '{ua[:30]}'…", end=" ", flush=True)
        req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "*/*"})
        t = time.time()
        try:
            r = urllib.request.urlopen(req, timeout=60)
            first = r.read(2048)
            print(f"antwoord {r.status} na {time.time()-t:.1f} s")
            snippet = first.decode("utf-8", "replace")
            if "#EXTM3U" in snippet or "#EXTINF" in snippet:
                print("\nGelukt: dit is een geldige playlist. Start de app gewoon en laad de link opnieuw.")
            else:
                print("\nDe provider antwoordt, maar het is geen playlist. Begin van het antwoord:")
                print("   " + mask(snippet[:300]).replace("\n", "\n   "))
            r.close()
            return
        except urllib.error.HTTPError as e:
            print(f"fout {e.code}")
        except Exception as e:
            print(f"geen antwoord ({type(e).__name__}, {time.time()-t:.0f} s)")
    print("\nGeen enkele poging werkte. Controleer of de link precies gelijk is aan die in je andere app,")
    print("en sluit die andere app even: veel abonnementen staan maar één verbinding tegelijk toe.")


def main():
    if "--test" in sys.argv:
        i = sys.argv.index("--test")
        if i + 1 >= len(sys.argv):
            print('Gebruik: python server.py --test "http://jouw-link"'); return
        return run_test(sys.argv[i + 1])

    handler = functools.partial(Handler, directory=BASE_DIR)
    server = http.server.ThreadingHTTPServer((HOST, PORT), handler)
    server.daemon_threads = True
    url = f"http://localhost:{PORT}"
    print(f"Mijn IPTV draait op {url}  (stoppen: Ctrl+C)")
    if "--no-browser" not in sys.argv:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nGestopt.")


if __name__ == "__main__":
    main()
