import json
import threading
import unittest
import urllib.error
import urllib.request

import tempfile
from pathlib import Path

from h50console.fleet import Fleet, RobotConfig
from h50console.hub import Hub
from h50console.web import ApiError, make_server

from .fake import FIXTURES, FakeClient, load_dump
from .test_stage23 import FakeClock

HAS_FIXTURES = (FIXTURES / "dump-full.json").exists()


@unittest.skipUnless(HAS_FIXTURES, "fixtures reais ausentes (tests/fixtures)")
class WebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        vals = load_dump()
        vals.update({"2/2": 2, "2/3": 0, "2/66": "", "11/2": 385877504, "11/1": True})
        cls.fake = FakeClient(vals)
        cls.tmp = tempfile.TemporaryDirectory()
        cls.fleet = Fleet(Path(cls.tmp.name), None, client_factory=lambda cfg: cls.fake, start_threads=False)
        cls.robot = cls.fleet.add(RobotConfig(id="", name="Rex", did="1000000001"), start=False)
        cls.robot.poller.clock = FakeClock()
        cls.robot.poller.tick()
        cls.hub = Hub(cls.fleet, None, raw_enabled=False)
        cls.store, cls.gw, cls.bus, cls.app = cls.robot.store, cls.robot.gw, cls.robot.bus, cls.robot.app
        cls.srv = make_server(cls.hub, "127.0.0.1", 0)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.fleet.stop_all()
        cls.tmp.cleanup()

    def call(self, method, path, body=None):
        req = urllib.request.Request(self.base + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as ex:
            with ex:
                return ex.code, json.loads(ex.read())

    def test_history_path(self):
        st, d = self.call("GET", "/api/history/path?stime=4242")
        self.assertEqual(st, 404)
        st, d = self.call("GET", "/api/history/path?stime=abc")
        self.assertEqual(st, 400)
        self.store.save_path(4242, [[0, 0, 1], [100, 50, 1], [200, 90, 0]], final=True)
        self.store.upsert_cleaning(4242, 600, 3.0, 3, "derived")
        st, d = self.call("GET", "/api/history/path?stime=4242")
        self.assertEqual((st, len(d["points"]), d["final"]), (200, 3, True))
        st, h = self.call("GET", "/api/history")
        self.assertTrue(next(r for r in h["records"] if r["stime"] == 4242)["has_path"])
        import time as _t
        self.store.save_path(int(_t.time()) - 600, [[0, 0, 1], [400, 100, 1]], final=True)
        st, cov = self.call("GET", "/api/dash/coverage?days=7")
        self.assertEqual((st, cov["cleanings"]), (200, 1))
        self.assertGreater(len(cov["cells"]), 0)
        self.store._exec("DELETE FROM cleaning_record WHERE stime=4242")      # o store é compartilhado entre os testes
        self.store._exec("DELETE FROM cleaning_path")

    def raw_request(self, headers, body=b"", method="POST", path="/api/commands"):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=5)
        conn.putrequest(method, path, skip_host="Host" in headers)
        for k, v in headers.items():
            conn.putheader(k, v)
        conn.endheaders(body)
        res = conn.getresponse()
        out = (res.status, dict(res.getheaders()), res.read())
        conn.close()
        return out

    def test_hardening(self):
        st, hdr, _ = self.raw_request({"Host": "evil.example"}, method="GET", path="/api/health")
        self.assertEqual(st, 421)                                   # DNS rebinding
        st, hdr, _ = self.raw_request({"Host": "localhost:8050"}, method="GET", path="/api/health")
        self.assertEqual(st, 200)
        self.assertEqual(hdr["Cross-Origin-Resource-Policy"], "same-origin")
        self.assertIn("camera=()", hdr["Permissions-Policy"])
        st, _, _ = self.raw_request({"Content-Length": "-5"})
        self.assertEqual(st, 400)                                   # Content-Length inválido
        st, _, _ = self.raw_request({"Content-Length": "abc"})
        self.assertEqual(st, 400)
        st, _, body = self.raw_request({"Content-Length": str(5 * 1024 * 1024), "X-H50": "1"})
        self.assertEqual(st, 400)                                   # corpo acima do limite: recusa sem ler

    def test_event_streams_are_capped(self):
        import http.client
        from h50console import web
        old, web.MAX_STREAMS = web.MAX_STREAMS, 2
        conns = []
        try:
            codes = []
            for _ in range(3):
                c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=5)
                c.request("GET", "/api/stream")
                r = c.getresponse()
                codes.append(r.status)
                conns.append(c)
            self.assertEqual(codes, [200, 200, 429])
        finally:
            web.MAX_STREAMS = old
            for c in conns:
                c.close()

    def test_metrics_prometheus_text(self):
        st, hdr, body = self.raw_request({"Host": "127.0.0.1"}, method="GET", path="/metrics")
        self.assertEqual(st, 200)
        self.assertTrue(hdr["Content-Type"].startswith("text/plain"))
        text = body.decode()
        self.assertIn('h50_battery_percent{robot="rex"}', text)
        self.assertIn("# TYPE h50_cleanings_total counter", text)
        self.assertIn('h50_consumable_percent{robot="rex",item="mop"}', text)
        self.assertNotIn("1000000001", text)                      # nada de DID
        for line in text.splitlines():
            if line and not line.startswith("#"):
                float(line.rsplit(" ", 1)[1])                      # todo valor é número

    def test_map_edit_draft_and_backup(self):
        from h50console import mapdecode
        from . import synth
        st, d = self.call("POST", "/api/map/edit/preview", {"ops": []})
        self.assertEqual(st, 404)                                              # ainda sem mapa baixado
        self.robot.map_dir.mkdir(parents=True, exist_ok=True)
        (self.robot.map_dir / "latest.bin").write_bytes(mapdecode.encrypt(synth.synthetic_map(), synth.MODEL, synth.DID))
        try:
            ops = [{"op": "add_furniture", "type": 1400, "cx": 0, "cy": -3000}]
            st, d = self.call("POST", "/api/map/edit/preview", {"ops": ops})
            self.assertEqual((st, len(d["map"]["furniture"]), d["saved_to_robot"]), (200, 3, False))
            self.assertTrue(d["changes"][0].startswith("+ móvel"))
            st, d = self.call("POST", "/api/map/edit/preview", {"ops": [{"op": "remove_wall", "id": 99}]})
            self.assertEqual(st, 400)                                          # operação inválida: mensagem clara
            self.assertIn("operação 1", d["error"])
            st, d = self.call("POST", "/api/map/edit/save", {"ops": ops})
            self.assertEqual(st, 501)                                          # gravação no robô ainda não existe
            st, d = self.call("POST", "/api/map/backup")
            self.assertEqual(st, 200)
            self.assertEqual(len(d["backups"]), 1)
            self.assertEqual(self.call("GET", "/api/map/backups")[1][0]["name"], d["name"])
        finally:
            (self.robot.map_dir / "latest.bin").unlink(missing_ok=True)
            for f in (self.robot.map_dir / "backups").glob("*"):
                f.unlink()

    def test_state(self):
        st, d = self.call("GET", "/api/state")
        self.assertEqual(st, 200)
        self.assertEqual(d["status_label"], "Carregando")
        rows = {r["key"]: r for r in d["rows"]}
        self.assertEqual(rows["2/6"]["value"], "0,87 m²")   # 2/6 = 87 no fixture

    def test_settings_and_put(self):
        _, items = self.call("GET", "/api/settings")
        by = {i["key"]: i for i in items}
        self.assertEqual(by["11/2"]["display"], "23:00–06:00")
        self.assertNotIn("2/29", by)                         # variante: sem troca automática de água
        self.assertEqual([v for v, _ in by["2/61"]["options"]][:3], [0, 2, 1])
        st, res = self.call("PUT", "/api/settings/11-2", {"value": "22:30-07:00"})
        self.assertEqual((st, res["code"]), (200, 0))
        self.assertEqual(self.fake.values["11/2"], 22 << 24 | 30 << 16 | 7 << 8)
        self.assertEqual(self.call("PUT", "/api/settings/2-31", {"value": 9})[0], 400)
        self.assertEqual(self.call("PUT", "/api/settings/2-2", {"value": 1})[0], 403)

    def test_rooms_custom_and_clean(self):
        _, rooms = self.call("GET", "/api/rooms")
        self.assertEqual(len(rooms), 6)
        _, custom = self.call("GET", "/api/custom")
        sala = next(c for c in custom if c["name"] == "Sala intenso")
        self.assertEqual(sala["rooms"], ["Sala de estar", "Corredor"])
        st, res = self.call("POST", f"/api/clean/custom/{sala['id']}")
        self.assertEqual((st, res["fallback"]), (200, "clean_rooms"))
        self.assertEqual(self.fake.calls[-1][1]["in"], [{"piid": 15, "value": "3,8"}])
        self.assertEqual(self.call("POST", "/api/clean/rooms", {"ids": []})[0], 400)

    def _custom(self, cid):
        self.fake.calls.clear()
        st, res = self.call("POST", f"/api/clean/custom/{cid}")
        return st, res, [(m, p) for m, p in self.fake.calls if m in ("set_properties", "action")]

    def test_custom_applies_routine_mode(self):
        old = self.gw.cache.get("2/42")
        labels = [{"id": 11, "name": "Só aspirar", "mop": [1], "room_ids": [3, 8], "user_cfg": []},
                  {"id": 12, "name": "Misturada", "mop": [1, 3], "room_ids": [3, 8], "user_cfg": []},
                  {"id": 13, "name": "Sem modo", "mop": [], "room_ids": [3], "user_cfg": []}]
        self.gw.cache.update({"2/42": json.dumps({"user_labels": labels}), "2/4": 3})
        self.fake.values["2/4"] = 3
        try:
            # modo diferente: grava 2/4 ANTES de mandar limpar os cômodos
            st, res, sent = self._custom(11)
            self.assertEqual((st, res["mode"]), (200, 1))
            self.assertEqual([m for m, _ in sent], ["set_properties", "action"])
            self.assertEqual((sent[0][1][0]["siid"], sent[0][1][0]["piid"], sent[0][1][0]["value"]), (2, 4, 1))
            self.assertEqual(sent[1][1]["in"], [{"piid": 15, "value": "3,8"}])
            # já no modo certo: não grava de novo
            st, _, sent = self._custom(11)
            self.assertEqual((st, [m for m, _ in sent]), (200, ["action"]))
            # modos diferentes por cômodo: recusa sem mexer em nada
            st, res, sent = self._custom(12)
            self.assertEqual((st, sent), (409, []))
            self.assertIn("app Xiaomi Home", res["error"])
            # sem modo na rotina: comportamento antigo
            st, res, sent = self._custom(13)
            self.assertEqual((st, res["mode"], [m for m, _ in sent]), (200, None, ["action"]))
            # robô ocupado: não muda o modo
            self.gw.cache.update({"2/4": 3, "2/2": 12})
            self.fake.values["2/4"] = 3
            st, _, sent = self._custom(11)
            self.assertEqual((st, sent), (409, []))
            self.assertEqual(self.gw.cache.get("2/4"), 3)
        finally:
            self.gw.cache.update({"2/42": old, "2/2": 2})

    def test_zones(self):
        from h50console import zones
        sofa = [1000, -500, 1000, 700, 2500, 700, 2500, -500]
        ud = {"user_define": {"id": 1791744466, "name": "Pelos gatos", "user_data": [["mode", "mode_data"], [2, [sofa]]]}}
        self.store.set_setting(zones.SAVED_KEY, [])
        try:
            # rotina por zonas ainda não vista: ▶ desabilitado com o motivo, e o painel recusa
            self.store._exec("DELETE FROM app_setting WHERE key=?", (zones.LEARNED_KEY,))
            _, custom = self.call("GET", "/api/custom")
            gatos = next(c for c in custom if c["name"] == "Pelos gatos")
            self.assertFalse(gatos["runnable"])
            self.assertIn("aprender as zonas", gatos["reason"])
            self.assertEqual(self.call("POST", f"/api/clean/custom/{gatos['id']}")[0], 409)
            # o robô roda a rotina pelo app: o poller aprende as zonas pelo 2/40
            self.assertTrue(zones.learn(self.store, json.dumps(ud)))
            self.assertFalse(zones.learn(self.store, json.dumps(ud)))            # sem mudança
            _, custom = self.call("GET", "/api/custom")
            gatos = next(c for c in custom if c["name"] == "Pelos gatos")
            self.assertEqual((gatos["runnable"], gatos["zones"]), (True, [sofa]))
            self.gw.cache.update({"2/4": 3}); self.fake.values["2/4"] = 3
            self.fake.calls.clear()
            st, res = self.call("POST", f"/api/clean/custom/{gatos['id']}")
            self.assertEqual((st, res["fallback"], res["mode"]), (200, "clean_zones", 1))
            sent = [(m, p) for m, p in self.fake.calls if m in ("set_properties", "action")]
            self.assertEqual([m for m, _ in sent], ["set_properties", "action"])      # modo antes da zona
            self.assertEqual((sent[1][1]["siid"], sent[1][1]["aiid"]), (2, 37))
            self.assertEqual(json.loads(sent[1][1]["in"][0]["value"]),
                             [{"blocks_region": [1000, 700, 1000, -500, 2500, -500, 2500, 700], "blocks_attr": 0}])
            # zonas com nome: nome único e diferente das rotinas
            st, z = self.call("POST", "/api/zones", {"name": "  Frente  do sofá ", "zones": [[1000, -500, 2500, 700]]})
            self.assertEqual((st, z["name"]), (200, "Frente do sofá"))
            self.assertEqual(self.call("POST", "/api/zones", {"name": "frente do SOFÁ", "zones": [[0, 0, 900, 900]]})[0], 409)
            st, res = self.call("POST", "/api/zones", {"name": "pelos gatos", "zones": [[0, 0, 900, 900]]})
            self.assertEqual(st, 409)
            self.assertIn("rotina", res["error"])
            self.assertEqual(self.call("POST", "/api/zones", {"name": "", "zones": [[0, 0, 900, 900]]})[0], 400)
            self.assertEqual(self.call("POST", "/api/zones", {"name": "x", "zones": [[0, 0, 100, 100]]})[0], 400)
            self.assertEqual(self.call("PUT", f"/api/zones/{z['id']}", {"name": "Sala intenso"})[0], 409)
            self.assertEqual(self.call("PUT", f"/api/zones/{z['id']}", {"name": "Sofá"})[1]["name"], "Sofá")
            self.assertEqual([x["name"] for x in self.call("GET", "/api/zones")[1]], ["Sofá"])
            # limite de zonas desenhadas (aba Configurações): vale para desenhar e salvar, não para o que já está salvo
            three = [[0, 0, 900, 900], [1000, 0, 1900, 900], [2000, 0, 2900, 900], [3000, 0, 3900, 900]]
            self.assertEqual(self.call("GET", "/api/zones/limit")[1], {"limit": 3})          # padrão
            st, res = self.call("POST", "/api/clean/zones", {"zones": three})
            self.assertEqual(st, 400)
            self.assertIn("no máximo 3", res["error"])
            self.assertEqual(self.call("PUT", "/api/zones/limit", {"limit": 2})[1], {"limit": 2})
            self.assertEqual(self.call("POST", "/api/zones", {"name": "Três", "zones": three[:3]})[0], 400)
            self.assertEqual(self.call("POST", "/api/clean/zones", {"zones": three[:2]})[0], 200)
            for bad in (0, 11, "3", True, None):
                self.assertEqual(self.call("PUT", "/api/zones/limit", {"limit": bad})[0], 400, bad)
            self.call("PUT", "/api/zones/limit", {"limit": 3})
            row = next(r for r in self.call("GET", "/api/settings")[1] if r["key"] == "zone_limit")   # tabela de ajustes
            self.assertEqual((row["group"], row["value"], row["range"], row["local"]), ("Sistema", 3, [1, 10, 1], True))
            # limpar pela zona salva ou por coordenadas
            self.fake.calls.clear()
            self.assertEqual(self.call("POST", "/api/clean/zones", {"zone_id": z["id"]})[1]["code"], 0)
            self.assertEqual(self.fake.calls[-1][1]["aiid"], 37)
            self.assertEqual(self.call("POST", "/api/clean/zones", {"zone_id": "ffffffff"})[0], 404)
            for bad in ({}, {"zones": []}, {"zones": [[1, 2, 3]]}, {"zones": [[0, 0, 100, 100]]}):
                self.assertEqual(self.call("POST", "/api/clean/zones", bad)[0], 400, bad)
            self.gw.cache.update({"2/2": 12})                                  # base trabalhando: não sai
            self.assertEqual(self.call("POST", "/api/clean/zones", {"zone_id": z["id"]})[0], 409)
            self.gw.cache.update({"2/2": 2})
            self.assertEqual(self.call("DELETE", f"/api/zones/{z['id']}")[0], 200)
            self.assertEqual(self.call("DELETE", f"/api/zones/{z['id']}")[0], 404)
            # importação do histórico (instalação que já tinha capturado a rotina antes desta versão)
            self.store._exec("DELETE FROM app_setting WHERE key=?", (zones.LEARNED_KEY,))
            self.store.save_changes({"2/40": ("", json.dumps(ud))})
            self.assertEqual(zones.learned(self.store)["1791744466"]["zones"], [sofa])
        finally:
            self.store.set_setting(zones.SAVED_KEY, [])
            self.gw.cache.update({"2/2": 2})

    def test_actions_blocked(self):
        self.gw.cache.update({"2/2": 12})
        try:
            st, res = self.call("POST", "/api/actions/start")
            self.assertEqual(st, 409)
            self.assertIn("base", res["error"])
            self.assertEqual(self.call("POST", "/api/actions/home")[0], 200)
            self.assertEqual(self.call("POST", "/api/actions/explode")[0], 404)
        finally:
            self.gw.cache.update({"2/2": 2})

    def test_misc_routes(self):
        for path in ("/api/health", "/api/consumables", "/api/history", "/api/schedules",
                     "/api/commands", "/api/faults", "/api/app"):
            self.assertEqual(self.call("GET", path)[0], 200, path)
        _, h = self.call("GET", "/api/history")
        self.assertEqual(h["totals"]["count"], 1)
        self.assertEqual(self.call("POST", "/api/raw/get", {"keys": ["2/2"]})[0], 403)
        self.assertEqual(self.call("GET", "/api/nada")[0], 404)
        self.assertEqual(self.call("PUT", "/api/app/name", {"value": "Rex"})[1]["name"], "Rex")

    def test_static_and_traversal(self):
        with urllib.request.urlopen(self.base + "/", timeout=5) as r:
            self.assertIn(b"H50 Console", r.read())
        with urllib.request.urlopen(self.base + "/app.js", timeout=5) as r:
            self.assertIn("javascript", r.headers["Content-Type"])
        self.assertEqual(self.call("GET", "/../config.py")[0], 404)

    def test_sse(self):
        with urllib.request.urlopen(self.base + "/api/r/rex/stream", timeout=5) as r:
            self.assertIn("text/event-stream", r.headers["Content-Type"])
            self.assertEqual(r.readline(), b"retry: 3000\n")
            r.readline()
            self.bus.publish("state", {"changes": {"2/2": 4}})
            self.assertEqual(r.readline(), b"event: state\n")
            self.assertIn(b'"2/2": 4', r.readline())

    def test_robot_prefix_and_default_route_agree(self):
        _, a = self.call("GET", "/api/state")
        _, b = self.call("GET", "/api/r/rex/state")
        self.assertEqual(a["values"]["2/2"], b["values"]["2/2"])
        self.assertEqual(self.call("GET", "/api/r/naoexiste/state")[0], 404)
        _, robots = self.call("GET", "/api/robots")
        self.assertEqual([(r["id"], r["name"]) for r in robots], [("rex", "Rex")])
        self.assertNotIn("token", robots[0])                     # segredos nunca saem pela API

    def test_dispatch_unit(self):
        with self.assertRaises(ApiError):
            self.hub.dispatch("DELETE", "/api/state", {}, None)


if __name__ == "__main__":
    unittest.main()
