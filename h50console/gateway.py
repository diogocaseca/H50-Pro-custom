"""DeviceGateway: o ÚNICO dono do acesso ao robô (spec §2).

- Serializa tudo com um lock de prioridade: comandos do usuário passam na frente do poll.
- Mantém o cache de estado (o que a UI lê), online/offline e latência.
- Registra todo comando em command_log e acorda o poller para confirmar o efeito.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from . import catalog
from .device import H50
from .events import EventBus
from .miio import MiioError
from .store import Store, iso

log = logging.getLogger(__name__)
START_CONFIRM_S = 60      # depois de um comando de limpeza aceito, espera o robô sair da base por até este tempo


class CommandBlocked(Exception):
    """Comando recusado pelo estado atual do robô (ex.: base trabalhando)."""


class PriorityLock:
    """Lock em que donos de alta prioridade (comandos) passam na frente dos de baixa (poll)."""

    def __init__(self):
        self._cond = threading.Condition()
        self._held = False
        self._high_waiting = 0

    def acquire(self, high: bool) -> None:
        with self._cond:
            if high:
                self._high_waiting += 1
                while self._held:
                    self._cond.wait()
                self._high_waiting -= 1
            else:
                while self._held or self._high_waiting:
                    self._cond.wait()
            self._held = True

    def release(self) -> None:
        with self._cond:
            self._held = False
            self._cond.notify_all()


class StateCache:
    """Último valor conhecido de cada prop, com o horário da última mudança."""

    def __init__(self):
        self.values: dict[str, object] = {}
        self.changed_at: dict[str, str] = {}
        self._lock = threading.Lock()

    def update(self, values: dict[str, object]) -> dict[str, tuple[object, object]]:
        """Aplica valores lidos e devolve só o que mudou: {"2/2": (antigo, novo)}."""
        now = iso()
        changes = {}
        with self._lock:
            for k, v in values.items():
                old = self.values.get(k)
                if k not in self.values or old != v or type(old) is not type(v):
                    changes[k] = (old, v)
                    self.values[k] = v
                    self.changed_at[k] = now
        return changes

    def get(self, key: str, default=None):
        with self._lock:
            return self.values.get(key, default)

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return dict(self.values)


class Gateway:
    def __init__(self, device: H50, store: Store, bus: EventBus):
        self.dev = device
        self.store = store
        self.bus = bus
        self.cache = StateCache()
        self.lock = PriorityLock()
        self.online: bool | None = None       # None = ainda não falou com o robô
        self.fails = 0
        self.last_ok: str | None = None
        self.last_latency_ms: int | None = None
        self.poke = threading.Event()         # acorda o poller após um comando
        self.clock = time.monotonic
        self.pending_start: dict | None = None   # comando de limpeza aceito, esperando o robô sair da base
        self.held_back: set[str] = set()         # props com o valor de "robô sem internet" ignorado agora

    # -- núcleo -------------------------------------------------------------------
    def _call(self, fn: Callable, *args, high: bool):
        self.lock.acquire(high)
        t0 = time.perf_counter()
        try:
            res = fn(*args)
        except MiioError:
            self._set_online(False)
            raise
        finally:
            self.last_latency_ms = int((time.perf_counter() - t0) * 1000)
            self.lock.release()
        self._set_online(True)
        return res

    def _set_online(self, ok: bool) -> None:
        if ok:
            self.last_ok = iso()
            self.fails = 0
        else:
            self.fails += 1
        if ok != self.online:
            self.online = ok
            self.bus.publish("online" if ok else "offline", self.health())

    @property
    def via(self) -> str:
        """Transporte em uso: "lan" ou "cloud"."""
        return getattr(self.dev.client, "mode", "lan")

    def health(self) -> dict:
        return {"online": self.online, "fails": self.fails, "last_ok": self.last_ok,
                "latency_ms": self.last_latency_ms, "via": self.via}

    # -- leitura (poller) ---------------------------------------------------------
    def read(self, keys: list[str]) -> dict[str, tuple[object, object]]:
        """Lê com prioridade baixa, atualiza o cache e devolve as mudanças."""
        values = self._call(self.dev.values, keys, high=False)
        return self.cache.update(self._without_offline_defaults(values))

    def _hold(self, key: str, why: str) -> None:
        if key not in self.held_back:
            self.held_back.add(key)
            log.warning("%s: %s (robô sem internet?); mantendo o último valor", key, why)

    def _without_offline_defaults(self, values: dict[str, object]) -> dict[str, object]:
        """Tira o que o robô sem internet devolve no lugar dos dados reais, se já houver um valor conhecido."""
        out = dict(values)
        for k in catalog.CLOUD_BACKED_PROPS & out.keys():
            if isinstance(out[k], str) and not out[k].strip() and self.cache.get(k):
                self._hold(k, "veio vazio")
                del out[k]
        pcts = [pct for pct, _ in catalog.CONSUMABLE_PROPS.values()]
        if all(out.get(k) == 100 for k in pcts):
            before = [self.cache.get(k) for k in pcts]
            if any(isinstance(v, (int, float)) and v < 100 for v in before):   # todos de uma vez: não é uma troca
                for k in (k for pair in catalog.CONSUMABLE_PROPS.values() for k in pair):
                    if k in out:
                        self._hold(k, "todos os consumíveis vieram em 100 %")
                        del out[k]
        for k in [k for k in self.held_back if k in out]:
            self.held_back.discard(k)
            log.info("%s: o robô voltou a mandar o valor real", k)
        return out

    # -- comandos (UI/API) ----------------------------------------------------------
    def _check_start(self, name: str) -> None:
        reason = catalog.start_block_reason(self.cache.snapshot())
        if name in catalog.LEAVES_BASE and reason:
            raise CommandBlocked(f"robô ocupado: {reason} (status {self.cache.get('2/2')})")

    def _send(self, fn: Callable, *args):
        wake = getattr(self.dev.client, "wake_lan", None)    # comando do usuário: tenta a LAN mesmo na janela da nuvem
        if wake:
            wake()
        return fn(*args)

    def _command(self, label: str, params, fn: Callable, *args, source: str = "ui", reread: list[str] = (),
                 leaves_base: bool = False):
        t0 = time.perf_counter()
        code, err, res = None, None, None
        try:
            res = self._call(self._send, fn, *args, high=True)
            code = res.get("code") if isinstance(res, dict) else 0
            if reread:
                changes = self.cache.update(self._call(self.dev.values, list(reread), high=True))
                if changes:
                    self.store.save_changes(changes)
                    self.bus.publish("state", {"changes": {k: v[1] for k, v in changes.items()}})
            return res
        except (MiioError, ValueError) as ex:
            err = f"{type(ex).__name__}: {ex}"
            raise
        finally:
            via = self.via
            cid = self.store.log_command(source, label, params, code, int((time.perf_counter() - t0) * 1000), err, via)
            if leaves_base and err is None and code == 0:
                self.pending_start = {"id": cid, "command": label, "via": via, "since": self.clock()}
            self.bus.publish("command", {"command": label, "code": code, "error": err, "via": via})
            self.poke.set()

    def check_start_confirmed(self) -> None:
        """Chamado pelo poller a cada ciclo: o robô saiu da base depois do último comando de limpeza aceito?

        A nuvem já aceitou comando (code 0) que não chegou ao robô; sem esta conferência parece que deu certo.
        """
        p = self.pending_start
        if p is None:
            return
        if self.cache.get("2/2") in catalog.ACTIVE_STATUS:     # limpando, lavando o mop antes de sair, trocando de tarefa…
            ok = True
        elif self.clock() - p["since"] >= START_CONFIRM_S:
            ok = False
            log.warning("o robô aceitou %s (via %s) mas não saiu da base em %s s", p["command"], p["via"], START_CONFIRM_S)
        else:
            return
        self.pending_start = None
        self.store.confirm_command(p["id"], ok)
        self.bus.publish("command_check", {"command": p["command"], "via": p["via"], "ok": ok})

    def check_start(self) -> None:
        """Levanta CommandBlocked se o robô não pode sair da base agora (antes de mudar ajustes para uma limpeza)."""
        self._check_start("start")

    def run(self, name: str, source: str = "ui"):
        """Ação simples pelo nome (start, home, dust, locate…)."""
        if name not in catalog.SIMPLE_ACTIONS:
            raise ValueError(f"ação desconhecida: {name}")
        self._check_start(name)
        return self._command(f"run:{name}", None, self.dev.run, name, source=source,
                             leaves_base=name in catalog.LEAVES_BASE)

    def clean_rooms(self, ids: list[int], source: str = "ui"):
        self._check_start("start")
        return self._command("action:2/16", ids, self.dev.clean_rooms, ids, source=source, leaves_base=True)

    def clean_zones(self, zones: list, source: str = "ui"):
        self._check_start("start")
        return self._command("action:2/37", zones, self.dev.clean_zones, zones, source=source, leaves_base=True)

    def set(self, key: str, value, source: str = "ui"):
        """Grava uma prop e relê para confirmar (o robô pode ajustar o valor).

        O robô às vezes devolve o valor ANTIGO na releitura imediata (observado em 12/10): se o set
        foi aceito (code 0) e a releitura não confere, vale o valor gravado até o poller confirmar.
        """
        res = self._command(f"set:{key}", value, self.dev.set, key, value, source=source, reread=[key])
        if isinstance(res, dict) and res.get("code") == 0 and self.cache.get(key) != res.get("value"):
            changes = self.cache.update({key: res["value"]})
            self.store.save_changes(changes)
            self.bus.publish("state", {"changes": {key: res["value"]}})
        return res

    def action(self, key: str, params: list | None = None, source: str = "ui"):
        return self._command(f"action:{key}", params, self.dev.action, key, params, source=source)

    def reset_consumable(self, item: str, source: str = "ui"):
        keys = list(catalog.CONSUMABLE_PROPS[item])
        return self._command(f"reset:{item}", None, self.dev.reset_consumable, item, source=source, reread=keys)

    def raw_get(self, keys: list[str]):
        return self._call(self.dev.get, keys, high=True)
