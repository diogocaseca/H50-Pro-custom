import tempfile
import unittest
from pathlib import Path
from unittest import mock

from h50console import catalog
from h50console.fleet import Fleet, RobotConfig
from h50console.hub import Hub
from h50console.web import ApiError

from .fake import FakeClient, load_dump, FIXTURES

HAS_FIXTURES = (FIXTURES / "dump-full.json").exists()
DID = "1000000001"


@unittest.skipUnless(HAS_FIXTURES, "fixtures reais ausentes (tests/fixtures)")
class Lab(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        vals = load_dump()
        vals["24/1"] = "segredo-novo"                         # propriedade FORA do catálogo (nem como extra)
        self.fake = FakeClient(vals)
        self.fleet = Fleet(Path(self.tmp.name), None, client_factory=lambda cfg: self.fake, start_threads=False)
        self.addCleanup(self.fleet.stop_all)
        self.robot = self.fleet.add(RobotConfig(id="", name="Rex", did=DID), start=False)
        self.hub = Hub(self.fleet, None)

    def call(self, method, path, query=None, body=None):
        return self.hub.dispatch(method, f"/api/r/rex{path}", query or {}, body)

    def test_props_lists_everything_with_flags(self):
        self.call("POST", "/lab/read", body={"keys": ["2/2", "3/1"]})
        props = {p["key"]: p for p in self.call("GET", "/lab/props")}
        self.assertEqual(props["2/2"]["name"], "status")
        self.assertIsNotNone(props["2/2"]["value"])
        self.assertTrue(props["2/31"]["writable"])
        self.assertEqual(props["2/31"]["options"][1], [2, "3 h"])
        self.assertFalse(props["2/2"]["writable"])
        self.assertFalse(props["23/1"]["in_spec"])                       # extra conhecida, fora da spec
        self.assertGreater(len(props), 150)

    def test_read_all_refreshes_the_cache(self):
        out = self.call("POST", "/lab/read", body={})
        self.assertIn("2/2", out["values"])
        self.assertEqual(out["via"], "lan")
        self.assertIsNotNone(self.robot.gw.cache.get("2/16"))

    def test_scan_marks_new_properties_and_keeps_them(self):
        res = self.call("POST", "/lab/scan", body={"siid": 24, "first": 1, "last": 3})["results"]
        by = {r["key"]: r for r in res}
        self.assertTrue(by["24/1"]["new"])
        self.assertEqual(by["24/1"]["value"], "segredo-novo")
        self.assertIsNone(by["24/2"]["value"])                          # não respondeu
        known = self.call("POST", "/lab/scan", body={"siid": 21, "first": 1, "last": 1})["results"][0]
        self.assertTrue(known["known"] and not known["new"])            # 21/1 já é um extra conhecido
        again = {p["key"]: p for p in self.call("GET", "/lab/props")}
        self.assertTrue(again["24/1"].get("discovered"))                # ficou guardada como descoberta
        self.assertEqual(again["24/1"]["service"], "x-descobertas")

    def test_scan_validation(self):
        for bad in ({}, {"siid": 21, "first": 5, "last": 1}, {"siid": 21, "first": 1, "last": 200},
                    {"siid": 0, "first": 1, "last": 2}, {"siid": "x", "first": 1, "last": 2}):
            with self.assertRaises(ApiError, msg=bad):
                self.call("POST", "/lab/scan", body=bad)

    def test_changes_and_notes_hide_noise_by_default(self):
        st = self.robot.store
        st.save_changes({"2/31": (1, 2), "2/89": (10, 11), "3/1": (90, 89)})
        self.call("POST", "/lab/notes", body={"text": "mudei a secagem para 3 h"})
        out = self.call("GET", "/lab/changes", {"minutes": ["30"]})
        self.assertEqual([c["prop"] for c in out["changes"]], ["2/31"])   # progresso e bateria são ruído
        self.assertEqual(out["changes"][0]["name"], "drying-time")
        self.assertEqual(out["notes"][0]["text"], "mudei a secagem para 3 h")
        st.save_changes({"2/4": (None, 3)})                                # leitura inicial: escondida por padrão
        self.assertEqual([c["prop"] for c in self.call("GET", "/lab/changes", {"minutes": ["30"]})["changes"]], ["2/31"])
        noisy = self.call("GET", "/lab/changes", {"minutes": ["30"], "noisy": ["1"]})
        self.assertEqual({c["prop"] for c in noisy["changes"]}, {"2/31", "2/89", "3/1", "2/4"})
        with self.assertRaises(ApiError):
            self.call("POST", "/lab/notes", body={"text": "   "})

    def test_noise_does_not_hide_older_changes_and_window_goes_to_7_days(self):
        st = self.robot.store
        st.save_changes({"2/42": ("a", "b")})
        for i in range(700):                                               # muito ruído depois (progresso da limpeza)
            st.save_changes({"2/89": (i, i + 1)})
        out = self.call("GET", "/lab/changes", {"minutes": ["10080"]})
        self.assertEqual([c["prop"] for c in out["changes"]], ["2/42"])    # o filtro vem antes do limite
        self.assertFalse(out["truncated"])
        st._exec("UPDATE prop_change SET ts = '2000-01-01T00:00:00+00:00' WHERE prop = '2/42'")
        self.assertEqual(self.call("GET", "/lab/changes", {"minutes": ["99999"]})["changes"], [])   # máximo: 7 dias

    def test_action_risk_classification(self):
        risk = {a["key"]: a["risk"] for a in self.call("GET", "/lab/actions")}
        self.assertEqual(risk["2/30"], "safe")                          # get-system-info
        self.assertEqual(risk["2/10"], "safe")                          # get-zone-configs
        self.assertEqual(risk["2/1"], "acts")                           # start-sweep
        self.assertEqual(risk["2/37"], "acts")                          # start-zone-sweep
        self.assertEqual(risk["2/20"], "acts")                          # start-dry
        self.assertEqual(risk["10/1"], "destructive")                   # clear-map
        self.assertEqual(risk["10/2"], "destructive")                   # delete-map
        self.assertEqual(risk["2/15"], "destructive")                   # merge-rooms
        self.assertEqual(risk["10/8"], "destructive")                   # restore-map
        self.assertEqual(catalog.action_risk("get-history-obj"), "safe")
        self.assertEqual(catalog.action_risk("operate-ai-recognize-obj"), "acts")      # desconhecida: prudência
        spec = {a["key"]: a for a in self.call("GET", "/lab/actions")}
        self.assertEqual(spec["2/16"]["params"][0]["piid"], 15)

    def test_cloud_map_only_serves_this_robots_objects(self):
        self.hub.cloud = mock.Mock()
        self.robot.app.cloud = self.hub.cloud
        for bad in ("", "1/2/3", f"9/{DID}/x", f"9/999/1", f"9/{DID}/1/extra", f"../{DID}/1"):
            with self.assertRaises(ApiError, msg=bad):
                self.call("GET", "/lab/cloud-map", {"obj": [bad]})
        self.hub.cloud.fetch_object.assert_not_called()

    def test_cloud_objects_need_the_cloud(self):
        with self.assertRaises(ApiError) as cm:
            self.call("GET", "/lab/cloud-objects")
        self.assertEqual(cm.exception.status, 503)


if __name__ == "__main__":
    unittest.main()
