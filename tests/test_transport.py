import unittest

from h50console import catalog
from h50console.device import H50
from h50console.miio import MiioError
from h50console.transport import AutoClient, CloudClient, build_client

from .fake import FIXTURES, FakeClient, load_dump


class FakeCloudService:
    """Imita CloudService.api: responde como a nuvem Xiaomi (formato /miotspec/*)."""

    def __init__(self, values: dict, fail: bool = False):
        self.values = dict(values)
        self.fail = fail
        self.calls: list[tuple[str, dict]] = []

    def api(self, path, data, did=None):
        from h50console.cloud import CloudError
        self.calls.append((path, data))
        if self.fail:
            raise CloudError("sessão expirada: rode login de novo")
        if path == "/miotspec/prop/get":
            res = []
            for p in data["params"]:
                k = f"{p['siid']}/{p['piid']}"
                item = {"did": p["did"], "siid": p["siid"], "piid": p["piid"], "updateTime": 1}
                item.update({"code": 0, "value": self.values[k]} if k in self.values else {"code": -4004})
                res.append(item)
            return {"code": 0, "result": res}
        if path == "/miotspec/prop/set":
            for p in data["params"]:
                self.values[f"{p['siid']}/{p['piid']}"] = p["value"]
            return {"code": 0, "result": [{**{k: p[k] for k in ("did", "siid", "piid")}, "code": 0} for p in data["params"]]}
        if path == "/miotspec/action":
            return {"code": 0, "result": {"code": 0, "out": []}}
        raise AssertionError(path)


class CloudClientTests(unittest.TestCase):
    def setUp(self):
        self.svc = FakeCloudService({"2/2": 4, "3/1": 80, "4/2": 60})
        self.c = CloudClient(self.svc, "1000000001", "xiaomi.vacuum.ov42gl")

    def test_get_set_action_have_lan_format(self):
        r = self.c.send("get_properties", [{"did": "x", "siid": 2, "piid": 2}, {"did": "x", "siid": 9, "piid": 9}])
        self.assertEqual(r[0], {"did": "1000000001", "siid": 2, "piid": 2, "code": 0, "value": 4})
        self.assertEqual(r[1]["code"], -4004)
        self.assertEqual(self.svc.calls[0][1]["datasource"], 2)           # leitura em tempo real
        s = self.c.send("set_properties", [{"did": "x", "siid": 4, "piid": 2, "value": 30}])
        self.assertEqual((s[0]["code"], self.svc.values["4/2"]), (0, 30))
        a = self.c.send("action", {"did": "x", "siid": 6, "aiid": 1, "in": []})
        self.assertEqual(a, {"code": 0, "out": []})
        self.assertEqual(self.svc.calls[-1][1]["params"], {"did": "1000000001", "siid": 6, "aiid": 1, "in": []})

    def test_errors_become_miio_error(self):
        self.svc.fail = True
        with self.assertRaises(MiioError):
            self.c.send("get_properties", [{"did": "x", "siid": 2, "piid": 2}])
        with self.assertRaises(MiioError):
            self.c.send("miio.nada")

    def test_h50_over_cloud_reads_oversized_10_3_from_cache(self):
        vals = {k: 0 for k in catalog.READABLE}
        vals["10/3"] = "x" * 2000                       # maior que o limite do UDP
        dev = H50(CloudClient(FakeCloudService(vals), "1000000001"))
        res = dev.get(["2/2", "10/3", "3/1"])
        self.assertEqual(res["10/3"]["code"], 0)
        sources = [c[1]["datasource"] for c in dev.client.service.calls]
        self.assertEqual(sorted(sources), [1, 2])       # 10/3 pelo cache; o resto em tempo real

    def test_batches_respect_cloud_item_limit(self):
        vals = {k: 0 for k in catalog.READABLE}
        dev = H50(CloudClient(FakeCloudService(vals), "1000000001"))
        dev.get([k for k in catalog.READABLE if k != "10/3"][:100])
        for _path, data in dev.client.service.calls:
            self.assertLessEqual(len(data["params"]), 40)

    def test_realtime_timeouts_are_retried_from_cache(self):
        class Flaky(FakeCloudService):
            def api(self, path, data, did=None):
                res = super().api(path, data, did)
                if path == "/miotspec/prop/get" and data["datasource"] == 2:
                    for i in res["result"]:
                        i.pop("value", None); i["code"] = -704220043      # robô não respondeu a tempo
                return res
        c = CloudClient(Flaky({"2/2": 4, "3/1": 80}), "1000000001")
        r = c.send("get_properties", [{"did": "x", "siid": 2, "piid": 2}, {"did": "x", "siid": 3, "piid": 1}])
        self.assertEqual([(i["code"], i["value"]) for i in r], [(0, 4), (0, 80)])


class AutoClientTests(unittest.TestCase):
    def setUp(self):
        self.lan = FakeClient({"2/2": 2, "3/1": 90})
        self.lan.handshake = lambda **_: (_ for _ in ()).throw(MiioError("off")) if self.lan.offline else None
        self.svc = FakeCloudService({"2/2": 4, "3/1": 80})
        self.auto = AutoClient(self.lan, CloudClient(self.svc, "1000000001"), retry_after=0.05)
        self.req = [{"did": "x", "siid": 3, "piid": 1}]

    def test_lan_first(self):
        r = self.auto.send("get_properties", self.req)
        self.assertEqual((r[0]["value"], self.auto.mode), (90, "lan"))
        self.assertEqual(self.svc.calls, [])

    def test_falls_back_to_cloud_and_returns_to_lan(self):
        self.lan.offline = True
        r = self.auto.send("get_properties", self.req)
        self.assertEqual((r[0]["value"], self.auto.mode), (80, "cloud"))
        self.assertFalse(self.auto.lan_client is None)
        import time
        self.lan.offline = False
        time.sleep(0.1)                                  # passou a janela de espera: sonda a LAN
        r = self.auto.send("get_properties", self.req)
        self.assertEqual((r[0]["value"], self.auto.mode), (90, "lan"))

    def test_lan_still_down_stays_on_cloud(self):
        import time
        self.lan.offline = True
        self.auto.send("get_properties", self.req)
        time.sleep(0.1)
        self.auto.send("get_properties", self.req)
        self.assertEqual(self.auto.mode, "cloud")

    def test_batch_budget_follows_mode(self):
        self.assertIsNone(self.auto.max_batch_bytes)
        self.lan.offline = True
        self.auto.send("get_properties", self.req)
        self.assertEqual((self.auto.max_batch_bytes, self.auto.max_batch_items), (2800, 40))   # limites medidos
        self.assertTrue(self.auto.oversize_ok)

    def test_one_lost_read_is_retried_on_lan(self):
        orig, seen = self.lan.send, []
        self.lan.timeout = 4.0

        def lossy(method, params=None, retry=1):     # o MiioClient repete sozinho até `retry` vezes
            seen.append((retry, self.lan.timeout))
            if not retry:                            # sem nova tentativa, a resposta perdida derruba a LAN
                raise MiioError("timeout")
            return orig(method, params, retry)
        self.lan.send = lossy
        r = self.auto.send("get_properties", self.req, retry=0)
        self.assertEqual((r[0]["value"], self.auto.mode), (90, "lan"))
        self.assertEqual(seen, [(1, 2.0)])           # leitura: uma nova tentativa, 2 s cada
        self.assertEqual(self.lan.timeout, 4.0)      # o timeout volta ao normal
        self.assertEqual(self.svc.calls, [])

    def test_command_probes_lan_inside_the_cloud_window(self):
        self.auto.retry_after = 60
        self.lan.offline = True
        self.auto.send("get_properties", self.req)
        self.assertEqual(self.auto.mode, "cloud")
        self.auto.wake_lan()                         # LAN ainda fora: continua na nuvem
        self.assertEqual(self.auto.mode, "cloud")
        self.lan.offline = False
        self.auto.wake_lan()                         # voltou: o comando vai pela LAN sem esperar os 60 s
        self.auto.send("action", {"did": "2/16", "siid": 2, "aiid": 16, "in": ["6"]})
        self.assertEqual(self.auto.mode, "lan")
        self.assertEqual(self.lan.calls[-1][0], "action")

    def test_without_internet_a_read_goes_back_to_the_lan(self):
        self.auto.retry_after = 60
        self.lan.offline = True
        self.auto.send("get_properties", self.req)                 # LAN perdeu uma resposta: janela da nuvem
        self.assertEqual(self.auto.mode, "cloud")
        self.svc.api = lambda *a, **k: (_ for _ in ()).throw(OSError("[Errno -3] Try again"))   # e a internet caiu
        with self.assertRaises(MiioError):                          # sem LAN nem nuvem: MiioError (não exceção solta)
            self.auto.send("get_properties", self.req)
        self.lan.offline = False
        r = self.auto.send("get_properties", self.req)              # a LAN responde: sai da janela sem esperar 60 s
        self.assertEqual((r[0]["value"], self.auto.mode), (90, "lan"))
        with self.assertRaises(MiioError):                          # comando não é repetido pela LAN aqui
            self.auto._lan_down_until = 1e18
            self.auto.send("action", {"did": "x", "siid": 2, "aiid": 16, "in": []})

    def test_without_cloud_raises(self):
        a = AutoClient(self.lan, None)
        self.lan.offline = True
        with self.assertRaises(MiioError):
            a.send("get_properties", self.req)

    def test_build_client(self):
        cloud = CloudClient(self.svc, "1")
        self.assertIs(build_client("lan", self.lan, cloud), self.lan)
        self.assertIs(build_client("cloud", self.lan, cloud), cloud)
        self.assertIsInstance(build_client("auto", self.lan, cloud), AutoClient)
        self.assertIs(build_client("auto", self.lan, None), self.lan)       # sem nuvem, só LAN


class SessionFile(unittest.TestCase):
    def test_session_is_saved_private_and_atomically(self):
        import json
        import os
        import stat
        import tempfile
        from pathlib import Path
        from h50console.cloud import Cloud
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "cloud-session.json"
            Cloud({"user_id": "1", "ssecurity": "x"}).save(f)
            self.assertEqual(json.loads(f.read_text(encoding="utf-8"))["user_id"], "1")
            self.assertFalse((Path(d) / "cloud-session.json.tmp").exists())
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(f.stat().st_mode), 0o600)


class CloudCountry(unittest.TestCase):
    """H50_CLOUD_COUNTRY: servidor da nuvem fixo, para contas com aparelhos em servidores diferentes."""

    def _service(self, d, session_country, forced):
        import json
        from pathlib import Path
        from h50console.cloud import CloudService
        f = Path(d) / "cloud-session.json"
        f.write_text(json.dumps({"user_id": "1", "ssecurity": "eA==", "service_token": "t",
                                 "country": session_country}), encoding="utf-8")
        return CloudService(f, Path(d) / "map", country=forced)

    def _fake_api(self, lists):
        asked = []

        def api(cloud, path, data, country=None):
            asked.append(country or cloud.forced_country or cloud.s.get("country"))
            return {"result": {"list": lists.get(asked[-1], [])}}
        return asked, api

    def test_forced_country_is_the_only_server_used(self):
        import tempfile
        from unittest import mock
        from h50console.cloud import Cloud
        asked, api = self._fake_api({"us": [{"did": "1", "token": "a"}], "de": [{"did": "42", "token": "b"}]})
        with tempfile.TemporaryDirectory() as d, mock.patch.object(Cloud, "api", api):
            svc = self._service(d, "us", "de")
            self.assertEqual([x["did"] for x in svc.devices()], ["42"])
            self.assertEqual(svc.credentials("42")["token"], "b")
            self.assertEqual(set(asked), {"de"})
            self.assertEqual(svc.status()["country"], "de")
            self.assertEqual(svc._cloud().find_device("42")["did"], "42")
            with self.assertRaises(Exception):
                svc._cloud().find_device("1")        # está no "us", mas o servidor fixo é o "de"

    def test_without_forced_country_nothing_changes(self):
        import tempfile
        from unittest import mock
        from h50console.cloud import COUNTRIES, Cloud
        asked, api = self._fake_api({"de": [{"did": "42"}]})
        with tempfile.TemporaryDirectory() as d, mock.patch.object(Cloud, "api", api):
            svc = self._service(d, "sg", "")
            self.assertEqual(svc._cloud().countries(), ["sg"] + [c for c in COUNTRIES if c != "sg"])
            self.assertEqual([x["did"] for x in svc.devices()], ["42"])
            self.assertEqual(asked[:2], ["sg", "us"])
            self.assertEqual(svc.status()["country"], "de")   # o primeiro servidor com aparelhos fica gravado

    def test_api_host_follows_the_forced_country(self):
        from unittest import mock
        from h50console.cloud import Cloud, CloudError
        urls = []

        def req(self, url, **_):
            urls.append(url)
            raise CloudError("fim")
        c = Cloud({"user_id": "1", "ssecurity": "eA==", "service_token": "t", "country": "us"}, country="de")
        with mock.patch.object(Cloud, "_req", req):
            for country in (None, "sg"):
                with self.assertRaises(CloudError):
                    c.api("/home/device_list", {}, country)
        self.assertEqual([u.split("/")[2] for u in urls], ["de.api.io.mi.com", "sg.api.io.mi.com"])

    def test_config_reads_and_validates_the_country(self):
        import os
        from unittest import mock
        from h50console.config import Config
        for value, expected in (("", ""), ("auto", ""), (" DE ", "de"), ("i2", "i2")):
            with mock.patch.dict(os.environ, {"H50_CLOUD_COUNTRY": value}):
                self.assertEqual(Config.from_env("/nao/existe/.env").cloud_country, expected)
        with mock.patch.dict(os.environ, {"H50_CLOUD_COUNTRY": "br"}), self.assertRaises(SystemExit):
            Config.from_env("/nao/existe/.env")


if __name__ == "__main__":
    unittest.main()
