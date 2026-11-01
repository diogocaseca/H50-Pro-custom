"""Grava o caminho percorrido em cada limpeza.

O robô só guarda a trajetória da limpeza atual/última (dentro do mapa 10/1, na nuvem) e a apaga na próxima. Então o app:
  * durante a limpeza baixa o mapa de tempos em tempos (no mínimo `interval` s entre downloads) e guarda os pontos sob uma
    chave provisória (o instante em que viu a limpeza começar);
  * quando a limpeza termina (evento `cleaning`, com o início oficial vindo do 2/17) espera `settle` s para o robô
    enviar o mapa final, baixa de novo e grava como definitivo, trocando a chave provisória pelo início oficial.
Só vale para limpezas feitas com o app rodando e a conta da nuvem conectada.
"""

from __future__ import annotations

import logging
import queue
import threading
import time

from . import decoders, mapdecode
from .poller import CLEANING

log = logging.getLogger(__name__)


def _signature(points: list) -> tuple:
    return (len(points), tuple(points[0][:2]) if points else None, tuple(points[-1][:2]) if points else None)


class PathRecorder:
    def __init__(self, gw, store, fetch, interval: float = 45, settle: float = 60, clock=time.time):
        self.gw, self.store, self.fetch = gw, store, fetch     # fetch() -> lista de [x, y, tipo] ou None
        self.interval, self.settle, self.clock = interval, settle, clock
        self.key: int | None = store.get_setting("path_live_key")
        self._last_fetch = 0.0
        self._stale: tuple | None = None       # caminho do mapa anterior: não vale como o desta limpeza

    def _fresh(self, pts):
        return pts if pts and _signature(pts) != self._stale else None

    def tick(self) -> bool:
        """Chamado a cada poucos segundos. True se baixou o mapa."""
        active = self.gw.cache.get("2/2") in CLEANING
        if not active:
            return False
        now = self.clock()
        if self.key is None:
            self.key = int(now)
            self.store.set_setting("path_live_key", self.key)
            old = self.store.get_path(self._latest_final() or 0)
            self._stale = _signature(old["points"]) if old else None
        if now - self._last_fetch < self.interval:
            return False
        self._last_fetch = now
        pts = self._fresh(self._safe_fetch())
        if pts:
            self.store.save_path(self.key, pts)
        return True

    def _latest_final(self):
        rows = self.store._query("SELECT stime FROM cleaning_path WHERE final=1 ORDER BY stime DESC LIMIT 1")
        return rows[0]["stime"] if rows else None

    def _safe_fetch(self):
        try:
            return self.fetch()
        except Exception as ex:  # noqa: BLE001 - a nuvem pode estar fora; a próxima tentativa resolve
            log.info("caminho: não consegui baixar o mapa (%s)", ex)
            return None

    def finish(self, stime: int) -> bool:
        """A limpeza que começou em `stime` acabou: grava o caminho final."""
        pts = self._fresh(self._safe_fetch())
        key, self.key = self.key, None
        self.store.set_setting("path_live_key", None)
        self._last_fetch = 0.0
        if pts:
            self.store.save_path(stime, pts, final=True)
            if key is not None and key != stime:
                self.store._exec("DELETE FROM cleaning_path WHERE stime=?", (key,))
            return True
        if key is not None:                         # sem mapa final: fica o último parcial, com o início oficial
            self.store.move_path(key, stime)
        return False


def make_fetch(gw, service, did_fn, model_fn, map_dir=None):
    """Baixa o mapa atual (10/1) pela nuvem e devolve só a trajetória.

    Com `map_dir`, o mapa baixado também vira o mapa do Início (latest.json) e o evento `mapdata` redesenha a tela:
    assim o mapa acompanha a limpeza sem depender de o navegador ver o 10/1 mudar.
    """
    def fetch():
        did = did_fn()
        obj = decoders.parse_json(gw.cache.get("10/1"), {}).get("obj_name")
        if not (did and obj) or service.state != "ok":
            return None
        if map_dir is not None:
            service.fetch_map(obj, str(did), map_dir, model=model_fn())
            gw.bus.publish("mapdata", {"at": int(time.time())})
            return (service.latest_map(map_dir) or {}).get("path")
        raw = service.fetch_object(obj, str(did))
        return mapdecode.for_ui(mapdecode.decrypt(raw, model_fn(), str(did)))["path"]
    return fetch


def start(bus, recorder: PathRecorder, stop: threading.Event | None = None) -> threading.Thread:
    q = bus.subscribe()
    stop = stop or threading.Event()

    def loop():
        last_tick = 0.0
        while not stop.is_set():
            try:
                event, payload = q.get(timeout=5)
            except queue.Empty:
                event, payload = "tick", None
            try:
                if event == "cleaning" and (payload or {}).get("stime") and not payload.get("source"):
                    if stop.wait(recorder.settle):
                        break
                    recorder.finish(int(payload["stime"]))
                elif time.monotonic() - last_tick >= 5:      # eventos de estado chegam sem parar: não depender do timeout
                    last_tick = time.monotonic()
                    recorder.tick()
            except Exception:  # noqa: BLE001
                log.exception("erro ao gravar o caminho")

    t = threading.Thread(target=loop, name="path-recorder", daemon=True)
    t.start()
    return t
