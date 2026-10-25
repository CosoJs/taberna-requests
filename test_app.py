"""Prueba de punta a punta con Komga, AniList, Prowlarr y qBittorrent falsos.

Uso: python test_app.py
"""
import base64
import http.server
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request

import app

ADDED = []  # lo que recibió el qBittorrent falso

ANILIST = {"data": {"Page": {"media": [{
    "id": 101, "idMal": 55, "format": "NOVEL", "status": "FINISHED", "volumes": 13, "chapters": None,
    "siteUrl": "https://anilist.co/manga/101", "synonyms": ["Eromanga-sensei"],
    "title": {"romaji": "Eromanga-sensei", "english": "Eromanga Sensei", "native": "エロマンガ先生"},
    "coverImage": {"large": "https://s4.anilist.co/x.jpg"}, "startDate": {"year": 2013},
    "description": "Una <br>novela."}]}}}

PROWLARR = [
    {"guid": "g1", "title": "Eromanga Sensei v01-13 [EPUB]", "size": 300_000_000, "seeders": 12, "leechers": 1,
     "indexer": "IndexerA", "indexerId": 1, "downloadUrl": "{base}/dl/torrent?apikey=SECRETO", "protocol": "torrent"},
    {"guid": "g2", "title": "Eromanga Sensei v01-13 [PDF]", "size": 500_000_000, "seeders": 30, "leechers": 0,
     "indexer": "IndexerB", "indexerId": 2, "downloadUrl": "{base}/dl/magnet?apikey=SECRETO", "protocol": "torrent"},
    {"guid": "g3", "title": "Nozoki Ana", "size": 1, "seeders": 99, "leechers": 0,
     "indexer": "IndexerA", "indexerId": 1, "downloadUrl": "{base}/dl/torrent", "protocol": "torrent"},
    # Komga ya tiene 8: este es el único que trae solo lo que falta
    {"guid": "g4", "title": "Eromanga Sensei v09-13 [EPUB]", "size": 100_000_000, "seeders": 2, "leechers": 0,
     "indexer": "IndexerA", "indexerId": 1, "downloadUrl": "{base}/dl/torrent", "protocol": "torrent"},
]


class Fake(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, status, body=b"", headers=None):
        self.send_response(status)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        base = f"http://127.0.0.1:{self.server.server_port}"
        path = urllib.parse.urlsplit(self.path).path
        if path == "/komga/api/v2/users/me":
            user = base64.b64decode(self.headers.get("Authorization", "Basic Og==")[6:]).decode()
            if user == "admin@x:pw":
                return self.reply(200, json.dumps({"email": "admin@x", "roles": ["ADMIN"]}).encode())
            if user == "amigo@x:pw":
                return self.reply(200, json.dumps({"email": "amigo@x", "roles": []}).encode())
            return self.reply(401)
        if path == "/komga/api/v1/libraries":
            return self.reply(200, json.dumps([{"id": "L1", "name": "Novels"}, {"id": "L2", "name": "Manga"}]).encode())
        if path == "/komga/api/v1/series":
            # 8 de 13 en Novels; el manga (otra biblioteca) no tiene que contar para la novela
            return self.reply(200, json.dumps({"content": [
                {"name": "Eromanga Sensei [LN]", "booksCount": 8, "libraryId": "L1"},
                {"name": "Eromanga Sensei", "booksCount": 12, "libraryId": "L2"}]}).encode())
        if path == "/prowlarr/api/v1/search":
            assert self.headers.get("X-Api-Key") == "KEY"
            data = json.dumps(PROWLARR).replace("{base}", base).encode()
            return self.reply(200, data)
        if path == "/jikan/manga/55":
            return self.reply(200, json.dumps({"data": {"titles": [{"type": "Synonym", "title": "Eroman-sensei"}],
                                                        "title_synonyms": ["Ero Manga Teacher"]}}).encode())
        if path == "/dl/torrent":
            return self.reply(200, b"d4:infod4:name1:xee")
        if path == "/dl/magnet":
            return self.reply(302, headers={"Location": "magnet:?xt=urn:btih:abc"})
        self.reply(404)

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if path == "/anilist":
            return self.reply(200, json.dumps(ANILIST).encode())
        if path == "/qbit/api/v2/auth/login":
            ok = body == b"username=u&password=p"
            return self.reply(200, b"Ok." if ok else b"Fails.", {"Set-Cookie": "SID=s1; HttpOnly"} if ok else {})
        if path == "/qbit/api/v2/torrents/add":
            assert "SID=s1" in self.headers.get("Cookie", "")
            ADDED.append(body)
            return self.reply(200, b"Ok.")
        self.reply(404)


class Client:
    def __init__(self, base):
        self.base, self.cookie = base, ""

    def call(self, path, body=None):
        req = urllib.request.Request(self.base + "/api/" + path, method="POST" if body is not None else "GET",
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json", "Cookie": self.cookie})
        try:
            with urllib.request.urlopen(req) as r:
                status, hdrs, data = r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            status, hdrs, data = e.code, e.headers, e.read()
        if hdrs.get("Set-Cookie"):
            self.cookie = hdrs["Set-Cookie"].split(";")[0]
        return status, json.loads(data or b"null")


class EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fake = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fake)
        threading.Thread(target=fake.serve_forever, daemon=True).start()
        fb = f"http://127.0.0.1:{fake.server_port}"
        cls.tmp = tempfile.mkdtemp()
        cfg_path = os.path.join(cls.tmp, "config.json")
        with open(cfg_path, "w") as f:
            json.dump({"host": "127.0.0.1", "port": 0, "db": os.path.join(cls.tmp, "t.db"),
                       "anilist_url": fb + "/anilist", "jikan_url": fb + "/jikan",
                       "komga": {"url": fb + "/komga", "username": "svc", "password": "x",
                                 "libraries": {"novel": ["Novels"], "manga": ["Manga"]}},
                       "prowlarr": {"url": fb + "/prowlarr", "api_key": "KEY"},
                       "qbittorrent": {"url": fb + "/qbit", "username": "u", "password": "p"}}, f)
        cls.cfg = app.load_config(cfg_path)
        srv = app.make_server(cls.cfg)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{srv.server_port}"

    def wait_status(self, client, req_id, status):
        for _ in range(50):
            rows = {r["id"]: r for r in client.call("requests")[1]}
            if rows[req_id]["status"] == status:
                return rows[req_id]
            time.sleep(0.1)
        self.fail(f"el pedido {req_id} nunca llegó a {status}: {rows[req_id]}")

    def test_flow(self):
        admin, amigo = Client(self.base), Client(self.base)
        self.assertEqual(Client(self.base).call("login", {"username": "admin@x", "password": "mal"})[0], 401)
        self.assertEqual(admin.call("login", {"username": "admin@x", "password": "pw"})[0], 200)
        self.assertEqual(amigo.call("login", {"username": "amigo@x", "password": "pw"})[0], 200)
        self.assertTrue(admin.call("me")[1]["is_admin"])
        self.assertFalse(amigo.call("me")[1]["is_admin"])

        # El amigo busca en AniList y pide: queda pendiente.
        status, items = amigo.call("anilist?kind=novel&q=eromanga")
        self.assertEqual((status, items[0]["title"]), (200, "Eromanga Sensei"))
        self.assertEqual(items[0]["available"], {"books": 8, "volumes": 13, "complete": False})  # parcial 8/13
        status, r = amigo.call("requests", items[0])
        self.assertEqual((status, r["status"]), (201, "pending"))
        self.assertEqual(amigo.call("requests", items[0])[0], 409)  # duplicado
        for _ in range(50):  # los títulos de MAL llegan en segundo plano
            if "Ero Manga Teacher" in amigo.call("requests")[1][0]["titles"]:
                break
            time.sleep(0.1)
        else:
            self.fail("no llegaron los títulos de MAL")
        self.assertEqual(amigo.call(f"requests/{r['id']}/search", {})[0], 403)  # sin permiso

        # El admin busca: resultados ordenados, sin URLs con claves, y elige uno.
        status, data = admin.call(f"requests/{r['id']}/search", {})
        self.assertEqual((status, data["have"]), (200, 8))  # 8 en Komga
        results = data["results"]
        # primero lo que trae solo lo que falta, después los packs (EPUB antes que PDF), Nozoki Ana último
        self.assertEqual([x["guid"] for x in results], ["g4", "g1", "g2", "g3"])
        self.assertEqual((results[0]["vol_from"], results[0]["vol_to"], results[0]["only_new"]), (9, 13, True))
        self.assertTrue(results[1]["new"] and not results[1]["only_new"])
        self.assertFalse(results[3]["match"])
        self.assertNotIn("SECRETO", json.dumps(results))
        self.assertEqual(admin.call(f"requests/{r['id']}/grab", {"guid": "g2"})[0], 200)
        self.assertIn(b"magnet:?xt=urn:btih:abc", ADDED[-1])
        self.assertIn(b'name="category"\r\n\r\nnovels', ADDED[-1])
        row = admin.call("requests")[1][0]
        self.assertEqual(row["status"], "grabbed")
        self.assertEqual(row["available"], {"books": 8, "volumes": 13, "complete": False})  # se muestra "Parcial 8/13"

        # Con auto-aprobar, el pedido se busca y baja solo: como ya hay 8 en Komga,
        # elige el que trae solo los volúmenes 9-13 y no un pack que los repite.
        self.assertEqual(admin.call("users/amigo%40x", {"auto_approve": True})[0], 200)
        admin.call(f"requests/{r['id']}/delete", {})
        n = len(ADDED)
        status, r2 = amigo.call("requests", items[0])
        self.assertEqual(r2["status"], "approved")
        row = self.wait_status(admin, r2["id"], "grabbed")
        self.assertIn("v09-13", row["release_title"])
        self.assertIn(b"d4:infod4:name1:xee", ADDED[n])

        # Seguimiento de una obra en emisión: una ronda de revisión no repite lo ya bajado.
        self.assertEqual(admin.call(f"requests/{r2['id']}/follow", {})[0], 200)
        self.cfg["recheck_pause"] = 0
        n = len(ADDED)
        app.recheck(self.cfg)
        self.assertEqual(len(ADDED), n)  # v09-13 ya se bajó y los packs repiten: no baja nada
        note = {x["id"]: x for x in admin.call("requests")[1]}[r2["id"]]["note"]
        self.assertIn("packs", note)

    def test_rank(self):
        res = app.rank([{"title": "Kono Sensei", "seeders": 5}, {"title": "Eromanga Sensei v1 [EPUB]", "seeders": 1}],
                       ["Eromanga Sensei"], ["epub"])
        self.assertEqual(res[0]["title"], "Eromanga Sensei v1 [EPUB]")
        self.assertIsNone(app.pick_best([{"title": "x", "match": False, "seeders": 9}]))

    def test_languages(self):
        d = app.detect_languages
        self.assertEqual(d("Monogatari (2006) [Vertical] [EPUB]"), ["English"])
        self.assertEqual(d("Mushoku Tensei Novela Ligera [ESP]"), ["Español"])
        self.assertEqual(d("Kimetsu no Yaiba Tomo 01-23 [Latino]"), ["Latino", "Español"])
        self.assertEqual(d("[Raws] Oshi no Ko v01-16 [JPN]"), ["Japonés"])
        self.assertEqual(d("Chi's Sweet Home v01-12"), [])
        self.assertEqual(d("Kaguya-sama - Love Is War v01-28 (Digital) (1r0n)"), ["English"])
        self.assertEqual(d("無職転生 第01-19巻"), ["Japonés"])
        self.assertEqual(d("Overlord ING v01-14"), ["English"])
        self.assertEqual(d("Overlord ES v01-14 EN"), ["Español", "English"])
        self.assertEqual(d("Lo que es la vida"), [])  # "es" en minúscula no cuenta

    def test_language_preference(self):
        langs = ["Latino", "Español", "English"]
        res = app.rank([
            {"title": "Overlord v01 [JPN]", "seeders": 99},
            {"title": "Overlord v01 [English]", "seeders": 10},
            {"title": "Overlord v01 [Español]", "seeders": 3},
        ], ["Overlord"], [], [], langs)
        self.assertEqual([r["seeders"] for r in res], [3, 10, 99])  # español, inglés, raw al final
        self.assertEqual(app.pick_best(res[2:]), None)  # un raw japonés nunca se baja solo

    def test_avoid_and_seeders(self):
        res = app.rank([
            {"title": "Mushoku Tensei (Web Novel) Vol 01-24 EPUB", "seeders": 14},
            {"title": "Mushoku Tensei [Seven Seas]", "seeders": 166},
            {"title": "Mushoku Tensei v03 EPUB", "seeders": 0},
        ], ["Mushoku Tensei"], ["epub"], ["web novel"])
        self.assertEqual(res[0]["seeders"], 166)  # ni la web novel ni el de 0 seeders
        self.assertEqual(app.pick_best(res)["seeders"], 166)

    def test_volumes(self):
        v = app.parse_volumes
        self.assertEqual(v("Senko-san - c001-061x3 (v01-08) [STALKER Scanlation]"), (1, 8))
        self.assertEqual(v("Kaguya-sama Love is War v10 (2019) (digital)"), (10, 10))
        self.assertEqual(v("Kaguya-sama Love Is War [Volumes 01-18] (scanlation)"), (1, 18))
        self.assertEqual(v("Kimetsu no Yaiba Tomo 1 a 23 [Latino]"), (1, 23))
        self.assertEqual(v("無職転生 第01-19巻"), (1, 19))
        self.assertEqual(v("[Light Novel] Bakemonogatari - vol.02"), (2, 2))
        self.assertIsNone(v("Zoku Owarimonogatari - 01v2 (BD 1080p)"))  # v2 es versión, no volumen

    def test_complete_first(self):
        # Sin nada en Komga: el pack completo gana al volumen suelto con más seeders
        res = app.rank([
            {"title": "Kaguya-sama v01 [English]", "seeders": 50},
            {"title": "Kaguya-sama v01-10 [English]", "seeders": 20},
            {"title": "Kaguya-sama v01-28 (COMPLETE) [English]", "seeders": 5},
        ], ["Kaguya-sama"], [], [], ["English"], total=28)
        self.assertEqual([r["seeders"] for r in res], [5, 20, 50])
        self.assertTrue(res[0]["complete"])
        # pero el idioma pesa más: en español incompleto antes que en inglés completo
        res = app.rank([
            {"title": "Kaguya-sama v01-28 COMPLETE [English]", "seeders": 5},
            {"title": "Kaguya-sama Tomo 1 a 10 [Español]", "seeders": 5},
        ], ["Kaguya-sama"], [], [], ["Español", "English"], total=28)
        self.assertIn("Español", res[0]["title"])

    def test_wrong_category(self):
        # Pedido de novela: un pack que el tracker clasifica como cómic/manga (7030) no se baja solo,
        # aunque el nombre no diga "manga".
        res = app.rank([
            {"title": "Bakemonogatari v01-22", "seeders": 50, "categories": [7030]},
            {"title": "Bakemonogatari v01-03", "seeders": 5, "categories": [7020]},
        ], ["Bakemonogatari"], ["epub"], [], [], [7030])
        self.assertEqual(app.pick_best(res)["seeders"], 5)
        self.assertTrue(res[-1]["wrong_kind"])


if __name__ == "__main__":
    unittest.main()
