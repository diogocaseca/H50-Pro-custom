"""Transporte miIO (UDP 54321) sem dependências: handshake, pacote cifrado, send().

Formato (ver xiaomi-h50-pro-hacking.md §2.3):
    header 32 bytes = magic 0x2131 | length | unknown (0) | did | stamp | checksum
    checksum = MD5(header[:16] + token + payload)
    payload  = AES-128-CBC(json + b"\\0"), key = MD5(token), iv = MD5(key + token)

Diferenças em relação ao python-miio:
- buffer de recepção de 64 KB (o python-miio usa 4 KB; hipótese da pendência P6);
- descarta respostas atrasadas cujo id não é o da requisição;
- só refaz o handshake depois de uma falha (não a cada retry de 5 s).
"""

from __future__ import annotations

import hashlib
import json
import logging
import socket
import struct
import time

from . import aes

log = logging.getLogger(__name__)

PORT = 54321
MAGIC = 0x2131
HELLO = bytes.fromhex("21310020" + "ff" * 28)
RECV_BUF = 65535


class MiioError(Exception):
    """Falha de comunicação (timeout, rede)."""


class MiioTokenError(MiioError):
    """Checksum ou padding inválido: o token não confere (D7)."""


class MiioDeviceError(MiioError):
    """O aparelho respondeu com {"error": …}."""

    def __init__(self, error: dict):
        super().__init__(error)
        self.code = error.get("code") if isinstance(error, dict) else None


def _md5(data: bytes) -> bytes:
    return hashlib.md5(data).digest()  # noqa: S324 (exigido pelo protocolo)


def encrypt(payload: dict, token: bytes) -> bytes:
    key = _md5(token)
    return aes.cbc_encrypt(json.dumps(payload).encode() + b"\x00", key, _md5(key + token))


def decrypt(data: bytes, token: bytes) -> dict:
    key = _md5(token)
    try:
        plain = aes.cbc_decrypt(data, key, _md5(key + token))
    except ValueError as ex:
        raise MiioTokenError(str(ex)) from ex
    plain = plain.rstrip(b"\x00")
    try:
        text = plain.decode("utf-8")
    except UnicodeDecodeError:
        # o firmware manda algumas props (ex.: 23/1) em Latin-1: "crian\xe7a"
        text = plain.decode("latin-1")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # quirk conhecida de alguns firmwares (python-miio): vírgula dupla
        return json.loads(text.replace(",,", ","))


def build_packet(payload: dict, token: bytes, did: bytes, stamp: int) -> bytes:
    data = encrypt(payload, token)
    head = struct.pack(">HHI4sI", MAGIC, 32 + len(data), 0, did, stamp)
    return head + _md5(head + token + data) + data


def parse_packet(pkt: bytes, token: bytes) -> tuple[bytes, int, dict | None]:
    """Devolve (did, stamp, payload). Payload é None para respostas de hello."""
    if len(pkt) < 32:
        raise MiioError(f"pacote curto ({len(pkt)} bytes)")
    magic, length, _unk, did, stamp = struct.unpack(">HHI4sI", pkt[:16])
    if magic != MAGIC:
        raise MiioError(f"magic inválido 0x{magic:04x}")
    if length == 32:
        return did, stamp, None
    data = pkt[32:length]
    if _md5(pkt[:16] + token + data) != pkt[16:32]:
        raise MiioTokenError("checksum inválido: confira o H50_TOKEN")
    return did, stamp, decrypt(data, token)


def _broadcasts(near_ip: str | None = None) -> list[str]:
    """Endereços de broadcast a tentar. No Windows com placas virtuais (WSL/Hyper-V), o
    255.255.255.255 pode sair pela placa errada; o broadcast da sub-rede (/24) é confiável."""
    addrs = []
    if near_ip:
        addrs.append(near_ip.rsplit(".", 1)[0] + ".255")
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                addrs.append(ip.rsplit(".", 1)[0] + ".255")
    except OSError:
        pass
    addrs.append("255.255.255.255")
    return list(dict.fromkeys(addrs))


def discover(timeout: float = 3.0, near_ip: str | None = None) -> dict[int, str]:
    """Hello miIO em broadcast: devolve {did: ip} de quem responder na rede local."""
    found: dict[int, str] = {}
    broadcast = _broadcasts(near_ip)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.settimeout(0.5)
        for dst in broadcast:
            for _ in range(2):
                try:
                    s.sendto(HELLO, (dst, PORT))
                except OSError:
                    pass
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                data, (ip, _) = s.recvfrom(1024)
            except OSError:
                continue
            if len(data) >= 32 and data[:2] == MAGIC.to_bytes(2, "big"):
                found[int.from_bytes(data[8:12], "big")] = ip
    return found


class MiioClient:
    """Cliente síncrono. NÃO é thread-safe: quem serializa o acesso é o gateway."""

    # contrato comum dos transportes (ver transport.py)
    lan = True
    mode = "lan"
    oversize_ok = False          # respostas > ~1,4 KB não chegam pela LAN
    max_batch_bytes = None       # None = usa o orçamento padrão do H50 (~1,15 KB)
    max_batch_items = None

    def __init__(self, ip: str, token: str, timeout: float = 5.0, expected_did: int | None = None,
                 on_ip_change=None):
        if len(token) != 32:
            raise ValueError("o token deve ter 32 caracteres hexadecimais")
        self.ip = ip
        # se o IP configurado parar de responder (DHCP trocou), procura o aparelho por este DID
        self.expected_did = expected_did
        self.on_ip_change = on_ip_change
        self.token = bytes.fromhex(token)
        self.timeout = timeout
        self.did: bytes | None = None
        self._stamp0 = 0           # stamp do aparelho no último handshake/resposta
        self._t0 = 0.0             # time.monotonic() correspondente
        self._id = int(time.time()) % 9000

    # -- handshake ----------------------------------------------------------
    def handshake(self, _rediscover: bool = True) -> None:
        with self._socket() as s:
            s.sendto(HELLO, (self.ip, PORT))
            try:
                pkt, _ = s.recvfrom(RECV_BUF)
            except OSError as ex:
                if _rediscover and self.expected_did:
                    new_ip = discover(timeout=3, near_ip=self.ip).get(self.expected_did)
                    if new_ip and new_ip != self.ip:
                        log.warning("robô mudou de IP: %s -> %s (achado pelo DID %s)", self.ip, new_ip, self.expected_did)
                        self.ip = new_ip
                        if self.on_ip_change:
                            self.on_ip_change(new_ip)
                        return self.handshake(_rediscover=False)
                raise MiioError(f"sem resposta ao handshake de {self.ip}") from ex
        did, stamp, _ = parse_packet(pkt, self.token)
        if self.expected_did is None:
            self.expected_did = int.from_bytes(did, "big")   # aprende o DID no primeiro contato
        self.did = did
        self._sync_stamp(stamp)
        log.debug("handshake ok did=%s stamp=%s", did.hex(), stamp)

    def _sync_stamp(self, stamp: int) -> None:
        self._stamp0, self._t0 = stamp, time.monotonic()

    def _stamp(self) -> int:
        return self._stamp0 + int(time.monotonic() - self._t0) + 1

    def _next_id(self) -> int:
        self._id = self._id + 1 if self._id < 9998 else 1
        return self._id

    def _socket(self) -> socket.socket:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(self.timeout)
        return s

    # -- envio --------------------------------------------------------------
    def send(self, method: str, params=None, retry: int = 1):
        """Envia um comando e devolve `result`. `retry` = novas tentativas após timeout."""
        for attempt in range(retry + 1):
            if self.did is None:
                self.handshake()
            req_id = self._next_id()
            payload = {"id": req_id, "method": method, "params": params if params is not None else []}
            pkt = build_packet(payload, self.token, self.did, self._stamp())
            log.debug(">> %s", payload)
            try:
                resp = self._roundtrip(pkt, req_id)
            except MiioTokenError:
                raise
            except MiioError:
                self.did = None            # força novo handshake
                self._id += 100            # mesma estratégia do python-miio
                if attempt == retry:
                    raise
                log.debug("sem resposta ao id=%s (%s); nova tentativa", req_id, method)
                continue
            log.debug("<< %s", resp)
            if "error" in resp:
                raise MiioDeviceError(resp["error"])
            return resp.get("result", resp)
        raise MiioError("inalcançável")  # pragma: no cover

    def _roundtrip(self, pkt: bytes, req_id: int) -> dict:
        deadline = time.monotonic() + self.timeout
        with self._socket() as s:
            try:
                s.sendto(pkt, (self.ip, PORT))
            except OSError as ex:
                raise MiioError(f"falha ao enviar: {ex}") from ex
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise MiioError("timeout")
                s.settimeout(left)
                try:
                    data, _ = s.recvfrom(RECV_BUF)
                except OSError as ex:
                    raise MiioError("timeout") from ex
                _did, stamp, resp = parse_packet(data, self.token)
                if resp is None:
                    continue
                self._sync_stamp(stamp)
                if resp.get("id") != req_id:
                    log.debug("descartando resposta atrasada id=%s (esperado %s)", resp.get("id"), req_id)
                    continue
                return resp
