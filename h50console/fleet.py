"""Frota de robôs: configuração persistente (robots.json) e o contexto em execução de cada robô.

Cada `Robot` tem o que é só dele: banco SQLite, barramento de eventos, cliente (LAN/nuvem/auto), gateway e
poller. A conta Xiaomi (`CloudService`) é compartilhada. Tokens ficam só em robots.json (arquivo 0600 quando o
sistema permite) e nunca saem pela API (`RobotConfig.public()` não os inclui).
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import unicodedata
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from . import catalog, history, paths
from .device import H50
from .events import EventBus
from .gateway import Gateway
from .miio import MiioClient
from .poller import Poller
from .store import Store
from .transport import CloudClient, OfflineClient, build_client

log = logging.getLogger(__name__)

TRANSPORTS = ("auto", "lan", "cloud")
SUPPORTED_MODELS = {catalog.MODEL}          # modelos com spec e rótulos verificados


def slugify(text: str) -> str:
    s = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-") or "robo"


@dataclass
class RobotConfig:
    id: str
    name: str
    ip: str = ""
    token: str = ""
    did: str = ""
    model: str = catalog.MODEL
    transport: str = "auto"      # auto = LAN, com nuvem de reserva
    db: str = ""                 # relativo a data_dir; vazio = robots/<id>/h50.db
    map_dir: str = ""            # idem; vazio = robots/<id>/map
    created: int = 0

    def public(self) -> dict:
        """Sem o token (que nunca vai para o navegador)."""
        d = asdict(self)
        d.pop("token")
        d["has_token"] = bool(self.token)
        d["supported"] = self.model in SUPPORTED_MODELS
        return d


class Settings:
    """Preferências do app que não são segredo (retenção do histórico etc.) em settings.json."""

    DEFAULTS = {"retention_days": 90}

    def __init__(self, path: Path):
        self.path = path
        try:
            self.data = {**self.DEFAULTS, **json.loads(path.read_text(encoding="utf-8"))}
        except (OSError, ValueError):
            self.data = dict(self.DEFAULTS)

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value) -> None:
        self.data[key] = value
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
        tmp.replace(self.path)


class Robot:
    """Um robô em execução."""

    def __init__(self, cfg: RobotConfig, fleet: "Fleet"):
        self.cfg = cfg
        self.fleet = fleet
        d = fleet.data_dir
        self.db_path = d / (cfg.db or f"robots/{cfg.id}/h50.db")
        self.map_dir = d / (cfg.map_dir or f"robots/{cfg.id}/map")
        self.store = Store(self.db_path)
        self.bus = EventBus()
        self.client = self._make_client()
        self.gw = Gateway(H50(self.client), self.store, self.bus)
        self.poller = Poller(self.gw, self.store)
        self.poller.retention_days = lambda: fleet.settings.get("retention_days", 90)
        self.path_recorder = None
        self.app = None                       # lógica das rotas do robô (web.App), criada pelo hub
        self._stop = threading.Event()
        self._started = False

    # -- cliente ------------------------------------------------------------------------------
    def _make_client(self):
        cfg, fl = self.cfg, self.fleet
        if fl.client_factory:
            return fl.client_factory(cfg)
        lan = None
        if cfg.ip and cfg.token and cfg.transport != "cloud":
            lan = MiioClient(cfg.ip, cfg.token, fl.timeout, expected_did=int(cfg.did) if cfg.did else None,
                             on_ip_change=self._ip_changed)
        cloud = CloudClient(fl.cloud, cfg.did, cfg.model) if cfg.did and cfg.transport != "lan" else None
        try:
            return build_client(cfg.transport, lan, cloud)
        except ValueError:
            return OfflineClient("robô sem IP/token e sem acesso pela nuvem: edite o robô")

    def rebuild_client(self) -> None:
        """Aplica mudanças de IP/token/transporte sem reiniciar o poller."""
        self.client = self._make_client()
        self.gw.dev.client = self.client

    def _ip_changed(self, ip: str) -> None:
        self.cfg.ip = ip
        self.fleet.save()
        self.bus.publish("ip", {"ip": ip})
        log.warning("[%s] IP atualizado para %s (achado pelo DID)", self.cfg.id, ip)

    def rename(self, name: str) -> None:
        self.cfg.name = name
        self.fleet.save()

    # -- ciclo de vida --------------------------------------------------------------------------
    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self.poller.start()
        if self.fleet.start_threads:
            history.start_sync(self.bus, self.fleet.cloud, self.store, lambda: self.cfg.did or None, stop=self._stop)
            self.path_recorder = paths.PathRecorder(
                self.gw, self.store, paths.make_fetch(self.gw, self.fleet.cloud, lambda: self.cfg.did or None,
                                                      lambda: self.cfg.model, map_dir=self.map_dir))
            paths.start(self.bus, self.path_recorder, stop=self._stop)
            threading.Thread(target=self._learn_did, name=f"did-{self.cfg.id}", daemon=True).start()

    def _learn_did(self) -> None:
        """Aprende o DID no primeiro handshake (cadastro manual por IP + token) e liga a nuvem de reserva."""
        for _ in range(120):
            if self._stop.is_set() or self.cfg.did:
                return
            did = getattr(self.client, "expected_did", None)
            if did:
                self.cfg.did = str(did)
                self.fleet.save()
                self.rebuild_client()
                return
            self._stop.wait(5)

    def stop(self) -> None:
        self._stop.set()
        self.poller.stop()
        self.store.close()

    # -- resumo para a API (sem segredos) --------------------------------------------------------
    def summary(self) -> dict:
        c = self.gw.cache
        status = c.get("2/2")
        return {**self.cfg.public(), "online": self.gw.online, "via": self.gw.via, "last_ok": self.gw.last_ok,
                "status": status, "status_label": catalog.label("2/2", status) if status is not None else None,
                "battery": c.get("3/1"), "active": status in catalog.ACTIVE_STATUS}


class Fleet:
    def __init__(self, data_dir, cloud, *, timeout: float = 4.0, client_factory=None, start_threads: bool = True):
        self.data_dir = Path(data_dir)
        self.cloud = cloud
        self.timeout = timeout
        self.client_factory = client_factory      # testes: substitui o cliente real
        self.start_threads = start_threads
        self.robots: dict[str, Robot] = {}
        self.lock = threading.RLock()
        self.path = self.data_dir / "robots.json"
        self.settings = Settings(self.data_dir / "settings.json")

    # -- persistência ----------------------------------------------------------------------------
    def load(self, seed: dict | None = None) -> None:
        """Lê robots.json. Se não existir, migra uma instalação antiga (data/h50.db) ou usa `seed`
        ({ip, token, name} do ambiente). Sem nada disso, a frota começa vazia (a UI faz o cadastro)."""
        with self.lock:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            if self.path.exists():
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                known = {f.name for f in fields(RobotConfig)}
                for r in raw.get("robots", []):
                    self._add(RobotConfig(**{k: v for k, v in r.items() if k in known}))
                return
            legacy = self._legacy_config(seed or {})
            if legacy:
                self._add(legacy)
                self.save()

    def _legacy_config(self, seed: dict) -> RobotConfig | None:
        """Instalação anterior ao suporte a vários robôs: um banco em data/h50.db e IP/token no ambiente."""
        db = self.data_dir / "h50.db"
        ip, token, name, did = seed.get("ip", ""), seed.get("token", ""), seed.get("name", ""), ""
        if db.exists():
            st = Store(db)
            try:
                ip = st.get_setting("robot_ip") or ip
                did = str(st.get_setting("robot_did") or "")
                name = st.get_setting("device_name") or name
            finally:
                st.close()
        if not token:
            return None                           # sem token não há como falar com o robô: cadastro pela UI
        name = name or "H50 Pro"                  # o nome do app anterior; renomeie na interface
        return RobotConfig(id=slugify(name), name=name, ip=ip, token=token, did=did,
                           db="h50.db" if db.exists() else "", map_dir="map" if (self.data_dir / "map").exists() else "",
                           created=int(time.time()))

    def save(self) -> None:
        with self.lock:
            payload = {"version": 1, "robots": [asdict(r.cfg) for r in self.robots.values()]}
            self.data_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
            try:
                os.chmod(tmp, 0o600)              # tem tokens (no Windows o efeito é limitado)
            except OSError:
                pass
            tmp.replace(self.path)

    # -- operações --------------------------------------------------------------------------------
    def _add(self, cfg: RobotConfig) -> Robot:
        if cfg.transport not in TRANSPORTS:
            raise ValueError(f"transporte inválido: {cfg.transport}")
        robot = Robot(cfg, self)
        self.robots[cfg.id] = robot
        return robot

    def unique_id(self, name: str) -> str:
        base, rid, n = slugify(name), slugify(name), 2
        while rid in self.robots:
            rid, n = f"{base}-{n}", n + 1
        return rid

    def add(self, cfg: RobotConfig, start: bool = True) -> Robot:
        with self.lock:
            if cfg.did and any(r.cfg.did == cfg.did for r in self.robots.values()):
                raise ValueError("este robô já está cadastrado")
            cfg.id = cfg.id or self.unique_id(cfg.name)
            if cfg.id in self.robots:
                raise ValueError(f"id {cfg.id} já existe")
            cfg.created = cfg.created or int(time.time())
            robot = self._add(cfg)
            self.save()
        if start:
            robot.start()
        return robot

    def update(self, rid: str, **changes) -> Robot:
        """Atualiza nome, IP, token, transporte. Token vazio/ausente = mantém o atual."""
        with self.lock:
            robot = self.get(rid)
            cfg = robot.cfg
            if "transport" in changes and changes["transport"] not in TRANSPORTS:
                raise ValueError(f"transporte inválido: {changes['transport']}")
            for k in ("name", "ip", "transport", "model"):
                if changes.get(k) not in (None, ""):
                    setattr(cfg, k, str(changes[k]).strip())
            if changes.get("token"):
                token = str(changes["token"]).strip()
                if len(token) != 32:
                    raise ValueError("o token deve ter 32 caracteres hexadecimais")
                cfg.token = token
            self.save()
            robot.rebuild_client()
            return robot

    def remove(self, rid: str, delete_data: bool = False) -> None:
        with self.lock:
            robot = self.get(rid)
            robot.stop()
            del self.robots[rid]
            self.save()
            if delete_data:
                for p in (robot.db_path, Path(str(robot.db_path) + "-wal"), Path(str(robot.db_path) + "-shm")):
                    p.unlink(missing_ok=True)
                import shutil
                shutil.rmtree(robot.map_dir, ignore_errors=True)

    def get(self, rid: str) -> Robot:
        try:
            return self.robots[rid]
        except KeyError:
            raise KeyError(f"robô {rid} não existe") from None

    def default(self) -> Robot | None:
        return next(iter(self.robots.values()), None)

    def reload(self, start: bool = True) -> None:
        """Descarta tudo e recarrega do disco (depois de restaurar um backup)."""
        with self.lock:
            self.stop_all()
            self.robots.clear()
            self.settings = Settings(self.data_dir / "settings.json")
            self.load()
        if start:
            self.start_all()

    def start_all(self) -> None:
        for r in list(self.robots.values()):
            r.start()

    def stop_all(self) -> None:
        for r in list(self.robots.values()):
            r.stop()

    def summaries(self) -> list[dict]:
        return [r.summary() for r in self.robots.values()]
