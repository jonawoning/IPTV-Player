#!/usr/bin/env python3
"""
Mijn IPTV - server
Start met:  python server.py      (of: python3 server.py)
Open daarna http://localhost:8000 in je browser.

Werkt een link niet? Test hem met:
    python server.py --test "http://jouw-link"
Dat laat stap voor stap zien waar het misgaat (wachtwoorden worden verborgen).

Op een eigen server (achter nginx, bijvoorbeeld via Ploi):
    python3 server.py --set-password          # eenmalig: wachtwoord instellen
    python3 server.py --no-browser --require-auth

De server:
  - serveert alleen index.html (en een inlogpagina als er een wachtwoord is ingesteld)
  - stuurt playlists en streams door via /proxy?url=...
    zodat de browser geen last heeft van CORS of http/https-blokkades
  - weigert verzoeken naar interne of privé-adressen, zodat de proxy niet misbruikt kan worden
Hij luistert alleen op 127.0.0.1; van buitenaf kom je er alleen via een webserver zoals nginx.
"""
import getpass
import hashlib
import hmac
import http.cookies
import http.server
import ipaddress
import json
import os
import re
import secrets
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
INDEX_FILE = os.path.join(BASE_DIR, "index.html")
AUTH_FILE = os.path.join(BASE_DIR, ".iptv-auth")
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

# ---------- beveiliging: instellingen ----------
SESSION_COOKIE = "iptv_session"
SESSION_DAYS = int(os.environ.get("IPTV_SESSION_DAYS", "30"))
PBKDF2_ITERATIONS = 600_000
MAX_FAILS_PER_IP = 5          # daarna 15 minuten geblokkeerd
MAX_FAILS_TOTAL = 50          # van alle adressen samen; daarna kan niemand 15 minuten inloggen
FAIL_WINDOW = 15 * 60
ALLOW_PRIVATE = os.environ.get("IPTV_ALLOW_PRIVATE") == "1"  # alleen voor een IPTV-server in je eigen netwerk

PAGE_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com; "
    "img-src 'self' http: https: data: blob:; "
    "media-src 'self' blob:; "
    "connect-src 'self'; "
    "worker-src 'self' blob:; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)
# Voor alles wat via de proxy komt: nooit als pagina met scripts op dit domein uitvoeren.
PROXY_CSP = "default-src 'none'; frame-ancestors 'none'; sandbox"


# ---------- netwerk: IPv4 eerst, en nooit naar interne adressen ----------
class BlockedAddress(socket.gaierror):
    pass


def _is_public(addr):
    ip = ipaddress.ip_address(addr.split("%")[0])
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global


_orig_getaddrinfo = socket.getaddrinfo
def _safe_getaddrinfo(*args, **kwargs):
    """Elke uitgaande verbinding loopt hierlangs, ook na een doorverwijzing. Zo kan de proxy
    nooit bij localhost, het interne netwerk of de metadata-dienst van de hostingpartij."""
    res = _orig_getaddrinfo(*args, **kwargs)
    if not ALLOW_PRIVATE:
        res = [r for r in res if _is_public(r[4][0])]
        if not res:
            raise BlockedAddress(socket.EAI_NONAME, "intern adres geblokkeerd")
    return sorted(res, key=lambda r: 0 if r[0] == socket.AF_INET else 1)
socket.getaddrinfo = _safe_getaddrinfo


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


def safe_content_type(ctype):
    """Geef nooit HTML of scripts van een provider door als iets dat de browser uitvoert."""
    c = ctype.lower()
    if any(x in c for x in ("html", "javascript", "ecmascript", "xhtml")):
        return "application/octet-stream"
    return ctype


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
            if isinstance(reason, BlockedAddress):
                return None, (403, "Dit adres is geblokkeerd: interne en privé-netwerken zijn niet toegestaan")
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


# ---------- wachtwoord en sessies ----------
def hash_password(password):
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password, stored):
    try:
        algo, iterations, salt, dk = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        test = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), int(iterations))
        return hmac.compare_digest(test, bytes.fromhex(dk))
    except (ValueError, TypeError):
        return False


def load_password_hash():
    env = os.environ.get("IPTV_PASSWORD_HASH", "").strip()
    if env:
        return env
    try:
        with open(AUTH_FILE, encoding="utf-8") as f:
            return json.load(f)["password_hash"]
    except (OSError, ValueError, KeyError):
        return None


PASSWORD_HASH = load_password_hash()
AUTH_ENABLED = PASSWORD_HASH is not None

_lock = threading.Lock()
_sessions = {}      # sha256(token) -> verloopt op (unix-tijd); alleen in het geheugen
_fails = {}         # ip -> [aantal, begin van de periode]
_all_fails = []     # tijdstippen van alle mislukte pogingen


def _digest(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_session():
    token = secrets.token_urlsafe(32)
    now = time.time()
    with _lock:
        for k, exp in list(_sessions.items()):
            if exp < now:
                del _sessions[k]
        _sessions[_digest(token)] = now + SESSION_DAYS * 86400
    return token


def session_valid(token):
    if not token:
        return False
    with _lock:
        exp = _sessions.get(_digest(token))
        return exp is not None and exp > time.time()


def end_session(token):
    if token:
        with _lock:
            _sessions.pop(_digest(token), None)


def login_blocked(ip):
    now = time.time()
    with _lock:
        _all_fails[:] = [t for t in _all_fails if now - t < FAIL_WINDOW]
        if len(_all_fails) >= MAX_FAILS_TOTAL:
            return True
        f = _fails.get(ip)
        return bool(f and now - f[1] < FAIL_WINDOW and f[0] >= MAX_FAILS_PER_IP)


def register_fail(ip):
    now = time.time()
    with _lock:
        _all_fails.append(now)
        f = _fails.get(ip)
        if not f or now - f[1] >= FAIL_WINDOW:
            _fails[ip] = [1, now]
        else:
            f[0] += 1


def clear_fails(ip):
    with _lock:
        _fails.pop(ip, None)


LOGIN_PAGE = """<!doctype html>
<html lang="nl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Inloggen · Mijn IPTV</title>
<style>
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:grid;place-items:center;padding:20px;background:#0a0d13;color:#eef2f8;
font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;
background-image:radial-gradient(ellipse at 20% 0%,rgba(109,93,252,.18),transparent 50%)}
form{width:min(400px,100%);background:#111620;border:1px solid #1f2735;border-radius:20px;padding:32px;box-shadow:0 30px 80px rgba(0,0,0,.5)}
.brand{width:46px;height:46px;border-radius:14px;background:linear-gradient(135deg,#6d5dfc,#3fa2ff);display:grid;place-items:center;margin-bottom:16px}
.brand svg{width:20px;height:20px;fill:#fff;margin-left:3px}
h1{margin:0;font-size:24px}
p{margin:4px 0 22px;color:#8b95a8}
label{display:grid;gap:6px;font-weight:700;font-size:13px;color:#8b95a8}
input{width:100%;padding:12px 14px;border-radius:11px;border:1px solid #1f2735;background:#0a0d13;color:inherit;font:inherit}
input:focus-visible,button:focus-visible{outline:2px solid #3fa2ff;outline-offset:2px}
button{width:100%;margin-top:16px;padding:13px;border:0;border-radius:11px;font:inherit;font-weight:700;color:#fff;cursor:pointer;
background:linear-gradient(135deg,#6d5dfc,#3fa2ff)}
.error{background:rgba(255,77,94,.12);color:#ff8a95;border-radius:10px;padding:10px 12px;margin:-8px 0 16px}
</style></head><body>
<form method="post" action="/login">
<div class="brand" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M6 4l14 8-14 8z"/></svg></div>
<h1>Mijn IPTV</h1>
<p>Deze server is beveiligd. Vul je wachtwoord in.</p>
__ERROR__
<label>Wachtwoord<input type="password" name="password" autocomplete="current-password" autofocus required></label>
<button type="submit">Inloggen</button>
</form></body></html>"""

NO_PASSWORD_MSG = ("Deze server is van buitenaf bereikbaar, maar er is nog geen wachtwoord ingesteld. "
                   "Voer op de server uit:  python3 server.py --set-password  en herstart de server.")


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "IPTV"
    sys_version = ""

    def log_message(self, fmt, *args):
        pass  # stil: URL's bevatten je gebruikersnaam en wachtwoord

    # ----- hulpjes -----
    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        # same-origin: binnen de site werkt alles normaal, naar andere sites lekt geen URL (met inloggegevens)
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("X-Robots-Tag", "noindex, nofollow")
        self.send_header("Content-Security-Policy", getattr(self, "csp", PROXY_CSP))
        if self.is_https():
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        super().end_headers()

    def from_local_proxy(self):
        try:
            return ipaddress.ip_address(self.client_address[0]).is_loopback
        except ValueError:
            return False

    def client_ip(self):
        # Alleen nginx op deze machine kan verbinden; die zet het echte adres in X-Real-IP.
        if self.from_local_proxy() and self.headers.get("X-Real-IP"):
            return self.headers["X-Real-IP"].strip()
        return self.client_address[0]

    def is_https(self):
        return self.from_local_proxy() and self.headers.get("X-Forwarded-Proto", "").lower() == "https"

    def is_forwarded(self):
        """Komt dit verzoek van buitenaf (via een webserver) in plaats van van deze computer zelf?"""
        if any(self.headers.get(h) for h in ("X-Forwarded-For", "X-Real-IP", "Forwarded", "X-Forwarded-Proto", "X-Forwarded-Host")):
            return True
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].lower()
        return host not in ("localhost", "127.0.0.1", "[::1]")

    def session_token(self):
        try:
            c = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
        except http.cookies.CookieError:
            return None
        m = c.get(SESSION_COOKIE)
        return m.value if m else None

    def authed(self):
        return not AUTH_ENABLED or session_valid(self.session_token())

    def cookie(self, value, max_age):
        return (f"{SESSION_COOKIE}={value}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}"
                + ("; Secure" if self.is_https() else ""))

    def same_origin(self):
        # Moderne browsers zeggen zelf waar een verzoek vandaan komt; een pagina kan dat niet vervalsen.
        site = self.headers.get("Sec-Fetch-Site")
        if site:
            return site in ("same-origin", "none")
        origin = self.headers.get("Origin")
        if not origin:
            return True  # oudere browsers sturen geen Origin; SameSite-cookie beschermt dan
        if origin == "null":
            return False
        return urllib.parse.urlparse(origin).netloc.lower() == (self.headers.get("Host") or "").lower()

    def send_body(self, code, body, ctype, csp=PROXY_CSP, extra=()):
        self.csp = csp
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def send_text_error(self, code, msg):
        self.send_body(code, msg.encode("utf-8"), "text/plain; charset=utf-8")

    def redirect(self, location, cookie=None):
        self.csp = PROXY_CSP
        self.send_response(303)
        self.send_header("Location", location)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def send_login(self, code=200, error=""):
        page = LOGIN_PAGE.replace("__ERROR__", f'<div class="error" role="alert">{error}</div>' if error else "")
        self.send_body(code, page.encode("utf-8"), "text/html; charset=utf-8",
                       csp="default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")

    # ----- routes -----
    def do_GET(self):
        if not AUTH_ENABLED and self.is_forwarded():
            return self.send_text_error(403, NO_PASSWORD_MSG)
        path = urllib.parse.urlparse(self.path).path
        if path == "/login":
            return self.redirect("/") if self.authed() else self.send_login()
        if not self.authed():
            if path == "/proxy":
                return self.send_text_error(401, "Niet ingelogd")
            return self.redirect("/login")
        if path in ("/", "/index.html"):
            try:
                with open(INDEX_FILE, "rb") as f:
                    body = f.read()
            except OSError:
                return self.send_text_error(500, "index.html ontbreekt")
            return self.send_body(200, body, "text/html; charset=utf-8", csp=PAGE_CSP)
        if path == "/session":
            return self.send_body(200, json.dumps({"auth": AUTH_ENABLED}).encode(), "application/json")
        if path == "/proxy":
            return self.handle_proxy(urllib.parse.urlparse(self.path))
        return self.send_text_error(404, "Niet gevonden")  # verder niets: geen bestanden uit de map

    def do_POST(self):
        if not AUTH_ENABLED and self.is_forwarded():
            return self.send_text_error(403, NO_PASSWORD_MSG)
        if not self.same_origin():
            return self.send_text_error(403, "Verzoek geweigerd")
        path = urllib.parse.urlparse(self.path).path
        if path == "/login":
            return self.handle_login()
        if path == "/logout":
            end_session(self.session_token())
            return self.redirect("/login", cookie=self.cookie("", 0))
        return self.send_text_error(404, "Niet gevonden")

    def handle_login(self):
        if not AUTH_ENABLED:
            return self.redirect("/")
        ip = self.client_ip()
        if login_blocked(ip):
            print(f"[login] geblokkeerd: {ip}")
            return self.send_login(429, "Te veel mislukte pogingen. Probeer het over 15 minuten opnieuw.")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 4096:
            return self.send_login(400, "Ongeldig verzoek.")
        form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
        if verify_password(form.get("password", [""])[0], PASSWORD_HASH):
            clear_fails(ip)
            print(f"[login] gelukt vanaf {ip}")
            return self.redirect("/", cookie=self.cookie(new_session(), SESSION_DAYS * 86400))
        register_fail(ip)
        print(f"[login] mislukt vanaf {ip}")
        time.sleep(1)
        return self.send_login(401, "Onjuist wachtwoord.")

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
                self.send_body(200, body, safe_content_type(ctype) or "text/plain; charset=utf-8")
            else:
                self.csp = PROXY_CSP
                self.send_response(resp.status)
                if ctype:
                    self.send_header("Content-Type", safe_content_type(ctype))
                for h in ("Content-Length", "Content-Range", "Accept-Ranges"):
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


# ---------- wachtwoord instellen ----------
def set_password():
    print("Stel het wachtwoord in waarmee je straks inlogt op deze server.")
    pw = getpass.getpass("Nieuw wachtwoord (minstens 12 tekens): ")
    if len(pw) < 12:
        print("Te kort. Gebruik minstens 12 tekens, bijvoorbeeld een zin van een paar woorden.")
        sys.exit(1)
    if getpass.getpass("Nog een keer: ") != pw:
        print("De wachtwoorden zijn niet gelijk.")
        sys.exit(1)
    fd = os.open(AUTH_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"password_hash": hash_password(pw)}, f)
    try:
        os.chmod(AUTH_FILE, 0o600)
    except OSError:
        pass
    print(f"Opgeslagen in {AUTH_FILE} (alleen een versleutelde hash, niet het wachtwoord zelf).")
    print("Herstart de server. Iedereen die nu is ingelogd, moet opnieuw inloggen.")


# ---------- testmodus ----------
def run_test(url):
    print(f"\nTest van: {mask(url)}\n")
    p = urllib.parse.urlparse(url)
    host, port = p.hostname, p.port or (443 if p.scheme == "https" else 80)

    print("1. Adres opzoeken…", end=" ", flush=True)
    try:
        ips = sorted({a[4][0] for a in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})
        print("ok:", ", ".join(ips))
    except BlockedAddress:
        print("GEBLOKKEERD: dit is een intern of privé-adres. Zet IPTV_ALLOW_PRIVATE=1 als dat de bedoeling is."); return
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
    if "--set-password" in sys.argv:
        return set_password()
    if "--test" in sys.argv:
        i = sys.argv.index("--test")
        if i + 1 >= len(sys.argv):
            print('Gebruik: python server.py --test "http://jouw-link"'); return
        return run_test(sys.argv[i + 1])
    if "--require-auth" in sys.argv and not AUTH_ENABLED:
        print("Geen wachtwoord ingesteld. Voer eerst uit:  python3 server.py --set-password")
        sys.exit(1)

    port = PORT
    if "--port" in sys.argv:
        i = sys.argv.index("--port")
        try:
            port = int(sys.argv[i + 1])
        except (IndexError, ValueError):
            print("Gebruik: python server.py --port 8010"); sys.exit(1)

    try:
        server = http.server.ThreadingHTTPServer((HOST, port), Handler)
    except OSError:
        print(f"Poort {port} is al in gebruik door een ander programma. Kies een andere, bijvoorbeeld:  --port 8010")
        sys.exit(1)
    server.daemon_threads = True
    url = f"http://localhost:{port}"
    print(f"Mijn IPTV draait op {url}  (stoppen: Ctrl+C)")
    print("Inloggen met wachtwoord: " + ("aan" if AUTH_ENABLED else "uit (alleen bereikbaar vanaf deze computer)"))
    if "--no-browser" not in sys.argv:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nGestopt.")


if __name__ == "__main__":
    main()
