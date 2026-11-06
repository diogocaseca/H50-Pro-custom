"""Robô sem internet: respostas vazias e consumíveis no máximo não apagam o que o painel já sabia."""
import unittest

from h50console import catalog
from h50console.device import H50
from h50console.events import EventBus
from h50console.gateway import Gateway
from h50console.store import Store

from . import synth
from .fake import FakeClient

ROUTINES = '{"user_labels":[{"id":1,"name":"Sala intenso","room_ids":[3],"mop":[1]}]}'
PCTS = [pct for pct, _ in catalog.CONSUMABLE_PROPS.values()]
ALL_CONSUMABLES = [k for pair in catalog.CONSUMABLE_PROPS.values() for k in pair]


class OfflineDefaults(unittest.TestCase):
    def setUp(self):
        self.values = synth.dump_values()
        self.values.update({"2/42": ROUTINES, "2/66": "", **{k: 80 for k in PCTS}, **{k: 150 for k in ALL_CONSUMABLES
                                                                                    if k not in PCTS}})
        self.fake = FakeClient(self.values)
        self.gw = Gateway(H50(self.fake), Store(":memory:"), EventBus())
        self.gw.read(["2/42", "2/16", "2/66", *ALL_CONSUMABLES])

    def offline(self, **vals):
        self.fake.values.update(vals)
        return self.gw.read(["2/42", "2/16", "2/66", *ALL_CONSUMABLES])

    def test_empty_routines_keep_the_last_known(self):
        with self.assertLogs("h50console.gateway", "WARNING"):
            changes = self.offline(**{"2/42": "", "2/16": ""})
        self.assertNotIn("2/42", changes)
        self.assertEqual(self.gw.cache.get("2/42"), ROUTINES)
        self.assertIn("2/42", self.gw.held_back)
        changes = self.offline(**{"2/42": '{"user_labels":[]}'})          # apagou todas no app: vale
        self.assertEqual(self.gw.cache.get("2/42"), '{"user_labels":[]}')
        self.assertNotIn("2/42", self.gw.held_back)

    def test_empty_is_real_outside_the_cloud_backed_props(self):
        self.offline(**{"2/66": '[{"code":1}]'})
        self.offline(**{"2/66": ""})                                      # falha resolvida
        self.assertEqual(self.gw.cache.get("2/66"), "")

    def test_all_consumables_at_100_is_ignored(self):
        with self.assertLogs("h50console.gateway", "WARNING"):
            changes = self.offline(**{k: 100 for k in PCTS}, **{k: 200 for k in ALL_CONSUMABLES if k not in PCTS})
        self.assertFalse(set(changes) & set(ALL_CONSUMABLES))
        self.assertEqual(self.gw.cache.get("9/1"), 80)
        self.offline(**{k: 79 for k in PCTS})                             # internet voltou: valores reais
        self.assertEqual((self.gw.cache.get("9/1"), self.gw.held_back), (79, set()))

    def test_one_replaced_consumable_is_accepted(self):
        changes = self.offline(**{"9/1": 100, "9/2": 180})
        self.assertEqual((changes["9/1"][1], self.gw.cache.get("9/2")), (100, 180))
