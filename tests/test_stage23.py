import threading
from pathlib import Path
import time
import unittest

from h50console import catalog
from h50console.device import H50
from h50console.events import EventBus
from h50console.gateway import CommandBlocked, Gateway, PriorityLock
from h50console.poller import Poller
from h50console.store import Store

from .fake import FIXTURES, FakeClient, load_dump

HAS_FIXTURES = (FIXTURES / "dump-full.json").exists()


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.s = Store(":memory:")

    def test_migrated(self):
        from h50console.store import MIGRATIONS
        self.assertEqual(self.s.version, len(MIGRATIONS))
        Store.__init__(self.s, ":memory:")  # reabrir não quebra
        self.assertEqual(self.s.version, len(MIGRATIONS))

    def test_old_v1_database_upgrades_without_losing_data(self):
        import sqlite3
        import tempfile
        from h50console.store import MIGRATIONS
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "antigo.db"
            db = sqlite3.connect(path)
            db.executescript(f"BEGIN; {MIGRATIONS[0]}; PRAGMA user_version = 1; COMMIT;")   # como era antes do Laboratório
            db.execute("INSERT INTO cleaning_record (stime, duration_s, area_m2, sweep_type, source, updated_at) "
                       "VALUES (1, 60, 2.5, 3, 'robot', 'x')")
            db.commit()
            db.close()
            st = Store(path)
            try:
                self.assertEqual(st.version, len(MIGRATIONS))
                self.assertEqual(st.cleanings()[0]["area_m2"], 2.5)       # dados antigos intactos
                st.add_note("depois da migração")                          # tabelas novas existem
                self.assertEqual(st.notes()[0]["text"], "depois da migração")
            finally:
                st.close()

    def test_changes_and_history(self):
        self.s.save_changes({"2/2": (None, 4), "2/16": (None, '{"a":1}')})
        self.assertEqual(self.s.prop_history("2/2")[0]["new_value"], "4")
        self.assertEqual(self.s.prop_history("2/16")[0]["new_value"], '{"a":1}')

    def test_cleaning_upsert_robot_wins(self):
        self.s.upsert_cleaning(100, 60, 1.0, 3, "derived", rooms=[5])
        self.s.upsert_cleaning(100, 120, 2.0, 3, "robot", raw_label="x")
        self.s.upsert_cleaning(100, 999, 9.0, 3, "derived")           # não sobrescreve o do robô
        r = self.s.cleanings()[0]
        self.assertEqual((r["duration_s"], r["source"], r["rooms"]), (120, "robot", [5]))

    def test_cleaning_upsert_keeps_known_values(self):
        self.s.upsert_cleaning(200, 1200, 13.06, 3, "derived", rooms=[5])
        self.s.upsert_cleaning(200, 1200, 13.06, None, "derived")    # reinício do app
        r = self.s.cleanings()[0]
        self.assertEqual((r["sweep_type"], r["rooms"]), (3, [5]))

    def test_faults_open_close(self):
        self.s.open_fault(210013)
        self.s.open_fault(210013)                 # não duplica
        self.assertEqual(len(self.s.faults()), 1)
        self.s.clear_faults(keep=set())
        self.assertIsNotNone(self.s.faults()[0]["cleared"])

    def test_settings_and_rooms(self):
        self.s.set_setting("device_name", "Rex")
        self.assertEqual(self.s.get_setting("device_name"), "Rex")
        self.s.replace_rooms(1, [{"id": 3, "name": "Sala"}], {3: 0})
        self.assertEqual(self.s.rooms(1)[0]["surface"], 0)


class PriorityLockTests(unittest.TestCase):
    def test_high_goes_first(self):
        lock, order = PriorityLock(), []
        lock.acquire(high=False)                  # poll em andamento

        def worker(name, high):
            lock.acquire(high)
            order.append(name)
            lock.release()

        low = threading.Thread(target=worker, args=("poll", False))
        low.start()
        time.sleep(0.05)
        high = threading.Thread(target=worker, args=("cmd", True))
        high.start()
        time.sleep(0.05)
        lock.release()
        low.join(1)
        high.join(1)
        self.assertEqual(order, ["cmd", "poll"])


@unittest.skipUnless(HAS_FIXTURES, "fixtures reais ausentes (tests/fixtures)")
class PollerTests(unittest.TestCase):
    def setUp(self):
        self.values = load_dump()
        self.values.update({"2/2": 2, "2/17": 1791743955, "2/6": 2445, "2/7": 1620, "2/5": 1,
                            "2/40": '{"clean_mode":1}', "2/3": 0, "2/66": ""})
        self.fake = FakeClient(self.values)
        self.store = Store(":memory:")
        self.bus = EventBus()
        self.events = self.bus.subscribe()
        self.gw = Gateway(H50(self.fake), self.store, self.bus)
        self.clock = FakeClock()
        self.p = Poller(self.gw, self.store, clock=self.clock)

    def drain(self):
        out = []
        while not self.events.empty():
            out.append(self.events.get_nowait()[0])
        return out

    def test_first_cycle_reads_all_groups(self):
        wait = self.p.tick()
        self.assertTrue(self.gw.online)
        self.assertEqual(wait, 30)                          # ocioso
        self.assertEqual(self.gw.cache.get("3/1"), self.values["3/1"])
        self.assertTrue(self.store.rooms())                 # 2/16 gravado
        self.assertEqual(len(self.store.cleanings()), 1)    # partida ociosa: última limpeza registrada
        self.assertIn("state", self.drain())

    def test_only_changes_are_saved(self):
        self.p.tick()
        n = len(self.store._query("SELECT * FROM prop_change"))
        self.clock.t += 30
        self.p.tick()
        self.assertEqual(len(self.store._query("SELECT * FROM prop_change")), n)

    def test_cleaning_session_derived(self):
        self.p.tick()
        # começa a limpar o quarto (5)
        self.fake.values.update({"2/2": 4, "2/5": 3, "2/6": 0, "2/7": 0, "2/40": '{"rooms":[5],"clean_mode":3}'})
        self.clock.t += 30
        self.assertEqual(self.p.tick(), 5)                  # ativo: 5 s
        self.fake.values.update({"2/6": 1250, "2/7": 600})
        self.clock.t += 5
        self.p.tick()
        # sequência real de 11/10 16:23–16:24: volta (6), base (14), ociosa (2) com 2/5 e 2/40
        # zerados, e só no ciclo SEGUINTE 2/17 vira o início desta limpeza
        for step in ({"2/2": 6}, {"2/2": 14},
                     {"2/2": 2, "2/5": 1, "2/40": '{"clean_mode":1}'},
                     {"2/17": 1791745000}):
            self.fake.values.update(step)
            self.clock.t += 30
            self.p.tick()
        rec = {r["stime"]: r for r in self.store.cleanings()}[1791745000]
        self.assertEqual((rec["duration_s"], rec["area_m2"], rec["sweep_type"], rec["rooms"]),
                         (600, 12.5, 3, [5]))
        self.assertEqual(rec["source"], "derived")

    def test_custom_routine_label(self):
        self.p.tick()
        rd = '{"user_define":{"id":1,"name":"Pelos gatos","user_data":[["mode","mode_data"],[2,[[0,0,1,0,1,1,0,1]]]]},"clean_mode":7}'
        for step in ({"2/2": 4, "2/5": 7, "2/40": rd, "2/6": 167, "2/7": 60},
                     {"2/2": 6, "2/5": 1, "2/40": '{"clean_mode":1}'}, {"2/2": 2}, {"2/17": 1791747463}):
            self.fake.values.update(step)
            self.clock.t += 30
            self.p.tick()
        rec = {r["stime"]: r for r in self.store.cleanings()}[1791747463]
        self.assertEqual((rec["sweep_type"], rec["raw_label"], rec["area_m2"]), (7, "custom:Pelos gatos", 1.67))
        from h50console import zones                     # as zonas da rotina ficam aprendidas para o painel rodá-la
        self.assertEqual(zones.learned(self.store)["1"]["zones"], [[0, 0, 1, 0, 1, 1, 0, 1]])

    def test_restart_mid_cleaning_reconstructs_from_db(self):
        # instância 1 vê o início de uma limpeza por zonas (sequência real de 11/10 16:47)
        self.p.tick()
        zones = '{"zones":[[6148,-6012,6148,-6823,8900,-6823,8900,-6012],[8346,-4626,8346,-6073,8859,-6073,8859,-4626]],"clean_mode":2}'
        stime = int(time.time()) - 600
        self.fake.values.update({"2/2": 12, "2/5": 2, "2/40": zones})
        self.clock.t += 30
        self.p.tick()
        # app reinicia: nova instância de gateway/poller, mesmo banco; o robô já terminou
        self.fake.values.update({"2/2": 14, "2/5": 1, "2/40": '{"clean_mode":1}', "2/17": stime, "2/6": 423, "2/7": 240})
        gw2 = Gateway(H50(self.fake), self.store, EventBus())
        Poller(gw2, self.store, clock=FakeClock()).tick()
        rec = {r["stime"]: r for r in self.store.cleanings()}[stime]
        self.assertEqual((rec["sweep_type"], rec["raw_label"], rec["area_m2"]), (2, "zones:2", 4.23))

    def test_faults(self):
        self.p.tick()
        self.fake.values.update({"2/3": 210013, "2/66": '{"ts":1791736164,"fault":[210013]}'})
        self.clock.t += 30
        self.p.tick()
        self.assertIsNone(self.store.faults()[0]["cleared"])
        self.fake.values.update({"2/3": 0, "2/66": '{"ts":1791736200,"fault":[]}'})
        self.clock.t += 30
        self.p.tick()
        self.assertIsNotNone(self.store.faults()[0]["cleared"])

    def test_offline_backoff(self):
        self.p.tick()
        self.drain()
        self.fake.offline = True
        waits = []
        for _ in range(4):
            self.clock.t += 400
            waits.append(self.p.tick())
        self.assertEqual(waits, [10, 20, 40, 80])
        self.assertFalse(self.gw.online)
        self.assertIn("offline", self.drain())
        self.fake.offline = False
        self.clock.t += 400
        self.p.tick()
        self.assertTrue(self.gw.online)
        self.assertIn("online", self.drain())

    def test_commands_logged_and_blocked(self):
        self.p.tick()
        self.gw.set("2/31", 3)
        self.assertEqual(self.gw.cache.get("2/31"), 3)      # relido após o set
        self.gw.run("locate")
        self.assertTrue(self.gw.poke.is_set())
        with self.assertRaises(ValueError):
            self.gw.set("2/31", 9)
        log = self.store.commands()
        self.assertEqual([c["command"] for c in log], ["set:2/31", "run:locate", "set:2/31"])
        self.assertIn("ValueError", log[0]["error"])
        self.assertEqual({c["via"] for c in log}, {"lan"})        # por onde cada comando foi
        # base trabalhando: não deixa sair
        self.gw.cache.update({"2/2": 12})
        with self.assertRaises(CommandBlocked):
            self.gw.run("start")
        with self.assertRaises(CommandBlocked):
            self.gw.clean_rooms([6])
        self.gw.run("home")                                 # voltar para a base pode

    def test_cleaning_command_waits_for_the_robot_to_leave(self):
        self.p.tick()
        self.gw.clock = FakeClock()
        self.drain()
        self.gw.clean_rooms([6])                           # aceito (code 0), mas o robô continua na base
        self.assertIsNotNone(self.gw.pending_start)
        self.gw.check_start_confirmed()
        self.assertIsNotNone(self.gw.pending_start)        # ainda dentro do prazo
        self.gw.clock.t += 61
        with self.assertLogs("h50console.gateway", "WARNING"):
            self.gw.check_start_confirmed()
        self.assertIsNone(self.gw.pending_start)
        row = self.store.commands()[0]
        self.assertEqual((row["command"], row["via"], row["confirmed"]), ("action:2/16", "lan", 0))
        evs = []
        while not self.events.empty():
            evs.append(self.events.get_nowait())
        self.assertIn(("command_check", {"command": "action:2/16", "via": "lan", "ok": False}), evs)
        # desta vez o robô sai: o poller lê o status novo e confirma
        self.gw.clean_zones([[0, 1000, 0, 0, 1000, 0, 1000, 1000]])
        self.fake.values["2/2"] = 4
        self.clock.t += 100
        self.p.tick()
        self.assertIsNone(self.gw.pending_start)
        self.assertEqual(self.store.commands()[0]["confirmed"], 1)
        self.gw.run("locate")                              # não sai da base: nada a confirmar
        self.assertIsNone(self.gw.pending_start)
        self.assertIsNone(self.store.commands()[0]["confirmed"])

    def test_set_with_stale_reread_keeps_written_value(self):
        self.p.tick()
        orig = self.fake.send
        stale = {"2/9": self.fake.values["2/9"]}

        def lagging(method, params=None, retry=1):   # robô aceita o set, mas a releitura imediata é antiga
            if method == "set_properties":
                res = orig(method, params, retry)
                self.fake.values.update(stale)
                return res
            return orig(method, params, retry)
        self.fake.send = lagging
        new = 4 if stale["2/9"] != 4 else 3
        self.gw.set("2/9", new)
        self.assertEqual(self.gw.cache.get("2/9"), new)

    def test_thread_start_stop(self):
        self.p.start()
        time.sleep(0.3)
        self.p.stop()
        self.assertFalse(self.p._thread.is_alive())


if __name__ == "__main__":
    unittest.main()
