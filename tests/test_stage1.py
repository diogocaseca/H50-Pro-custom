import unittest
from datetime import datetime, timezone

from h50console import catalog, decoders
from h50console.device import H50, plan_batches
from h50console.miio import MiioError

from .fake import FIXTURES, FakeClient, load_dump

HAS_FIXTURES = (FIXTURES / "dump-full.json").exists() and (FIXTURES / "dump.json").exists()


class Catalog(unittest.TestCase):
    def test_spec_loaded(self):
        self.assertEqual(catalog.PROPS["2/31"].name, "drying-time")
        self.assertTrue(catalog.PROPS["2/31"].writable)
        self.assertEqual(catalog.ACTIONS["2/16"].params, (15,))
        self.assertIn("23/1", catalog.READABLE)          # fora da spec
        self.assertNotIn("2/15", catalog.READABLE)       # só parâmetro

    def test_settings_exist_and_writable(self):
        for key, _group, _label in catalog.SETTINGS:
            self.assertIn(key, catalog.PROPS, key)
            self.assertTrue(catalog.PROPS[key].writable, key)

    def test_options_in_app_order(self):
        self.assertEqual([v for v, _ in catalog.options("2/61")], [0, 2, 1, 3, 4, 5])
        self.assertEqual(dict(catalog.options("2/31"))[2], "3 h")

    def test_variants(self):
        keys = {k for k, _, _ in catalog.visible_settings({"2/62": False})}
        self.assertIn("2/61", keys)
        self.assertNotIn("2/29", keys)
        keys = {k for k, _, _ in catalog.visible_settings({"2/62": True})}
        self.assertNotIn("2/61", keys)

    def test_start_block_reason(self):
        self.assertIsNone(catalog.start_block_reason({"2/2": 14, "2/18": '{"mode":1,"progress":5}'}))   # secando: pode
        self.assertIsNone(catalog.start_block_reason({"2/2": 2}))
        self.assertIn("lavando", catalog.start_block_reason({"2/2": 14, "2/18": '{"mode":3,"progress":40}'}))
        self.assertEqual(catalog.start_block_reason({"2/2": 12}), "base trabalhando")

    def test_label(self):
        self.assertEqual(catalog.label("2/2", 4), "Limpando")
        self.assertEqual(catalog.label("2/30", 0), "Minuciosa")
        self.assertEqual(catalog.label("3/1", 95), "95")


@unittest.skipUnless(HAS_FIXTURES, "fixtures reais ausentes (tests/fixtures)")
class Decoders(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.v = load_dump()

    def test_clean_record_totals(self):
        r = decoders.clean_record(self.v["10/3"])
        self.assertEqual(r["total_count"], 12)
        self.assertEqual(len(r["records"]), 12)
        self.assertEqual(r["total_min"], 188)
        self.assertAlmostEqual(r["total_area_m2"], 151.78)
        self.assertEqual(sum(x["duration_s"] for x in r["records"]), 188 * 60)
        self.assertAlmostEqual(sum(x["area_m2"] for x in r["records"]), 151.78, places=3)

    def test_schedules(self):
        s = decoders.schedules(self.v["2/19"])
        self.assertEqual(s[0]["time"], "06:30")
        self.assertEqual(s[0]["days"], ["seg", "ter", "qua", "qui", "sex"])
        self.assertTrue(s[0]["repeat"])
        self.assertEqual(s[1]["time"], "08:30")
        self.assertEqual(s[1]["days"], ["dom", "sáb"])
        self.assertEqual(s[1]["mode"], 7)

    def test_rooms(self):
        r = decoders.rooms(self.v["2/16"])
        self.assertEqual(r["map_uid"], 1)
        self.assertEqual({x["id"]: x["name"] for x in r["rooms"]}[4], "Quarto de criança")
        self.assertEqual(set(decoders.room_surfaces(self.v["23/1"])), {3, 4, 5, 6, 7, 8})

    def test_zones(self):
        self.assertEqual(decoders.zone_region([2500, 700, 1000, -500]), [1000, 700, 1000, -500, 2500, -500, 2500, 700])
        self.assertEqual(decoders.zone_region([1000, -500, 1000, 700, 2500, 700, 2500, -500]),
                         [1000, 700, 1000, -500, 2500, -500, 2500, 700])
        for bad in ([1, 2, 3], [0, 0, 100, 900], [0, 0, 40000, 900], "x", [0, 0, True, 900]):
            with self.assertRaises(ValueError):
                decoders.zone_region(bad)
        self.assertEqual(decoders.zone_param([[0, 0, 1000, 1000]]),
                         '[{"blocks_region":[0,1000,0,0,1000,0,1000,1000],"blocks_attr":0}]')
        with self.assertRaises(ValueError):
            decoders.zone_param([[0, 0, 1000, 1000]] * 11)

    def test_custom_cleans(self):
        c = {x["name"]: x for x in decoders.custom_cleans(self.v["2/42"])}
        self.assertEqual(c["Sala intenso"]["room_ids"], [3, 8])

    def test_dnd(self):
        self.assertEqual(decoders.dnd_period(385878016), ("23:00", "08:00"))
        self.assertEqual(decoders.dnd_period(385877504), ("23:00", "06:00"))   # observado no app
        self.assertEqual(decoders.encode_dnd("23:00", "06:00"), 385877504)
        with self.assertRaises(ValueError):
            decoders.encode_dnd("24:00", "06:00")

    def test_faults_and_misc(self):
        f = decoders.faults('{"ts":1791736164,"fault":[210013]}')
        self.assertEqual(f["codes"], [210013])
        self.assertEqual(f["ts"], datetime.fromtimestamp(1791736164, timezone.utc))
        self.assertEqual(decoders.faults("")["codes"], [])
        self.assertEqual(decoders.progress(-1), None)
        self.assertEqual(decoders.area_m2(2445), 24.45)
        self.assertEqual(decoders.current_config('{"rooms":[8],"clean_mode":3}')["rooms"], [8])
        self.assertEqual(decoders.current_config('{"clean_mode":1}')["rooms"], [])

    def test_drying_percent(self):
        secando = '{"mode":1,"progress":0}'
        # o H50 Pro deixa o 2/88 em 0: calcula pela duração (2/31 = 2 -> 3 h) e minutos restantes (2/90)
        self.assertEqual(decoders.drying_percent({"2/18": secando, "2/88": 0, "2/31": 2, "2/90": 135}), 25)
        self.assertEqual(decoders.drying_percent({"2/18": secando, "2/31": 1, "2/90": 0}), 100)
        self.assertEqual(decoders.drying_percent({"2/18": "{}", "2/31": 3, "2/90": 60}), 75)   # só os minutos restantes
        self.assertEqual(decoders.drying_percent({"2/18": secando, "2/31": 1, "2/90": 999}), 0)
        self.assertEqual(decoders.drying_percent({"2/18": secando, "2/88": 40, "2/31": 2, "2/90": 10}), 40)  # vale o informado
        self.assertEqual(decoders.drying_percent({"2/18": '{"mode":0}', "2/88": 0, "2/31": 2, "2/90": 0}), 0)
        self.assertIsNone(decoders.drying_percent({"2/18": secando, "2/31": 9, "2/90": 30}))
        # o progresso do 2/18 é a % da secagem (observado em 11/11/2026: 1 a 100 ao longo da secagem)
        self.assertEqual(decoders.drying_percent({"2/18": '{"mode":1,"progress":16}', "2/31": 2, "2/90": 150}), 16)
        # interrompida por uma limpeza: a base sai do modo 1 e o 2/90 fica com o último valor
        self.assertEqual(decoders.drying_percent({"2/18": '{"mode":0,"progress":0}', "2/31": 2, "2/90": 150}), 0)
        self.assertEqual(decoders.drying_percent({"2/18": '{"mode":3,"progress":40}', "2/90": 150}), 0)   # lavando o mop


class Batching(unittest.TestCase):
    def test_plan_respects_budget(self):
        sizes = {"a": 600, "b": 600, "c": 70}
        self.assertEqual(plan_batches(["a", "b", "c"], sizes, 1150), [["a"], ["b", "c"]])
        self.assertEqual(plan_batches(["x"] * 3, {}, 1150), [["x", "x", "x"]])


@unittest.skipUnless(HAS_FIXTURES, "fixtures reais ausentes (tests/fixtures)")
class Device(unittest.TestCase):
    def setUp(self):
        self.fake = FakeClient(load_dump())
        self.dev = H50(self.fake)

    def test_read_everything_within_limit(self):
        res = self.dev.get(catalog.READABLE)
        ok = [k for k, r in res.items() if r["code"] == 0]
        self.assertEqual(res["10/3"]["code"], "oversized")   # nem tenta (P7)
        self.assertGreater(len(ok), 160)
        # nenhum lote estourou o limite: nenhuma chamada caiu no item-a-item
        gets = [p for m, p in self.fake.calls if m == "get_properties"]
        self.assertLess(len(gets), 25)
        self.assertTrue(all(len(p) >= 1 for p in gets))

    def test_fallback_item_by_item(self):
        self.dev.sizes = {}                     # sem estimativas: lotes grandes demais
        self.dev.budget = 5000
        res = self.dev.get(["2/16", "2/19", "2/42", "2/1", "10/5", "10/13", "2/2"])
        self.assertTrue(all(r["code"] == 0 for r in res.values()))

    def test_offline_raises(self):
        self.fake.offline = True
        with self.assertRaises(MiioError):
            self.dev.get(["2/2", "3/1"])

    def test_values_matched_by_siid_piid(self):
        v = self.dev.values(["2/2", "3/1"])
        self.assertEqual(set(v), {"2/2", "3/1"})

    def test_set_validation(self):
        self.assertEqual(self.dev.set("2/31", 2), {"code": 0, "value": 2})
        self.assertEqual(self.dev.set("11/1", "on")["value"], True)
        for key, bad in [("2/31", 9), ("4/2", 101), ("2/2", 1), ("99/1", 1)]:
            with self.assertRaises(ValueError, msg=key):
                self.dev.set(key, bad)

    def test_actions(self):
        self.dev.clean_rooms([6, 7])
        m, p = self.fake.calls[-1]
        self.assertEqual(p["in"], [{"piid": 15, "value": "6,7"}])
        self.dev.run("locate")
        self.assertEqual(self.fake.calls[-1][1]["siid"], 6)
        with self.assertRaises(ValueError):
            self.dev.action("2/16")             # falta o parâmetro


if __name__ == "__main__":
    unittest.main()
