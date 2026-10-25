#!/usr/bin/env python3
"""Taberna Requests: pedidos de manga y novelas ligeras.

AniList para el catálogo, Komga para el login, Prowlarr para buscar en los
trackers y qBittorrent para descargar. Solo librería estándar (Python 3.8+).

Uso: python app.py [ruta/a/config.json]
"""
import base64
import http.server
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
STATIC_FILES = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css"}
SESSION_SECONDS = 30 * 24 * 3600
COOKIE = "taberna_session"
UA = "taberna-requests/0.1"

DEFAULT_KINDS = {
    # wrong_categories: categorías de Prowlarr que indican el otro tipo (7020 = ebook, 7030 = cómic/manga)
    "manga": {"newznab": [7030, 7000], "qbit_category": "manga", "wrong_categories": [7020],
              "formats": ["MANGA", "ONE_SHOT"], "prefer": ["cbz", "cbr", "zip"],
              "avoid": ["audiobook", "light novel", "novel", "ln", "novela"]},
    "novel": {"newznab": [7020, 7000], "qbit_category": "novels", "wrong_categories": [7030],
              "formats": ["NOVEL"], "prefer": ["epub"],
              "avoid": ["web novel", "wn", "audiobook", "mtl", "machine translation", "manga", "scanlated", "scanlation"]},
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  email TEXT PRIMARY KEY, is_admin INT DEFAULT 0,
  auto_approve INT DEFAULT 0, can_choose INT DEFAULT 0, created REAL);
CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, email TEXT, expires REAL);
CREATE TABLE IF NOT EXISTS requests(
  id INTEGER PRIMARY KEY, anilist_id INT, kind TEXT, title TEXT, titles TEXT,
  cover TEXT, volumes INT, status TEXT, requested_by TEXT, created REAL, updated REAL,
  release_title TEXT, release_indexer TEXT, note TEXT);
"""

ANILIST_QUERY = """
query($q: String, $formats: [MediaFormat]) {
  Page(perPage: 24) {
    media(search: $q, type: MANGA, format_in: $formats, sort: SEARCH_MATCH, isAdult: false) {
      id idMal format status volumes chapters siteUrl synonyms
      title { romaji english native }
      coverImage { large }
      startDate { year }
      description(asHtml: false)
    }
  }
}"""


class UpstreamError(Exception):
    """Un servicio externo (Komga, Prowlarr, qBittorrent, AniList) falló."""


def load_config(path):
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.setdefault("host", "127.0.0.1")
    cfg.setdefault("port", 8787)
    cfg.setdefault("base_path", "")
    cfg.setdefault("db", os.path.join(HERE, "data.db"))
    cfg.setdefault("admins", [])
    cfg.setdefault("local_users", {})
    cfg.setdefault("auto_grab", True)
    cfg.setdefault("max_queries", 2)
    cfg.setdefault("secure_cookies", False)
    cfg.setdefault("anilist_url", "https://graphql.anilist.co")
    cfg.setdefault("jikan_url", "https://api.jikan.moe/v4")  # vacío = no usar MyAnimeList
    cfg.setdefault("prefer_languages", ["Latino", "Español", "English"])  # en orden de preferencia
    # revisión periódica: aprobados sin descargar y obras en emisión
    cfg.setdefault("recheck_hours", 24)
    cfg.setdefault("recheck_batch", 5)
    cfg.setdefault("recheck_pause", 30)  # segundos entre un pedido y otro
    kinds = {k: dict(v) for k, v in DEFAULT_KINDS.items()}
    for k, v in cfg.get("kinds", {}).items():
        kinds.setdefault(k, {}).update(v)
    cfg["kinds"] = kinds
    return cfg


# ---------------------------------------------------------------- HTTP saliente

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # las redirecciones las maneja fetch() para poder ver un magnet:


_opener = urllib.request.build_opener(_NoRedirect)


def fetch(method, url, body=None, headers=None, timeout=30, follow=True):
    """Petición HTTP. Sigue redirecciones http(s); devuelve (status, headers, bytes)."""
    headers = {"User-Agent": UA, **(headers or {})}
    for _ in range(5):
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with _opener.open(req, timeout=timeout) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            status, hdrs, data = e.code, e.headers, e.read()
            e.close()
        except (urllib.error.URLError, OSError) as e:
            raise UpstreamError(f"No se pudo conectar con {url.split('?')[0]}: {e}")
        loc = hdrs.get("Location") if status in (301, 302, 303, 307, 308) else None
        if not (follow and loc):
            return status, hdrs, data
        nxt = urllib.parse.urljoin(url, loc)
        if not nxt.startswith(("http://", "https://")):
            return status, hdrs, data  # magnet: u otro esquema, lo decide quien llama
        url = nxt
        if status == 303:
            method, body = "GET", None
    raise UpstreamError("Demasiadas redirecciones")


def get_json(method, url, payload=None, headers=None, timeout=30):
    h = {"Accept": "application/json", **(headers or {})}
    body = None
    if payload is not None:
        body = json.dumps(payload).encode()
        h["Content-Type"] = "application/json"
    status, _, data = fetch(method, url, body, h, timeout)
    if status >= 400:
        # sin la query string: ahí podría ir una clave
        raise UpstreamError(f"{url.split('?')[0]} respondió {status}")
    return json.loads(data or b"null")


def multipart(fields, files):
    boundary = "----taberna" + uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    for k, (filename, data, ctype) in files.items():
        head = (f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; filename="{filename}"\r\n'
                f"Content-Type: {ctype}\r\n\r\n").encode()
        parts.append(head + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), "multipart/form-data; boundary=" + boundary


# ---------------------------------------------------------------- servicios

def komga_login(cfg, username, password):
    """Devuelve (email, es_admin) si Komga acepta las credenciales, None si no."""
    url = cfg["komga"]["url"].rstrip("/") + "/api/v2/users/me"
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    status, _, data = fetch("GET", url, headers={
        "Authorization": "Basic " + token, "Accept": "application/json",
        "X-Requested-With": "XMLHttpRequest"})
    if status in (401, 403):
        return None
    if status >= 400:
        raise UpstreamError(f"Komga respondió {status}")
    me = json.loads(data)
    return (me.get("email") or username).lower(), "ADMIN" in me.get("roles", [])


_KOMGA_CACHE = {"at": 0, "series": []}
KOMGA_CACHE_SECONDS = 300


def komga_series(cfg):
    """Todas las series de Komga (nombre y cantidad de libros), cacheadas 5 minutos.
    Usa la cuenta de servicio de config: komga.username/password. Sin ella, no hay disponibilidad."""
    k = cfg.get("komga") or {}
    if not k.get("url") or not k.get("username"):
        return []
    if time.time() - _KOMGA_CACHE["at"] < KOMGA_CACHE_SECONDS:
        return _KOMGA_CACHE["series"]
    token = base64.b64encode(f"{k['username']}:{k.get('password', '')}".encode()).decode()
    auth = {"Authorization": "Basic " + token, "X-Requested-With": "XMLHttpRequest"}
    base = k["url"].rstrip("/")
    libraries = {lib.get("id"): lib.get("name") for lib in get_json("GET", base + "/api/v1/libraries", headers=auth) or []}
    data = get_json("GET", base + "/api/v1/series?unpaged=true", headers=auth)
    series = [{"names": [s.get("name"), (s.get("metadata") or {}).get("title")],
               "books": s.get("booksCount") or 0, "library": libraries.get(s.get("libraryId"))}
              for s in (data or {}).get("content", [])]
    _KOMGA_CACHE.update(at=time.time(), series=series)
    return series


def availability(cfg, kind, titles, volumes):
    """Cuántos libros hay en Komga de esta obra: None si no está.
    Coincide si algún título de AniList/MAL aparece entero dentro del nombre de la serie."""
    # nombres de biblioteca de Komga para este tipo, ej. {"manga": ["Manga"], "novel": ["Novels"]}
    libs = ((cfg.get("komga") or {}).get("libraries") or {}).get(kind) or []
    names = [n for n in (norm(t) for t in titles) if len(n) >= 4]
    books = 0
    for s in komga_series(cfg):
        if libs and s["library"] not in libs:
            continue
        sn = [f" {norm(x)} " for x in s["names"] if x]
        if any(f" {n} " in x for n in names for x in sn):
            books = max(books, s["books"])
    if not books:
        return None
    return {"books": books, "volumes": volumes, "complete": bool(volumes) and books >= volumes}


def anilist_search(cfg, q, kind):
    data = get_json("POST", cfg["anilist_url"], {
        "query": ANILIST_QUERY,
        "variables": {"q": q, "formats": cfg["kinds"][kind]["formats"]}})
    out = []
    for m in (data or {}).get("data", {}).get("Page", {}).get("media", []):
        t = m.get("title") or {}
        out.append({
            "anilist_id": m["id"], "mal_id": m.get("idMal"), "kind": kind,
            "title": t.get("english") or t.get("romaji") or t.get("native"),
            "titles": [x for x in [t.get("english"), t.get("romaji"), t.get("native"), *m.get("synonyms", [])] if x],
            "cover": (m.get("coverImage") or {}).get("large"),
            "volumes": m.get("volumes"), "chapters": m.get("chapters"),
            "status": m.get("status"), "format": m.get("format"),
            "year": (m.get("startDate") or {}).get("year"),
            "description": m.get("description"), "url": m.get("siteUrl")})
    return out


def mal_titles(cfg, mal_id):
    """Títulos alternativos de MyAnimeList vía Jikan. Es un extra: si falla, sigue sin ellos."""
    try:
        d = (get_json("GET", f"{cfg['jikan_url'].rstrip('/')}/manga/{int(mal_id)}", timeout=15) or {}).get("data") or {}
    except (UpstreamError, ValueError):
        return []
    titles = [t.get("title") for t in d.get("titles") or []]
    titles += [d.get("title"), d.get("title_english"), d.get("title_japanese"), *(d.get("title_synonyms") or [])]
    return [t for t in titles if t]


# Pistas de idioma en el nombre del release (ideas tomadas del LanguageParser de
# Sonarr/Radarr, pero "LAT" es latino y no letón). Solo importan español e inglés;
# japonés se marca para reconocer los raws.
LANGS = [
    ("Latino", r"lat|latino|latam|es[-_ ]?(mx|419)"),
    ("Español", r"esp|espa[ñn]ol|spa|spanish|castellano|es[-_ ]es|novela|tomo|volumen|traducci[oó]n"),
    # inglés: también las editoriales oficiales y los grupos que suben releases digitales en inglés
    ("English", r"eng|ing|english|light novel|yen press|j[-_ ]?novel|seven seas|vertical|kodansha|viz|one peace"
                r"|digital|scanlation|danke[-_ ]empire|lucaz|1r0n|zaibatsu|oak|akraa|baka[-_ ]tsuki|colored council"),
    ("Japonés", r"jpn|jap|japanese|japon[eé]s|raw|raws|第\s*\d+|\d+\s*巻"),
]
_LANG_RE = [(name, re.compile(rf"(?<![a-z0-9])({rx})(?![a-z0-9])")) for name, rx in LANGS]
# "EN" y "ES" sueltos solo cuentan en mayúsculas, como en Radarr ("es" en minúscula es una palabra común)
_UPPER_RE = [("English", re.compile(r"(?<![A-Za-z0-9])EN(?![A-Za-z0-9])")),
             ("Español", re.compile(r"(?<![A-Za-z0-9])ES(?![A-Za-z0-9])"))]


def detect_languages(title):
    t = title or ""
    found = {name for name, rx in _LANG_RE if rx.search(t.lower())}
    found |= {name for name, rx in _UPPER_RE if rx.search(t)}
    return [name for name, _ in LANGS if name in found]


def prowlarr_search(cfg, kind, queries):
    p = cfg["prowlarr"]
    base = p["url"].rstrip("/")
    seen, out = set(), []
    for q in queries:
        params = [("query", q), ("type", "search"), ("limit", "100")]
        params += [("categories", str(c)) for c in cfg["kinds"][kind]["newznab"]]
        params += [("indexerIds", str(i)) for i in p.get("indexer_ids", [])]
        items = get_json("GET", base + "/api/v1/search?" + urllib.parse.urlencode(params),
                         headers={"X-Api-Key": p["api_key"]}, timeout=120)
        for it in items or []:
            guid = it.get("guid")
            if it.get("protocol", "torrent") != "torrent" or guid in seen:
                continue
            seen.add(guid)
            r = {k: it.get(k) for k in (
                "guid", "title", "size", "seeders", "leechers", "indexer", "indexerId",
                "downloadUrl", "magnetUrl", "infoUrl", "publishDate")}
            r["categories"] = [c.get("id") for c in it.get("categories") or [] if isinstance(c, dict)]
            out.append(r)
    return out


def norm(s):
    return " ".join(re.sub(r"[^0-9a-z]+", " ", (s or "").lower()).split())


# "v01-08", "Vol. 9", "Volumes 01-18", "Tomo 1 a 10", "第01-19巻". El "v2" de "01v2" no
# cuenta (es versión, no volumen) porque la v tiene que ir separada.
_VOL_RE = re.compile(r"(?<![a-z0-9])(?:v|vol|vols|volume|volumes|tomo|tomos)\.?\s*(\d{1,3})"
                     r"(?:\s*(?:-|~|to|a)\s*(?:v|vol|volume|tomo)?\.?\s*(\d{1,3}))?(?![0-9])", re.I)
_JP_VOL_RE = re.compile(r"第\s*(\d{1,3})(?:\s*-\s*(\d{1,3}))?\s*巻")


def parse_volumes(title):
    """(desde, hasta) de los volúmenes que trae el release, o None si el nombre no lo dice."""
    ranges = [(int(a), int(b or a)) for rx in (_VOL_RE, _JP_VOL_RE) for a, b in rx.findall(title or "")]
    if not ranges:
        return None
    return min(a for a, _ in ranges), max(b for _, b in ranges)


_COMPLETE_RE = re.compile(r"(?<![a-z])(complete|completo|completa|batch)(?![a-z])", re.I)


def rank(results, titles, prefer, avoid=(), langs=(), wrong_categories=(), have=0, total=None):
    """Marca cada release y los ordena: coincide con el título, no es algo a evitar
    (web novel, audiolibro, el otro tipo de obra…), tiene seeders, trae volúmenes que
    faltan (si ya tenés `have`), idioma preferido, completo, cuántos volúmenes trae,
    formato preferido y seeders. `total` = volúmenes según AniList (None si no se sabe)."""
    names = [n for n in (norm(t) for t in titles) if len(n) >= 3]
    avoid = [norm(a) for a in avoid]
    for r in results:
        vol = parse_volumes(r["title"])
        r["vol_from"], r["vol_to"] = vol or (None, None)
        r["new"] = bool(have and vol and vol[1] > have)        # trae algo que falta
        r["only_new"] = bool(have and vol and vol[0] > have)   # trae solo lo que falta, sin repetir
        r["span"] = vol[1] - vol[0] + 1 if vol else 0
        # completo: del 1 al último según AniList, o lo dice el nombre ("Complete", "Batch"…)
        r["complete"] = bool((total and vol and vol[0] <= 1 and vol[1] >= total)
                             or _COMPLETE_RE.search(r["title"] or ""))
        t = norm(r["title"])
        r["match"] = any(f" {n} " in f" {t} " for n in names)
        r["preferred"] = any(p in t.split() for p in prefer)
        r["wrong_kind"] = any(c in wrong_categories for c in r.get("categories") or [])
        r["avoid"] = r["wrong_kind"] or any(f" {a} " in f" {t} " for a in avoid)
        r["languages"] = detect_languages(r["title"])
        r["lang_score"] = max((len(langs) - langs.index(l) for l in r["languages"] if l in langs), default=0)
        # sin idioma detectado puede ser cualquiera; con idioma detectado tiene que ser uno de los tuyos
        r["lang_ok"] = not langs or r["lang_score"] > 0 or not r["languages"]
    results.sort(key=lambda r: (r["match"], not r["avoid"], (r.get("seeders") or 0) > 0,
                                r["new"] or not have, r["only_new"],
                                r["lang_score"], r["complete"], r["span"],
                                r["preferred"], r.get("seeders") or 0), reverse=True)
    return results


def pick_best(results, have=0, skip_title=None):
    """El primero (ya ordenados) que coincide, no es a evitar, está en tus idiomas y tiene
    seeders. Si ya tenés volúmenes, solo uno que traiga únicamente los que faltan
    (un pack completo repetiría lo que ya está: ese se elige a mano)."""
    return next((r for r in results if r["match"] and not r.get("avoid") and r.get("lang_ok", True)
                 and (r.get("seeders") or 0) > 0 and r["title"] != skip_title
                 and (not have or r.get("only_new"))), None)


def get_torrent(rel):
    if rel.get("magnetUrl"):
        return {"urls": rel["magnetUrl"]}
    status, hdrs, data = fetch("GET", rel["downloadUrl"], timeout=60)
    loc = hdrs.get("Location") or ""
    if loc.startswith("magnet:"):
        return {"urls": loc}
    if status >= 400 or not data:
        raise UpstreamError(f"No se pudo bajar el .torrent (respuesta {status})")
    return {"file": data}


def qbit_add(cfg, torrent, category):
    q = cfg["qbittorrent"]
    base = q["url"].rstrip("/")
    ref = {"Referer": base + "/"}  # qBittorrent compara Referer/Origin con el Host (CSRF)
    form = urllib.parse.urlencode({"username": q["username"], "password": q["password"]}).encode()
    status, hdrs, data = fetch("POST", base + "/api/v2/auth/login", form, {
        **ref, "Content-Type": "application/x-www-form-urlencoded"}, follow=False)
    if status != 200 or b"Ok" not in data:
        raise UpstreamError("qBittorrent rechazó el login (revisá usuario y contraseña)")
    cookie = "; ".join(c.split(";")[0] for c in (hdrs.get_all("Set-Cookie") or []))
    fields = {"category": category, "autoTMM": "true"}
    files = {}
    if "urls" in torrent:
        fields["urls"] = torrent["urls"]
    else:
        files["torrents"] = ("release.torrent", torrent["file"], "application/x-bittorrent")
    body, ctype = multipart(fields, files)
    status, _, data = fetch("POST", base + "/api/v2/torrents/add", body, {
        **ref, "Cookie": cookie, "Content-Type": ctype}, timeout=60, follow=False)
    if status != 200 or data.strip() == b"Fails.":
        raise UpstreamError(f"qBittorrent no aceptó el torrent (respuesta {status})")


# ---------------------------------------------------------------- base de datos

def db(cfg):
    con = sqlite3.connect(cfg["db"], timeout=10)
    con.row_factory = sqlite3.Row
    return con


def init_db(cfg):
    with db(cfg) as con:
        con.executescript(SCHEMA)
        cols = {r[1] for r in con.execute("PRAGMA table_info(requests)")}
        if "releasing" not in cols:  # bases creadas antes del seguimiento de obras en emisión
            con.execute("ALTER TABLE requests ADD COLUMN releasing INT DEFAULT 0")


def request_row(con, req_id):
    row = con.execute("SELECT * FROM requests WHERE id=?", (req_id,)).fetchone()
    return dict(row, titles=json.loads(row["titles"] or "[]")) if row else None


def set_request(cfg, req_id, **fields):
    fields["updated"] = time.time()
    cols = ", ".join(f"{k}=?" for k in fields)
    with db(cfg) as con:
        con.execute(f"UPDATE requests SET {cols} WHERE id=?", (*fields.values(), req_id))


# Resultados de la última búsqueda por pedido. Quedan en el servidor porque
# las URLs de descarga de Prowlarr llevan la API key.
RESULTS = {}
RESULTS_LOCK = threading.Lock()


def books_in_komga(cfg, req):
    """Cuántos libros de este pedido hay en Komga (0 si no está o si Komga no responde)."""
    try:
        av = availability(cfg, req["kind"], req["titles"], req["volumes"])
    except UpstreamError:
        return 0
    return av["books"] if av else 0


def search_request(cfg, req):
    """Busca en Prowlarr. Devuelve (resultados ordenados, libros que ya hay en Komga)."""
    queries = list(dict.fromkeys(t for t in req["titles"] if norm(t)))[:cfg["max_queries"]]
    k = cfg["kinds"][req["kind"]]
    have = books_in_komga(cfg, req)
    results = rank(prowlarr_search(cfg, req["kind"], queries or [req["title"]]),
                   req["titles"], k["prefer"], k.get("avoid", []), cfg["prefer_languages"],
                   k.get("wrong_categories", []), have, req.get("volumes"))
    with RESULTS_LOCK:
        RESULTS[req["id"]] = results
    return results, have


def grab(cfg, req, rel, actor):
    qbit_add(cfg, get_torrent(rel), cfg["kinds"][req["kind"]]["qbit_category"])
    set_request(cfg, req["id"], status="grabbed", release_title=rel["title"],
                release_indexer=rel.get("indexer"), note=f"Elegido por {actor}")


def auto_grab(cfg, req_id):
    """Busca y baja el mejor release. En un pedido ya descargado (obra en emisión) solo
    baja si hay volúmenes posteriores a los que están en Komga."""
    try:
        with db(cfg) as con:
            req = request_row(con, req_id)
        if not req:
            return
        followup = req["status"] == "grabbed"
        results, have = search_request(cfg, req)
        every = f"se vuelve a revisar cada {cfg['recheck_hours']} h"
        if followup and not have:
            # sin saber cuántos hay, bajar "lo mejor" podría repetir lo mismo una y otra vez
            set_request(cfg, req_id, note="No lo encuentro en Komga, así que no busco volúmenes nuevos solo.")
            return
        best = pick_best(results, have, skip_title=req["release_title"])
        if best:
            grab(cfg, req, best, "búsqueda automática" + (" (volúmenes nuevos)" if have else ""))
        elif have and any(r["new"] and r["match"] for r in results):
            set_request(cfg, req_id, note=f"Hay volúmenes nuevos, pero solo en packs que repiten los {have} que ya tenés: elegí a mano.")
        elif followup:
            set_request(cfg, req_id, note=f"Sin volúmenes nuevos todavía (tenés {have}); {every}.")
        else:
            set_request(cfg, req_id, note=f"La búsqueda automática no encontró nada seguro: elegí a mano o esperá ({every}).")
    except Exception as e:  # el hilo no tiene a quién avisar: lo deja anotado en el pedido
        set_request(cfg, req_id, note=f"La búsqueda automática falló: {e}")


def recheck(cfg):
    """Una ronda: reintenta los aprobados sin descargar y busca volúmenes nuevos de lo que
    está en emisión. Pocos por ronda y con pausa, para no gastar el cupo de los trackers."""
    with db(cfg) as con:
        ids = [r["id"] for r in con.execute(
            "SELECT id FROM requests WHERE status='approved' OR (status='grabbed' AND releasing=1)"
            " ORDER BY updated LIMIT ?", (cfg["recheck_batch"],))]
    for req_id in ids:
        auto_grab(cfg, req_id)
        time.sleep(cfg["recheck_pause"])


def recheck_loop(cfg):
    while True:
        time.sleep(cfg["recheck_hours"] * 3600)
        try:
            recheck(cfg)
        except Exception as e:  # que un error no apague el seguimiento
            sys.stderr.write(f"Revisión periódica falló: {e}\n")


def after_create(cfg, req_id, mal_id, grab):
    """En segundo plano: suma los títulos de MAL (van al final; sirven para reconocer
    releases, no para buscar más veces) y, si el pedido ya está aprobado, busca y baja."""
    if mal_id:
        extra = [t[:200] for t in mal_titles(cfg, mal_id)]
        if extra:
            with db(cfg) as con:
                req = request_row(con, req_id)
            if req:
                titles = list(dict.fromkeys(req["titles"] + extra))[:30]
                set_request(cfg, req_id, titles=json.dumps(titles, ensure_ascii=False))
    if grab and cfg["auto_grab"]:
        auto_grab(cfg, req_id)


def start_auto_grab(cfg, req_id):
    if cfg["auto_grab"]:
        threading.Thread(target=auto_grab, args=(cfg, req_id), daemon=True).start()


def public_result(r):
    return {k: r.get(k) for k in ("guid", "title", "size", "seeders", "leechers", "indexer",
                                   "infoUrl", "publishDate", "match", "preferred", "avoid", "wrong_kind", "languages",
                                   "vol_from", "vol_to", "new", "only_new", "complete")}


# ---------------------------------------------------------------- servidor web

class App(http.server.BaseHTTPRequestHandler):
    cfg = None
    server_version = "TabernaRequests"

    def log_message(self, fmt, *args):
        sys.stderr.write(f"{self.command} {self.path.split('?')[0]}\n")

    # --- utilidades
    def send_json(self, status, obj, cookie=None):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > 1_000_000:
            raise ValueError("Cuerpo demasiado grande")
        return json.loads(self.rfile.read(length) or b"{}")

    def route(self):
        path = urllib.parse.urlsplit(self.path).path
        bp = self.cfg["base_path"].rstrip("/")
        if bp:
            if path == bp:
                return None  # sin barra final las rutas relativas del frontend no andan
            if path.startswith(bp + "/"):
                path = path[len(bp):]
        return path

    def cookie(self, value, max_age):
        parts = [f"{COOKIE}={value}", f"Path={self.cfg['base_path'].rstrip('/') or '/'}",
                 f"Max-Age={max_age}", "HttpOnly", "SameSite=Lax"]
        if self.cfg["secure_cookies"]:
            parts.append("Secure")
        return "; ".join(parts)

    def current_user(self):
        raw = self.headers.get("Cookie") or ""
        m = re.search(rf"(?:^|;\s*){COOKIE}=([^;]+)", raw)
        if not m:
            return None
        with db(self.cfg) as con:
            row = con.execute(
                "SELECT u.* FROM sessions s JOIN users u ON u.email=s.email WHERE s.token=? AND s.expires>?",
                (m.group(1), time.time())).fetchone()
        return dict(row) if row else None

    # --- entrada
    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def dispatch(self, method):
        path = self.route()
        if path is None:
            self.send_response(301)
            self.send_header("Location", self.cfg["base_path"].rstrip("/") + "/")
            self.end_headers()
            return
        if not path.startswith("/api/"):
            return self.static(path) if method == "GET" else self.send_json(405, {"error": "Método no permitido"})
        if method == "POST" and "application/json" not in (self.headers.get("Content-Type") or ""):
            return self.send_json(415, {"error": "Se esperaba JSON"})  # también frena CSRF por formulario
        try:
            self.api(method, path.strip("/").split("/")[1:])
        except UpstreamError as e:
            self.send_json(502, {"error": str(e)})
        except (ValueError, KeyError, TypeError) as e:
            self.send_json(400, {"error": f"Pedido inválido: {e}"})

    def static(self, path):
        name = STATIC_FILES.get(path)
        if not name:
            return self.send_json(404, {"error": "No existe"})
        with open(os.path.join(STATIC, name), "rb") as f:
            data = f.read()
        ctype = {"html": "text/html", "js": "text/javascript", "css": "text/css"}[name.rsplit(".", 1)[1]]
        self.send_response(200)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def api(self, method, parts):
        cfg = self.cfg
        if parts == ["login"] and method == "POST":
            return self.login()
        user = self.current_user()
        if not user:
            return self.send_json(401, {"error": "Iniciá sesión"})

        if parts == ["logout"] and method == "POST":
            with db(cfg) as con:
                con.execute("DELETE FROM sessions WHERE email=?", (user["email"],))
            return self.send_json(200, {"ok": True}, cookie=self.cookie("", 0))

        if parts == ["me"]:
            return self.send_json(200, user)

        if parts == ["anilist"]:
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            q, kind = qs.get("q", [""])[0].strip(), qs.get("kind", ["manga"])[0]
            if not q or kind not in cfg["kinds"]:
                return self.send_json(400, {"error": "Falta qué buscar"})
            items = anilist_search(cfg, q, kind)
            with db(cfg) as con:
                existing = {r["anilist_id"]: r["status"] for r in con.execute(
                    "SELECT anilist_id, status FROM requests WHERE kind=? AND status!='denied'", (kind,))}
            try:
                komga_series(cfg)  # precarga; si Komga falla, se busca igual sin disponibilidad
                have_komga = True
            except UpstreamError:
                have_komga = False
            for it in items:
                it["request_status"] = existing.get(it["anilist_id"])
                it["available"] = availability(cfg, kind, it["titles"], it["volumes"]) if have_komga else None
            return self.send_json(200, items)

        if parts == ["requests"] and method == "GET":
            with db(cfg) as con:
                rows = [dict(r, titles=json.loads(r["titles"] or "[]"))
                        for r in con.execute("SELECT * FROM requests ORDER BY created DESC LIMIT 500")]
            try:
                komga_series(cfg)  # una sola consulta (cacheada) para todos los pedidos
                have_komga = True
            except UpstreamError:
                have_komga = False
            for r in rows:  # lo que ya hay en Komga: "Disponible" / "Parcial" en vez de "Descargando"
                r["available"] = availability(cfg, r["kind"], r["titles"], r["volumes"]) if have_komga else None
            return self.send_json(200, rows)

        if parts == ["requests"] and method == "POST":
            return self.create_request(user, self.body())

        if len(parts) == 3 and parts[0] == "requests" and parts[1].isdigit() and method == "POST":
            return self.request_action(user, int(parts[1]), parts[2])

        if parts == ["users"] and user["is_admin"]:
            with db(cfg) as con:
                return self.send_json(200, [dict(r) for r in con.execute("SELECT * FROM users ORDER BY email")])

        if len(parts) == 2 and parts[0] == "users" and method == "POST" and user["is_admin"]:
            b = self.body()
            flags = {k: int(bool(b[k])) for k in ("is_admin", "auto_approve", "can_choose") if k in b}
            if flags:
                with db(cfg) as con:
                    con.execute(f"UPDATE users SET {', '.join(f'{k}=?' for k in flags)} WHERE email=?",
                                (*flags.values(), urllib.parse.unquote(parts[1])))
            return self.send_json(200, {"ok": True})

        return self.send_json(404, {"error": "No existe"})

    def login(self):
        cfg = self.cfg
        b = self.body()
        username, password = str(b.get("username", "")).strip(), str(b.get("password", ""))
        result = None
        local = cfg["local_users"].get(username)
        if local is not None:
            if secrets.compare_digest(str(local.get("password", "")), password):
                result = (username.lower(), bool(local.get("admin")))
        elif cfg.get("komga", {}).get("url"):
            result = komga_login(cfg, username, password)
        if not result:
            time.sleep(1)  # frena un poco los intentos a ciegas
            return self.send_json(401, {"error": "Usuario o contraseña incorrectos"})
        email, admin = result
        admin = admin or email in [a.lower() for a in cfg["admins"]]
        token = secrets.token_urlsafe(32)
        with db(cfg) as con:
            con.execute("INSERT OR IGNORE INTO users(email, created) VALUES(?, ?)", (email, time.time()))
            if admin:
                con.execute("UPDATE users SET is_admin=1 WHERE email=?", (email,))
            con.execute("DELETE FROM sessions WHERE expires<?", (time.time(),))
            con.execute("INSERT INTO sessions VALUES(?, ?, ?)", (token, email, time.time() + SESSION_SECONDS))
        return self.send_json(200, {"ok": True}, cookie=self.cookie(token, SESSION_SECONDS))

    def create_request(self, user, b):
        cfg = self.cfg
        kind = b["kind"]
        if kind not in cfg["kinds"]:
            raise ValueError("tipo desconocido")
        titles = [str(t)[:200] for t in b.get("titles", [])][:20]
        cover = str(b.get("cover") or "")
        if not cover.startswith("https://"):
            cover = ""
        title = str(b.get("title") or (titles[0] if titles else ""))[:200]
        if not title:
            raise ValueError("falta el título")
        auto = bool(user["is_admin"] or user["auto_approve"])
        with db(cfg) as con:
            dup = con.execute("SELECT id, status FROM requests WHERE anilist_id=? AND kind=? AND status!='denied'",
                              (int(b["anilist_id"]), kind)).fetchone()
            if dup:
                return self.send_json(409, {"error": "Ya está pedido", "id": dup["id"], "status": dup["status"]})
            cur = con.execute(
                "INSERT INTO requests(anilist_id, kind, title, titles, cover, volumes, status, requested_by,"
                " created, updated, releasing) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (int(b["anilist_id"]), kind, title, json.dumps(titles or [title], ensure_ascii=False), cover,
                 b.get("volumes"), "approved" if auto else "pending", user["email"], time.time(), time.time(),
                 int(b.get("status") == "RELEASING")))  # en emisión en AniList: se siguen los volúmenes nuevos
            req_id = cur.lastrowid
        # MAL puede tardar varios segundos: se completa después de responder.
        mal_id = b.get("mal_id") if cfg["jikan_url"] else None
        threading.Thread(target=after_create, args=(cfg, req_id, mal_id, auto), daemon=True).start()
        return self.send_json(201, {"id": req_id, "status": "approved" if auto else "pending"})

    def request_action(self, user, req_id, action):
        cfg = self.cfg
        with db(cfg) as con:
            req = request_row(con, req_id)
        if not req:
            return self.send_json(404, {"error": "Ese pedido no existe"})
        admin = bool(user["is_admin"])
        owner = req["requested_by"] == user["email"]
        chooser = admin or (owner and user["can_choose"])

        if action in ("approve", "deny"):
            if not admin:
                return self.send_json(403, {"error": "Solo el admin puede hacer eso"})
            set_request(cfg, req_id, status="approved" if action == "approve" else "denied")
            if action == "approve":
                start_auto_grab(cfg, req_id)
            return self.send_json(200, {"ok": True})

        if action == "follow":  # prende/apaga el seguimiento de volúmenes nuevos
            if not admin:
                return self.send_json(403, {"error": "Solo el admin puede hacer eso"})
            set_request(cfg, req_id, releasing=0 if req.get("releasing") else 1)
            return self.send_json(200, {"ok": True})

        if action == "delete":
            if not (admin or (owner and req["status"] == "pending")):
                return self.send_json(403, {"error": "No podés borrar este pedido"})
            with db(cfg) as con:
                con.execute("DELETE FROM requests WHERE id=?", (req_id,))
            return self.send_json(200, {"ok": True})

        if action == "search":
            if not chooser:
                return self.send_json(403, {"error": "No tenés permiso para buscar releases"})
            results, have = search_request(cfg, req)
            return self.send_json(200, {"have": have, "results": [public_result(r) for r in results]})

        if action == "grab":
            if not chooser:
                return self.send_json(403, {"error": "No tenés permiso para elegir releases"})
            if not admin and req["status"] == "pending" and not user["auto_approve"]:
                return self.send_json(403, {"error": "El pedido todavía no está aprobado"})
            guid = self.body().get("guid")
            with RESULTS_LOCK:
                rel = next((r for r in RESULTS.get(req_id, []) if r["guid"] == guid), None)
            if not rel:
                return self.send_json(410, {"error": "Ese resultado ya no está: volvé a buscar"})
            grab(cfg, req, rel, user["email"])
            return self.send_json(200, {"ok": True})

        return self.send_json(404, {"error": "Acción desconocida"})


def make_server(cfg):
    init_db(cfg)
    handler = type("Handler", (App,), {"cfg": cfg})
    return http.server.ThreadingHTTPServer((cfg["host"], cfg["port"]), handler)


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "config.json")
    cfg = load_config(path)
    srv = make_server(cfg)
    print(f"Taberna Requests escuchando en http://{cfg['host']}:{cfg['port']}{cfg['base_path'].rstrip('/')}/")
    if cfg["recheck_hours"] > 0:
        threading.Thread(target=recheck_loop, args=(cfg,), daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
