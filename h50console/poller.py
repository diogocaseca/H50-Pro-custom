"""Poller: thread que lê o robô por grupos (catalog.POLL_GROUPS) e alimenta cache, banco e SSE.

Intervalos adaptativos: 'ativo' quando 2/2 ∈ ACTIVE_STATUS, 'ocioso' no resto.
Offline: backoff exponencial até 5 min, sem travar nada (a UI lê o cache).

Efeitos colaterais de cada ciclo:
- prop_change: tudo que mudou;
- cleaning_record: histórico DERIVADO (P7: o 10/3 não chega pela LAN). Observado em 11/10:
  ao fim da base (14 -> 2), 2/5 e 2/40 são zerados e ~5 s DEPOIS 2/17 passa a valer o início
  da limpeza que terminou; 2/6/2/7 guardam área e tempo dela até a próxima começar. Por isso o
  tipo (2/5) e os cômodos (2/40) são memorizados DURANTE a sessão e usados quando 2/17 muda;
- fault_event: abre/fecha conforme 2/3 e 2/66;
- room: substituído quando 2/16 ou 23/1 mudam;
- consumable_snapshot: foto diária.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from . import catalog, decoders, zones
from .gateway import Gateway
from .miio import MiioError
from .store import Store, iso as store_iso

log = logging.getLogger(__name__)

FAST_KEYS = catalog.POLL_GROUPS["fast"][0]
MAX_BACKOFF = 300
CLEANING = {4, 5, 10, 16, 17, 18}   # limpando, pausado, mapeando, aspira+pano, pano, mapeamento pausado


def _absorb(session: dict, sweep_type, cfg_raw) -> None:
    """Junta à sessão o tipo (2/5) e o que o 2/40 diz: cômodos, rotina ou zonas."""
    if sweep_type:
        session.setdefault("sweep_type", sweep_type)
    cfg = decoders.current_config(cfg_raw)
    if cfg.get("rooms"):
        session["rooms"] = cfg["rooms"]
    custom = decoders.running_custom(cfg_raw)
    if custom:
        session["label"] = f"custom:{custom['name']}"
    elif cfg.get("zones"):
        session["label"] = f"zones:{len(cfg['zones'])}"


class Poller:
    def __init__(self, gw: Gateway, store: Store, clock=time.monotonic):
        self.gw = gw
        self.store = store
        self.clock = clock
        self.next_due = {g: 0.0 for g in catalog.POLL_GROUPS}   # tudo vence no primeiro ciclo
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_purge_day: str | None = None
        self._session: dict = {}              # tipo e cômodos da limpeza em andamento
        self.retention_days = lambda: 90      # a frota troca por uma leitura de settings.json

    # -- ciclo -----------------------------------------------------------------
    def active(self) -> bool:
        return self.gw.cache.get("2/2") in catalog.ACTIVE_STATUS

    def tick(self) -> float:
        """Um ciclo: lê os grupos vencidos e devolve quantos segundos dormir."""
        now = self.clock()
        due = [g for g, t in self.next_due.items() if t <= now]
        if not due:
            return max(0.5, min(self.next_due.values()) - now)
        keys = list(dict.fromkeys(k for g in due for k in catalog.POLL_GROUPS[g][0]))
        try:
            changes = self.gw.read(keys)
        except MiioError as ex:
            wait = min(MAX_BACKOFF, 10 * 2 ** (self.gw.fails - 1))
            log.warning("robô sem resposta (%s falhas): %s; nova tentativa em %ss", self.gw.fails, ex, wait)
            for g in due:
                self.next_due[g] = now + wait
            return wait

        if changes:
            self.store.save_changes(changes)
            self.gw.bus.publish("state", {"changes": {k: v[1] for k, v in changes.items()}})
        self._side_effects(changes, due)

        mode = 0 if self.active() else 1
        factor = 3 if self.gw.via == "cloud" else 1       # a nuvem é mais lenta e tem limite de uso
        for g in due:
            self.next_due[g] = now + catalog.POLL_GROUPS[g][1][mode] * factor
        # se o robô acabou de ficar ativo, o grupo rápido passa a valer o intervalo curto já
        if mode == 0:
            self.next_due["fast"] = min(self.next_due["fast"], now + catalog.POLL_GROUPS["fast"][1][0] * factor)
        return max(0.5, min(self.next_due.values()) - now)

    # -- efeitos ---------------------------------------------------------------
    def _reconstruct(self, stime: int) -> dict:
        """Tipo/cômodos/rotina da limpeza que começou em `stime`, a partir do prop_change.

        No início da limpeza o robô grava o 2/5 e o 2/40 da sessão; ao fim, zera os dois.
        Então vale a primeira mudança a partir do início (com folga de 30 s antes).
        """
        since = datetime.fromtimestamp(stime - 30, timezone.utc).isoformat(timespec="seconds")
        first = {}
        for prop in ("2/5", "2/40"):
            rows = self.store._query(
                "SELECT new_value FROM prop_change WHERE prop=? AND ts>=? AND old_value IS NOT NULL ORDER BY ts, id LIMIT 1",
                (prop, since))
            if rows:
                first[prop] = rows[0]["new_value"]
        session: dict = {}
        stype = first.get("2/5")
        _absorb(session, int(stype) if stype and stype.isdigit() else None, first.get("2/40"))
        return session

    def _side_effects(self, changes: dict, due: list[str]) -> None:
        cache = self.gw.cache
        # nova limpeza saindo da base: começa a sessão do zero (antes de memorizar o que está nela)
        if "2/2" in changes and changes["2/2"][1] in CLEANING and changes["2/2"][0] not in catalog.ACTIVE_STATUS:
            self._session = {}
        # rotina por zonas rodando (app Xiaomi Home): guarda as zonas dela para o painel poder rodá-la depois
        if "2/40" in changes and zones.learn(self.store, changes["2/40"][1]):
            self.gw.bus.publish("zones", {"learned": True})
        # memoriza tipo, cômodos e rotina só enquanto LIMPA: na volta à base (6) o 2/5 já foi zerado
        if cache.get("2/2") in CLEANING:
            _absorb(self._session, cache.get("2/5"), cache.get("2/40"))

        # histórico derivado: 2/17 mudou = uma limpeza terminou. Na partida (antigo = None) só vale
        # se o robô estiver ocioso: aí 2/6 e 2/7 ainda são da última limpeza.
        ended = "2/17" in changes and changes["2/17"][1] and (changes["2/17"][0] is not None or not self.active())
        if ended:
            stime = int(changes["2/17"][1])
            # sessão não acompanhada (app reiniciado no meio): reconstrói pelo prop_change do banco
            session = self._session or self._reconstruct(stime)
            self.store.upsert_cleaning(
                stime=stime, duration_s=cache.get("2/7"), area_m2=decoders.area_m2(cache.get("2/6")),
                sweep_type=session.get("sweep_type"), source="derived",
                rooms=session.get("rooms"), raw_label=session.get("label"),
            )
            self.gw.bus.publish("cleaning", {"stime": changes["2/17"][1]})
            self._session = {}

        # falhas
        if "2/3" in changes or "2/66" in changes:
            codes = set(decoders.faults(cache.get("2/66")).get("codes", []))
            if cache.get("2/3"):
                codes.add(cache.get("2/3"))
            for c in codes:
                self.store.open_fault(int(c))
            self.store.clear_faults(keep=codes)
            if codes:
                self.gw.bus.publish("fault", {"codes": sorted(codes)})

        # cômodos
        if "2/16" in changes or "23/1" in changes:
            r = decoders.rooms(cache.get("2/16"))
            if r["map_uid"] is not None:
                self.store.replace_rooms(r["map_uid"], r["rooms"], decoders.room_surfaces(cache.get("23/1")))

        # mapa editado no app (paredes, zonas, cômodos): a geometria fica na nuvem, só o aviso chega.
        # 10/13 (backup do mapa) muda na edição; 10/5 também muda ao fim de toda limpeza, não serve.
        if "10/13" in changes and changes["10/13"][0] is not None:
            self.store.set_setting("map_edited_at", store_iso())
            self.gw.bus.publish("map", {"at": store_iso()})

        # consumíveis: uma foto por dia (INSERT OR REPLACE mantém a última do dia)
        if "consumables" in due:
            snap = {item: (cache.get(p), cache.get(h)) for item, (p, h) in catalog.CONSUMABLE_PROPS.items()}
            self.store.snapshot_consumables(snap)

        # histórico do robô (10/3), se um dia couber na resposta UDP
        if "10/3" in changes and changes["10/3"][1]:
            for r in decoders.clean_record(changes["10/3"][1])["records"]:
                self.store.upsert_cleaning(r["stime"], r["duration_s"], r["area_m2"], r["sweep_type"],
                                           source="robot", raw_label=r["raw_label"])

        # retenção diária
        day = time.strftime("%Y-%m-%d")
        if day != self._last_purge_day:
            self._last_purge_day = day
            self.store.purge_changes(None, days=int(self.retention_days()))

    # -- thread ----------------------------------------------------------------
    def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                wait = self.tick()
            except Exception:  # noqa: BLE001 - o poller não pode morrer
                log.exception("erro no ciclo do poller")
                wait = 10
            # um comando acorda o poller: confirma o efeito em ~1 s
            if self.gw.poke.wait(wait):
                self.gw.poke.clear()
                self.next_due["fast"] = 0
                self.next_due["settings"] = 0     # confirma ajustes gravados (o robô aplica com atraso)
                self._stop.wait(2)

    def start(self) -> None:
        self._thread = threading.Thread(target=self.run_forever, name="poller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.gw.poke.set()
        if self._thread:
            self._thread.join(timeout=10)
