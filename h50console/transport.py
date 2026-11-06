"""Transportes para falar com o robô, todos com o mesmo contrato: `send(método, parâmetros, retry)`.

- LAN:   miio.MiioClient (UDP 54321, token do aparelho). Resposta máxima ~1,4 KB.
- Nuvem: CloudClient, a mesma API que o app oficial usa fora de casa (/miotspec/*). Sem limite de tamanho
         (o 10/3 completo vem inteiro), mas mais lenta (~1–2,5 s) e dependente da conta Xiaomi.
- Auto:  AutoClient tenta a LAN e cai para a nuvem quando a LAN não responde (fora de casa, IP trocado).

Os resultados têm o formato da LAN, então Gateway/H50/Poller não sabem qual transporte está em uso.
`mode` ("lan"|"cloud") diz qual foi usado por último; o poller relaxa os intervalos na nuvem.
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager

from .miio import MiioClient, MiioError

log = logging.getLogger(__name__)

# Medido em 12/10/2026 (ov42gl): em tempo real (datasource 2) a nuvem responde até 40 propriedades por pedido
# (~2,8 KB); acima disso o robô não responde a tempo (códigos -704220043/-704083036). O cache (datasource 1)
# devolve tudo de uma vez (149 de 165 props em 2 s) e é o único caminho do 10/3 completo.
CLOUD_BATCH_BYTES = 2800
CLOUD_BATCH_ITEMS = 40
_OK_OR_MISSING = {0, -704040002, -704040003}   # ok; propriedade inexistente / sem acesso de leitura
LAN_RETRY_AFTER = 60.0             # depois de a LAN falhar, só tenta de novo após este tempo
# O UDP perde uma resposta de vez em quando (visto em 26/10 no H50 Pro, ao lado do roteador: uma a cada poucos
# minutos, em pedidos que respondem em ~10 ms). Leitura: duas tentativas de 2 s (o tempo de uma de 4 s) antes
# de ir para a nuvem. Comando do usuário: dentro da janela da nuvem, sonda a LAN antes (a nuvem já aceitou
# comando com code 0 que não chegou ao robô).
LAN_READ_TIMEOUT = 2.0
LAN_PROBE_TIMEOUT = 1.5


class CloudClient:
    """Fala com o robô pela nuvem Xiaomi (leitura em tempo real: datasource 2)."""

    lan = False
    mode = "cloud"
    oversize_ok = True
    max_batch_bytes = CLOUD_BATCH_BYTES
    max_batch_items = CLOUD_BATCH_ITEMS

    def __init__(self, service, did, model: str = "", fresh: bool = True):
        self.service = service          # cloud.CloudService (sessão, país, chamadas assinadas)
        self.did = str(did)
        self.model = model
        self.fresh = fresh

    def _call(self, path: str, data: dict) -> dict:
        from .cloud import CloudError
        try:
            res = self.service.api(path, data, did=self.did)
        except (CloudError, OSError) as ex:              # OSError: sem internet (DNS, conexão recusada, timeout)
            raise MiioError(f"nuvem: {ex}") from ex
        if res.get("code") not in (0, None):
            raise MiioError(f"nuvem code {res.get('code')}: {res.get('message', '')}")
        return res

    def _get(self, params: list, datasource: int) -> list[dict]:
        req = [{"did": self.did, "siid": p["siid"], "piid": p["piid"]} for p in params]
        res = self._call("/miotspec/prop/get", {"params": req, "datasource": datasource})
        return [{"did": self.did, "siid": i["siid"], "piid": i["piid"], "code": i.get("code", 0),
                 **({"value": i["value"]} if "value" in i else {})} for i in res.get("result") or []]

    def _get_properties(self, params: list) -> list[dict]:
        from . import catalog
        key = lambda p: f"{p['siid']}/{p['piid']}"           # noqa: E731
        big = [p for p in params if key(p) in catalog.OVERSIZED]    # 10/3: só o cache devolve inteiro
        rest = [p for p in params if key(p) not in catalog.OVERSIZED]
        out: list[dict] = []
        if rest:
            out = self._get(rest, 2 if self.fresh else 1)
            bad = [i for i in out if i["code"] not in _OK_OR_MISSING]      # o robô não respondeu a tempo
            if bad and self.fresh:
                cached = {(i["siid"], i["piid"]): i for i in self._get(bad, 1)}
                out = [cached.get((i["siid"], i["piid"]), i) if i in bad else i for i in out]
        if big:
            out += self._get(big, 1)
        return out

    def send(self, method, params=None, retry=1):
        if method == "get_properties":
            return self._get_properties(params or [])
        if method == "set_properties":
            req = [{"did": self.did, "siid": p["siid"], "piid": p["piid"], "value": p["value"]} for p in params or []]
            res = self._call("/miotspec/prop/set", {"params": req})
            return [{"did": self.did, "siid": i["siid"], "piid": i["piid"], "code": i.get("code", 0)}
                    for i in res.get("result") or []]
        if method == "action":
            a = params or {}
            res = self._call("/miotspec/action", {"params": {"did": self.did, "siid": a["siid"], "aiid": a["aiid"],
                                                              "in": a.get("in", [])}})
            r = res.get("result") or {}
            return {"code": r.get("code", 0), "out": r.get("out", [])}
        if method == "miIO.info":
            return {"model": self.model, "via": "cloud"}
        raise MiioError(f"a nuvem não suporta o método {method}")


class AutoClient:
    """LAN primeiro; se falhar e houver nuvem, usa a nuvem e volta à LAN quando ela responder de novo."""

    lan = True

    def __init__(self, lan: MiioClient, cloud: CloudClient | None, retry_after: float = LAN_RETRY_AFTER):
        self.lan_client = lan
        self.cloud_client = cloud
        self.retry_after = retry_after
        self.mode = "lan"
        self._lan_down_until = 0.0
        self._lock = threading.Lock()

    # atributos que o resto do app consulta no cliente LAN
    @property
    def ip(self):
        return self.lan_client.ip

    @property
    def expected_did(self):
        return self.lan_client.expected_did

    @property
    def oversize_ok(self) -> bool:
        return self.mode == "cloud"

    @property
    def max_batch_bytes(self):
        return CLOUD_BATCH_BYTES if self.mode == "cloud" else None

    @property
    def max_batch_items(self):
        return CLOUD_BATCH_ITEMS if self.mode == "cloud" else None

    def _lan_usable(self) -> bool:
        if time.monotonic() < self._lan_down_until:
            return False
        if self._lan_down_until:                       # janela de espera acabou: sonda a LAN antes de voltar
            try:
                self.lan_client.handshake()
            except MiioError:
                self._lan_down_until = time.monotonic() + self.retry_after
                return False
            self._lan_down_until = 0.0
            log.info("LAN voltou a responder; saindo do modo nuvem")
        return True

    @contextmanager
    def _lan_timeout(self, seconds: float):
        """Encurta o timeout do cliente LAN só durante o bloco (as chamadas já vêm serializadas pelo Gateway)."""
        old = getattr(self.lan_client, "timeout", None)
        if old is None:
            yield
            return
        self.lan_client.timeout = min(old, seconds)
        try:
            yield
        finally:
            self.lan_client.timeout = old

    def wake_lan(self) -> None:
        """Antes de um comando do usuário: dentro da janela da nuvem, sonda a LAN agora (não espera a janela acabar)."""
        with self._lock:
            if self.cloud_client is None or not self._lan_down_until:
                return
            try:
                with self._lan_timeout(LAN_PROBE_TIMEOUT):
                    self.lan_client.handshake(_rediscover=False)
            except MiioError:
                log.info("comando: LAN ainda sem resposta; vai pela nuvem")
                return
            self._lan_down_until = 0.0
            self.mode = "lan"
            log.info("LAN voltou a responder (sondada antes de um comando); saindo do modo nuvem")

    def prepare(self) -> None:
        """Decide o transporte ANTES de planejar os lotes (o limite de tamanho depende dele)."""
        with self._lock:
            if self.cloud_client is None:
                self.mode = "lan"
            elif self._lan_usable():
                self.mode = "lan"
            else:
                self.mode = "cloud"

    def send(self, method, params=None, retry=1):
        self.prepare()
        if self.mode == "lan":
            try:
                if method == "get_properties":                     # leitura: pode repetir sem efeito colateral
                    with self._lan_timeout(LAN_READ_TIMEOUT):
                        return self.lan_client.send(method, params, retry=max(retry, 1))
                return self.lan_client.send(method, params, retry=retry)
            except MiioError:
                if self.cloud_client is None:
                    raise
                self._lan_down_until = time.monotonic() + self.retry_after
                self.mode = "cloud"
                log.warning("LAN sem resposta; usando a nuvem por %.0f s", self.retry_after)
        if self.cloud_client is None:
            raise MiioError("sem transporte disponível")
        self.mode = "cloud"
        try:
            return self.cloud_client.send(method, params, retry)
        except MiioError as cloud_err:
            # sem internet a nuvem também falha: numa leitura (sem efeito colateral) tenta a LAN de novo antes de
            # desistir. Comandos não repetem aqui; o wake_lan já sondou a LAN antes deles.
            if method != "get_properties":
                raise
            try:
                with self._lan_timeout(LAN_READ_TIMEOUT):
                    res = self.lan_client.send(method, params, retry=retry)
            except MiioError:
                raise cloud_err from None
            with self._lock:
                self._lan_down_until = 0.0
                self.mode = "lan"
            log.info("nuvem sem resposta (%s), mas a LAN respondeu; saindo do modo nuvem", cloud_err)
            return res

    def handshake(self) -> None:
        self.lan_client.handshake()


def build_client(transport: str, lan: MiioClient | None, cloud: CloudClient | None):
    """transport: "lan" | "cloud" | "auto" (nuvem só se existir)."""
    if transport == "cloud" and cloud is not None:
        return cloud
    if transport == "auto" and cloud is not None and lan is not None:
        return AutoClient(lan, cloud)
    if lan is None:
        raise ValueError("robô sem IP/token para a LAN e sem nuvem")
    return lan


class OfflineClient:
    """Robô sem como ser alcançado (sem IP/token e sem nuvem): toda chamada falha com uma mensagem útil."""

    lan = False
    mode = "lan"
    oversize_ok = False
    max_batch_bytes = None
    max_batch_items = None

    def __init__(self, reason: str):
        self.reason = reason

    def send(self, method, params=None, retry=1):
        raise MiioError(self.reason)
