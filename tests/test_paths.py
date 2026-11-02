import unittest

from h50console.paths import PathRecorder
from h50console.store import Store


class FakeGw:
    def __init__(self):
        self.cache = {"2/2": 2, "10/1": '{"index": 1, "obj_name": "u/42/0"}'}


class Recorder(unittest.TestCase):
    def setUp(self):
        self.store, self.gw, self.now = Store(":memory:"), FakeGw(), [1000.0]
        self.maps = []                                         # o que o "robô" devolve a cada download
        self.rec = PathRecorder(self.gw, self.store, lambda: self.maps.pop(0) if self.maps else None,
                                interval=45, settle=0, clock=lambda: self.now[0])

    def path(self, n, x0=0):
        return [[x0 + i * 100, i * 50, 1] for i in range(n)]

    def new_map(self, index):                                  # o robô enviou um mapa novo para a nuvem
        self.gw.cache["10/1"] = f'{{"index": {index}, "obj_name": "u/42/0"}}'

    def test_idle_robot_downloads_nothing(self):
        self.maps = [self.path(5)]
        self.assertFalse(self.rec.tick())
        self.assertEqual(len(self.maps), 1)

    def test_live_then_final_moves_to_official_start(self):
        self.gw.cache["2/2"] = 4
        self.maps = [self.path(5), self.path(9), self.path(20)]
        self.assertTrue(self.rec.tick())
        self.now[0] += 20
        self.assertFalse(self.rec.tick())                      # mapa igual (index do 10/1) e menos de 45 s
        self.new_map(2)
        self.assertTrue(self.rec.tick())                       # mapa novo: baixa na hora
        self.assertEqual(self.store.get_path(self.rec.key)["points"], self.path(9))
        self.gw.cache["2/2"] = 2
        self.assertTrue(self.rec.finish(555))                  # fim: o início oficial vem do 2/17
        got = self.store.get_path(555)
        self.assertEqual((len(got["points"]), got["final"]), (20, True))
        self.assertEqual(self.store.path_stimes(), {555})      # a chave provisória sumiu
        self.assertIsNone(self.rec.key)

    def test_without_new_map_downloads_again_after_45s(self):
        self.gw.cache["2/2"] = 4
        self.maps = [self.path(5), self.path(8)]
        self.assertTrue(self.rec.tick())
        self.now[0] += 44
        self.assertFalse(self.rec.tick())
        self.now[0] += 1
        self.assertTrue(self.rec.tick())                       # reserva: o robô pode não trocar o index

    def test_new_maps_in_a_row_wait_the_minimum_gap(self):
        self.gw.cache["2/2"] = 4
        self.maps = [self.path(5), self.path(8)]
        self.assertTrue(self.rec.tick())
        self.new_map(2)
        self.now[0] += 5
        self.assertFalse(self.rec.tick())                      # menos de 10 s desde o último download
        self.now[0] += 5
        self.assertTrue(self.rec.tick())

    def test_outside_a_cleaning_updates_the_map_but_records_no_path(self):
        self.gw.cache["2/2"] = 7                               # lavando o mop na base, sem limpeza em andamento
        self.maps = [self.path(5), self.path(6)]
        self.assertTrue(self.rec.tick())                       # primeiro index visto conta como mapa novo
        self.assertEqual((self.rec.key, self.store.path_stimes()), (None, set()))
        self.now[0] += 100
        self.assertFalse(self.rec.tick())                      # sem limpeza não há reserva por tempo
        self.new_map(2)
        self.assertTrue(self.rec.tick())

    def test_way_back_to_the_base_still_feeds_the_cleaning_path(self):
        self.gw.cache["2/2"] = 4
        self.maps = [self.path(5), self.path(9)]
        self.rec.tick()
        self.gw.cache["2/2"] = 6                               # voltando para a base
        self.new_map(2)
        self.now[0] += 15
        self.assertTrue(self.rec.tick())
        self.assertEqual(self.store.get_path(self.rec.key)["points"], self.path(9))

    def test_10_1_is_read_with_the_fast_group(self):
        from h50console import catalog
        self.assertIn("10/1", catalog.POLL_GROUPS["fast"][0])
        self.assertNotIn("10/1", catalog.POLL_GROUPS["edits"][0])

    def test_final_failure_keeps_last_partial_under_official_key(self):
        self.gw.cache["2/2"] = 4
        self.maps = [self.path(7)]
        self.rec.tick()
        self.gw.cache["2/2"] = 2
        self.assertFalse(self.rec.finish(777))                 # nenhum mapa disponível
        got = self.store.get_path(777)
        self.assertEqual((len(got["points"]), got["final"]), (7, False))

    def test_previous_cleaning_path_is_not_reused(self):
        self.store.save_path(100, self.path(12), final=True)
        self.gw.cache["2/2"] = 4
        self.maps = [self.path(12)]                            # o mapa ainda traz o caminho da limpeza anterior
        self.rec.tick()
        self.assertEqual(self.store.path_stimes(), {100})

    def test_final_is_never_replaced_by_partial(self):
        self.store.save_path(9, self.path(30), final=True)
        self.store.save_path(9, self.path(3), final=False)
        self.assertEqual(len(self.store.get_path(9)["points"]), 30)

    def test_survives_restart_mid_cleaning(self):
        self.gw.cache["2/2"] = 4
        self.maps = [self.path(4)]
        self.rec.tick()
        key = self.rec.key
        again = PathRecorder(self.gw, self.store, lambda: None, interval=45, settle=0, clock=lambda: self.now[0])
        self.assertEqual(again.key, key)


class LiveMap(unittest.TestCase):
    def test_download_while_cleaning_becomes_the_home_map(self):
        import json
        from h50console.paths import make_fetch

        class Bus:
            def __init__(self): self.events = []
            def publish(self, ev, payload): self.events.append(ev)

        class Service:                                         # o mesmo contrato do CloudService
            state = "ok"
            def __init__(self): self.calls = []
            def fetch_map(self, obj, did, map_dir, model=""):
                self.calls.append((obj, did, map_dir, model))
                (map_dir / "latest.json").write_text(json.dumps({"path": [[1, 2, 1]]}))
            def latest_map(self, map_dir):
                return json.loads((map_dir / "latest.json").read_text())

        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            gw, svc = FakeGw(), Service()
            gw.bus, gw.cache["10/1"] = Bus(), json.dumps({"index": 1, "obj_name": "u/42/0"})
            fetch = make_fetch(gw, svc, lambda: 42, lambda: "m", map_dir=Path(d))
            self.assertEqual(fetch(), [[1, 2, 1]])
            self.assertEqual(svc.calls, [("u/42/0", "42", Path(d), "m")])
            self.assertEqual(gw.bus.events, ["mapdata"])               # a tela do Início redesenha


class Coverage(unittest.TestCase):
    def test_counts_each_cleaning_once_per_cell(self):
        import time
        from h50console import dash
        store = Store(":memory:")
        now = int(time.time())
        line = [[i * 100, 0, 1] for i in range(11)]                      # 1 m em linha reta, passando duas vezes pelos mesmos quadrados
        store.save_path(now - 3600, line + line[::-1], final=True)
        store.save_path(now - 7200, [[0, 0, 1], [300, 0, 1]], final=True)
        store.save_path(now - 40 * 86400, line, final=True)               # fora do período
        res = dash.coverage(store, 7, cell=250)
        self.assertEqual(res["cleanings"], 2)
        by_cell = {(cx, cy): n for cx, cy, n in res["cells"]}
        self.assertEqual(by_cell[(0, 0)], 2)                              # as duas limpezas cobriram o primeiro quadrado
        self.assertEqual(by_cell[(750, 0)], 1)                            # só a primeira chegou ao fim da linha
        self.assertEqual(res["max"], 2)

    def test_jump_larger_than_one_meter_is_not_filled(self):
        import time
        from h50console import dash
        store = Store(":memory:")
        store.save_path(int(time.time()) - 60, [[0, 0, 0], [5000, 0, 1], [5100, 0, 1]], final=True)
        cells = {(cx, cy) for cx, cy, _ in dash.coverage(store, 1, cell=250)["cells"]}
        self.assertIn((0, 0), cells)
        self.assertNotIn((2500, 0), cells)                                # o salto base -> área não vira "limpeza"


class Forecast(unittest.TestCase):
    def test_uses_only_the_stretch_after_the_last_replacement(self):
        from datetime import date
        from h50console import dash
        hist = [{"day": "2026-09-01", "pct": 20, "hours": 40}, {"day": "2026-09-10", "pct": 5, "hours": 10},   # antes da troca
                {"day": "2026-09-11", "pct": 100, "hours": 200}, {"day": "2026-09-21", "pct": 95, "hours": 150},
                {"day": "2026-09-26", "pct": 92, "hours": 125}]
        f = dash.consumable_forecast(hist, today=date(2026, 9, 26))
        self.assertEqual((f["per_day"], f["unit"], f["days_left"], f["based_on_days"]), (5.0, "h", 25, 15))
        self.assertEqual(f["date"], "2026-10-21")

    def test_no_forecast_without_data_or_wear(self):
        from h50console import dash
        self.assertIsNone(dash.consumable_forecast([]))
        self.assertIsNone(dash.consumable_forecast([{"day": "2026-09-01", "pct": 90, "hours": 100}]))
        flat = [{"day": "2026-09-01", "pct": 90, "hours": 100}, {"day": "2026-09-09", "pct": 90, "hours": 100}]
        self.assertIsNone(dash.consumable_forecast(flat))                # sem desgaste não há previsão
        short = [{"day": "2026-09-01", "pct": 90, "hours": 100}, {"day": "2026-09-02", "pct": 89, "hours": 90}]
        self.assertIsNone(dash.consumable_forecast(short))               # menos de 2 dias: ruído demais

    def test_falls_back_to_percent_when_hours_are_missing(self):
        from datetime import date
        from h50console import dash
        hist = [{"day": "2026-09-01", "pct": 80, "hours": None}, {"day": "2026-09-11", "pct": 70, "hours": None}]
        f = dash.consumable_forecast(hist, today=date(2026, 9, 11))
        self.assertEqual((f["unit"], f["per_day"], f["days_left"]), ("%", 1.0, 70))


if __name__ == "__main__":
    unittest.main()
