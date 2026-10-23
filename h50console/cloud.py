"""Módulo de nuvem EXPERIMENTAL (só biblioteca padrão): login Xiaomi e download do mapa.

O mapa do H50 Pro não vem pela LAN (10/1 é só o nome do objeto na nuvem). Este módulo:
  1. faz login na conta Xiaomi (a senha é pedida no terminal e NÃO é gravada);
  2. guarda só a sessão (userId, ssecurity, serviceToken) em data/cloud-session.json;
  3. pede a URL temporária do mapa pelo obj_name do 10/1 e baixa o arquivo.

Uso (no SEU terminal, para a senha não passar por outro lugar):
    py -m h50console.cloud login            # pede usuário e senha; avisa se precisar de 2FA
    py -m h50console.cloud devices          # confere em qual servidor (país) o robô está
    py -m h50console.cloud map              # baixa o mapa atual para data/map/

Protocolo: o mesmo usado pelo Xiaomi Cloud Map Extractor e micloud (RC4 + assinatura SHA-1).
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import hmac  # noqa: F401  (reservado para a assinatura legada)
import http.cookiejar
import json
import os
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

SESSION_FILE = Path("data/cloud-session.json")
COUNTRIES = ["us", "de", "sg", "cn", "ru", "i2"]   # "us" costuma ser o servidor das contas do Brasil
UA = ("Android-7.1.1-1.0.0-ONEPLUS A3010-136-%s APP/xiaomi.smarthome APPV/62830"
      % base64.b16encode(os.urandom(8)).decode())


class CloudError(Exception):
    pass


class TwoFactorRequired(CloudError):
    def __init__(self, url: str):
        super().__init__(f"verificação em duas etapas necessária: {url}")
        self.url = url


# ---------------------------------------------------------------------------
# RC4 (para o modo cifrado da API) — poucas linhas, sem dependência
# ---------------------------------------------------------------------------
class RC4:
    def __init__(self, key: bytes):
        s = list(range(256))
        j = 0
        for i in range(256):
            j = (j + s[i] + key[i % len(key)]) & 0xFF
            s[i], s[j] = s[j], s[i]
        self.s, self.i, self.j = s, 0, 0

    def crypt(self, data: bytes) -> bytes:
        s, i, j = self.s, self.i, self.j
        out = bytearray(len(data))
        for n, b in enumerate(data):
            i = (i + 1) & 0xFF
            j = (j + s[i]) & 0xFF
            s[i], s[j] = s[j], s[i]
            out[n] = b ^ s[(s[i] + s[j]) & 0xFF]
        self.i, self.j = i, j
        return bytes(out)


def _rc4(signed_nonce: str, data: bytes) -> bytes:
    r = RC4(base64.b64decode(signed_nonce))
    r.crypt(bytes(1024))  # descarta os primeiros 1024 bytes (RC4-drop1024)
    return r.crypt(data)


def _signed_nonce(ssecurity: str, nonce: str) -> str:
    return base64.b64encode(hashlib.sha256(base64.b64decode(ssecurity) + base64.b64decode(nonce)).digest()).decode()


def _nonce() -> str:
    return base64.b64encode(os.urandom(8) + int(time.time() / 60).to_bytes(4, "big")).decode()


def _enc_signature(path: str, signed_nonce: str, params: dict) -> str:
    parts = ["POST", path] + [f"{k}={v}" for k, v in params.items()] + [signed_nonce]
    return base64.b64encode(hashlib.sha1("&".join(parts).encode()).digest()).decode()


# ---------------------------------------------------------------------------
# Sessão
# ---------------------------------------------------------------------------
class Cloud:
    def __init__(self, session: dict | None = None, country: str = ""):
        self.s = session or {}
        self.forced_country = country   # H50_CLOUD_COUNTRY: usa só este servidor (vazio = descobre sozinho)
        self.jar = http.cookiejar.CookieJar()
        self.http = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    # -- persistência --------------------------------------------------------
    @classmethod
    def load(cls, path: Path = SESSION_FILE) -> "Cloud":
        if not path.exists():
            raise CloudError("sem sessão: rode `py -m h50console.cloud login`")
        return cls(json.loads(path.read_text(encoding="utf-8")))

    def save(self, path: Path = SESSION_FILE) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")        # tem ssecurity e serviceToken: só o dono lê, e a troca é atômica
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(self.s, indent=1))
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)

    # -- HTTP ------------------------------------------------------------------
    def _req(self, url: str, data: dict | None = None, headers: dict | None = None, cookies: dict | None = None):
        h = {"User-Agent": UA, "Accept-Encoding": "identity"}
        h.update(headers or {})
        if cookies:
            h["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        req = urllib.request.Request(url, data=body, headers=h, method="POST" if body is not None else "GET")
        try:
            with self.http.open(req, timeout=20) as r:
                return r.status, r.read(), r.headers
        except urllib.error.HTTPError as ex:
            with ex:
                return ex.code, ex.read(), ex.headers

    @staticmethod
    def _json(raw: bytes) -> dict:
        text = raw.decode("utf-8", "replace").replace("&&&START&&&", "", 1)
        return json.loads(text)

    # -- login ------------------------------------------------------------------
    def login(self, user: str, password: str) -> None:
        # 1. _sign
        _, raw, _ = self._req("https://account.xiaomi.com/pass/serviceLogin?sid=xiaomiio&_json=true",
                              cookies={"userId": user, "sdkVersion": "3.8.6", "deviceId": self._device_id()})
        sign = self._json(raw).get("_sign", "")
        # 2. credenciais (a senha vai só como MD5, como no app oficial)
        _, raw, _ = self._req("https://account.xiaomi.com/pass/serviceLoginAuth2", data={
            "sid": "xiaomiio", "hash": hashlib.md5(password.encode()).hexdigest().upper(),  # noqa: S324
            "callback": "https://sts.api.io.mi.com/sts", "qs": "%3Fsid%3Dxiaomiio%26_json%3Dtrue",
            "user": user, "_sign": sign, "_json": "true",
        }, cookies={"sdkVersion": "3.8.6", "deviceId": self._device_id()})
        d = self._json(raw)
        if d.get("notificationUrl"):
            raise TwoFactorRequired(d["notificationUrl"])
        if not d.get("ssecurity") or not d.get("location"):
            raise CloudError(f"login recusado (code {d.get('code')}): {d.get('desc') or d.get('description') or ''}")
        # 3. serviceToken
        _, _, _ = self._req(d["location"], headers={"Content-Type": "application/x-www-form-urlencoded"})
        token = next((c.value for c in self.jar if c.name == "serviceToken"), None)
        if not token:
            raise CloudError("login sem serviceToken")
        self.s.update({"user_id": str(d["userId"]), "ssecurity": d["ssecurity"], "service_token": token,
                       "login_at": int(time.time())})

    def _device_id(self) -> str:
        self.s.setdefault("device_id", base64.b16encode(os.urandom(8)).decode().lower())
        return self.s["device_id"]

    # -- login por QR code (contas Google/Apple/Facebook: sem senha Xiaomi) ----------
    def qr_start(self) -> dict:
        """Pede um QR de login. Devolve {"qr": url_da_imagem, "login_url": …, "lp": url_long_poll, "timeout": s}.

        O QR é escaneado no app Xiaomi Home (já logado) e confirmado lá; depois qr_wait() completa o login.
        """
        params = urllib.parse.urlencode({
            "_qrsize": "240", "qs": "%3Fsid%3Dxiaomiio%26_json%3Dtrue", "callback": "https://sts.api.io.mi.com/sts",
            "_hasLogo": "false", "sid": "xiaomiio", "serviceParam": "", "_locale": "pt_BR",
            "_dc": str(int(time.time() * 1000)),
        })
        status, raw, _ = self._req(f"https://account.xiaomi.com/longPolling/loginUrl?{params}",
                                   cookies={"sdkVersion": "3.8.6", "deviceId": self._device_id()})
        d = self._json(raw)
        if status != 200 or not d.get("lp"):
            raise CloudError(f"não foi possível gerar o QR (HTTP {status}, code {d.get('code')})")
        return {"qr": d.get("qr"), "login_url": d.get("loginUrl"), "lp": d["lp"], "timeout": d.get("timeout", 300)}

    def qr_wait(self, lp: str, timeout: float = 300) -> None:
        """Long polling até o QR ser confirmado no celular; completa a sessão (serviceToken)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                req = urllib.request.Request(lp, headers={"User-Agent": UA})
                with self.http.open(req, timeout=min(60, max(5, deadline - time.time()))) as r:
                    d = self._json(r.read())
            except (TimeoutError, urllib.error.URLError, OSError):
                continue  # o long polling expira sem resposta enquanto ninguém escaneia
            if d.get("code") not in (0, None) or not d.get("ssecurity"):
                if d.get("code") in (700, 701, 702):  # ainda esperando / escaneado sem confirmar
                    continue
                raise CloudError(f"QR recusado (code {d.get('code')}): {d.get('desc', '')}")
            self.s["user_id"] = str(d["userId"])
            self.s["ssecurity"] = d["ssecurity"]
            self._req(d["location"], headers={"Content-Type": "application/x-www-form-urlencoded"})
            token = next((c.value for c in self.jar if c.name == "serviceToken"), None)
            if not token:
                raise CloudError("QR confirmado, mas sem serviceToken")
            self.s.update({"service_token": token, "login_at": int(time.time()), "login_by": "qr"})
            return
        raise CloudError("QR expirou sem confirmação; gere outro")

    # -- API ---------------------------------------------------------------------
    def api(self, path: str, data: dict, country: str | None = None) -> dict:
        """POST cifrado (RC4) em https://<país>.api.io.mi.com/app<path>. Devolve o JSON decifrado."""
        country = country or self.forced_country or self.s.get("country", "us")
        host = "api.io.mi.com" if country == "cn" else f"{country}.api.io.mi.com"
        url = f"https://{host}/app{path}"
        nonce = _nonce()
        sn = _signed_nonce(self.s["ssecurity"], nonce)
        params = {"data": json.dumps(data, separators=(",", ":"))}
        params["rc4_hash__"] = _enc_signature(path, sn, params)
        params = {k: base64.b64encode(_rc4(sn, v.encode())).decode() for k, v in params.items()}
        params["signature"] = _enc_signature(path, sn, params)
        params["ssecurity"] = self.s["ssecurity"]
        params["_nonce"] = nonce
        status, raw, _ = self._req(url, data=params, headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "x-xiaomi-protocal-flag-cli": "PROTOCAL-HTTP2", "MIOT-ENCRYPT-ALGORITHM": "ENCRYPT-RC4",
        }, cookies={"userId": self.s["user_id"], "yetAnotherServiceToken": self.s["service_token"],
                    "serviceToken": self.s["service_token"], "locale": "pt_BR", "timezone": "GMT-03:00",
                    "is_daylight": "0", "dst_offset": "0", "channel": "MI_APP_STORE"})
        if status == 401:
            raise CloudError("sessão expirada: rode `py -m h50console.cloud login` de novo")
        if status != 200:
            raise CloudError(f"HTTP {status} em {path}: {raw[:200]!r}")
        try:
            return json.loads(_rc4(sn, base64.b64decode(raw)).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return json.loads(raw)  # algumas respostas de erro vêm em claro

    def find_device(self, did: str) -> dict:
        """Procura o robô nos servidores; grava o país encontrado na sessão."""
        for c in self.countries():
            try:
                res = self.api("/home/device_list", {"getVirtualModel": False, "getHuamiDevices": 0}, c)
            except CloudError:
                continue
            for dev in (res.get("result") or {}).get("list") or []:
                if str(dev.get("did")) == str(did):
                    self.s["country"] = c
                    return dev
        raise CloudError(f"robô did={did} não encontrado em {self.countries()}")

    def countries(self) -> list[str]:
        """Servidores a consultar, em ordem: só o fixo, se houver; senão o da sessão e depois os demais."""
        if self.forced_country:
            return [self.forced_country]
        return list(dict.fromkeys(([self.s["country"]] if self.s.get("country") else []) + COUNTRIES))

    def map_url(self, obj_name: str) -> str:
        """URL temporária do arquivo do mapa. Tenta a rota _pro (modelos novos) e a clássica."""
        errors = []
        for path in ("/v2/home/get_interim_file_url_pro", "/v2/home/get_interim_file_url"):
            res = self.api(path, {"obj_name": obj_name})
            url = (res.get("result") or {}).get("url")
            if url:
                return url
            errors.append(f"{path}: code {res.get('code')} {res.get('message', '')}")
        raise CloudError("sem URL do mapa: " + " | ".join(errors))

    def download(self, url: str) -> bytes:
        status, raw, _ = self._req(url)
        if status != 200:
            raise CloudError(f"download do mapa: HTTP {status}")
        return raw


# ---------------------------------------------------------------------------
# Serviço para o app web: login por QR em segundo plano + download do mapa
# ---------------------------------------------------------------------------
class CloudService:
    def __init__(self, session_file: Path = SESSION_FILE, map_dir: Path = Path("data/map"), country: str = ""):
        self.session_file = session_file
        self.map_dir = map_dir
        self.country = country                   # H50_CLOUD_COUNTRY (vazio = descobre o servidor sozinho)
        self.lock = threading.Lock()
        self.state = "ok" if session_file.exists() else "none"   # none | waiting | ok | error
        self.error: str | None = None
        self.qr: dict | None = None
        self._last_fetch: dict[str, float] = {}  # por did: evita martelar a nuvem
        self._c: Cloud | None = None             # sessão carregada (assinatura, país)
        self._api_lock = threading.RLock()

    # -- chamadas assinadas e dispositivos da conta -------------------------------------------
    def _cloud(self) -> "Cloud":
        if self._c is None:
            self._c = Cloud.load(self.session_file)
            self._c.forced_country = self.country
        return self._c

    def api(self, path: str, data: dict, did: str | None = None) -> dict:
        """Chamada assinada à API da Xiaomi. Descobre o servidor (país) pelo did na primeira vez."""
        with self._api_lock:
            c = self._cloud()
            if not c.s.get("country") and did:
                c.find_device(did)
                c.save(self.session_file)
            try:
                return c.api(path, data)
            except CloudError as ex:
                if "expirada" in str(ex):
                    self.state = "expired"          # a UI pede novo login por QR
                raise

    def _device_list(self) -> list[dict]:
        """Lista CRUA (traz token, localização e IP público: nunca devolver ao navegador como está)."""
        with self._api_lock:
            c = self._cloud()
            for country in c.countries():
                try:
                    res = c.api("/home/device_list", {"getVirtualModel": False, "getHuamiDevices": 0}, country)
                except CloudError:
                    continue
                lst = (res.get("result") or {}).get("list") or []
                if lst:
                    if c.s.get("country") != country:
                        c.s["country"] = country
                        c.save(self.session_file)
                    return lst
            return []

    def devices(self) -> list[dict]:
        """Dispositivos da conta SÓ com o necessário (sem token, coordenadas nem IP público)."""
        return [{"did": str(d.get("did")), "name": d.get("name"), "model": d.get("model"),
                 "online": d.get("isOnline"), "localip": d.get("localip"), "has_token": bool(d.get("token"))}
                for d in self._device_list()]

    def credentials(self, did: str) -> dict:
        """Token e IP de um dispositivo, para uso interno (cadastro do robô). Não expor em respostas da API."""
        for d in self._device_list():
            if str(d.get("did")) == str(did):
                return {"did": str(did), "name": d.get("name"), "model": d.get("model"),
                        "token": d.get("token"), "ip": d.get("localip")}
        raise CloudError(f"dispositivo {did} não está na conta")

    def reload(self) -> None:
        """Relê a sessão do disco (depois de restaurar um backup)."""
        self._c = None
        self.state = "ok" if self.session_file.exists() else "none"

    def status(self) -> dict:
        info = {"state": self.state, "error": self.error, "qr": (self.qr or {}).get("qr"),
                "login_url": (self.qr or {}).get("login_url")}
        if self.session_file.exists():
            s = json.loads(self.session_file.read_text(encoding="utf-8"))
            info.update({"user_id": s.get("user_id"), "country": self.country or s.get("country"), "login_at": s.get("login_at")})
        return info

    def start_qr(self, on_done=None) -> dict:
        with self.lock:
            cloud = Cloud()
            self.qr = cloud.qr_start()
            self.state, self.error = "waiting", None

        def wait():
            try:
                cloud.qr_wait(self.qr["lp"], self.qr.get("timeout", 300))
                cloud.save(self.session_file)
                self._c = None                    # recarrega a sessão nova
                self.state = "ok"
            except Exception as ex:  # noqa: BLE001
                self.state, self.error = "error", str(ex)
            finally:
                self.qr = None
                if on_done:
                    on_done(self.status())

        threading.Thread(target=wait, name="cloud-qr", daemon=True).start()
        return self.status()

    def logout(self) -> dict:
        if self.session_file.exists():
            self.session_file.unlink()
        self._c = None
        self.state, self.error, self.qr = "none", None, None
        return self.status()

    def last_fetch(self, did: str) -> float:
        return self._last_fetch.get(str(did), 0.0)

    def fetch_map(self, obj_name: str, did: str, map_dir: Path | None = None, model: str = "") -> dict:
        """Baixa o mapa do obj_name (10/1) e grava em `map_dir` (por robô). Devolve metadados do arquivo."""
        with self._api_lock:
            cloud = self._cloud()
            if not cloud.s.get("country"):
                cloud.find_device(did)
                cloud.save(self.session_file)
            raw = cloud.download(cloud.map_url(obj_name))
        out = Path(map_dir) if map_dir else self.map_dir
        out.mkdir(parents=True, exist_ok=True)
        (out / "latest.bin").write_bytes(raw)
        from . import mapdecode
        from .catalog import MODEL
        m = mapdecode.decrypt(raw, model or MODEL, did)
        ui = mapdecode.for_ui(m)
        ui["fetched_at"] = int(time.time())
        (out / "latest.json").write_text(json.dumps(ui, ensure_ascii=False), encoding="utf-8")
        self._last_fetch[str(did)] = time.time()
        return {"bytes": len(raw), "width": ui["width"], "height": ui["height"], "rooms": len(ui["rooms"]),
                "walls": len(ui["walls"]), "furniture": len(ui["furniture"]), "path_points": len(ui["path"])}

    def fetch_object(self, obj_name: str, did: str) -> bytes:
        """Baixa um objeto do armazenamento do robô na nuvem (mapa, backup de mapa, trajetória...)."""
        with self._api_lock:
            cloud = self._cloud()
            if not cloud.s.get("country"):
                cloud.find_device(did)
                cloud.save(self.session_file)
            return cloud.download(cloud.map_url(obj_name))

    def clean_record(self, did) -> str:
        """10/3 (clean-record) pela nuvem: pela LAN ele passa do limite de ~1,4 KB do UDP do robô."""
        res = self.api("/miotspec/prop/get", {"params": [{"did": str(did), "siid": 10, "piid": 3}], "datasource": 1},
                       did=str(did))
        items = res.get("result") or []
        if res.get("code") != 0 or not items or items[0].get("code") != 0 or not items[0].get("value"):
            raise CloudError(f"nuvem sem o 10/3 (code {res.get('code')} {res.get('message', '')})")
        return items[0]["value"]

    def latest_map(self, map_dir: Path | None = None) -> dict | None:
        f = (Path(map_dir) if map_dir else self.map_dir) / "latest.json"
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _obj_name_from_robot() -> tuple[str, str]:
    """Lê 10/1 (e o did) pela LAN. Devolve (did, obj_name)."""
    from .config import Config
    from .device import H50
    from .miio import MiioClient
    cfg = Config.from_env()
    dev = H50(MiioClient(cfg.ip, cfg.token, cfg.timeout))
    v = dev.values(["10/1", "10/5"])
    obj = json.loads(v["10/1"])["obj_name"]
    return obj.split("/")[1], obj


def main(argv=None) -> int:
    for st in (sys.stdout, sys.stderr):
        if hasattr(st, "reconfigure"):
            st.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Nuvem Xiaomi (experimental): login e mapa")
    ap.add_argument("cmd", choices=["login", "qr", "devices", "map"])
    ap.add_argument("--obj", help="obj_name explícito (padrão: o do 10/1)")
    args = ap.parse_args(argv)

    if args.cmd == "qr":
        cloud = Cloud()
        q = cloud.qr_start()
        print("Abra o QR no navegador e escaneie com o app Xiaomi Home (logado na sua conta):")
        print("   " + str(q["qr"]))
        print("Aguardando confirmação no celular…")
        cloud.qr_wait(q["lp"], q["timeout"])
        cloud.save()
        print(f"Login ok (userId {cloud.s['user_id']}). Sessão salva em {SESSION_FILE}.")
        return 0

    if args.cmd == "login":
        user = input("Conta Xiaomi (e-mail, telefone ou ID): ").strip()
        pwd = getpass.getpass("Senha (não aparece na tela e não é gravada): ")
        cloud = Cloud()
        try:
            cloud.login(user, pwd)
        except TwoFactorRequired as ex:
            print("\nA Xiaomi pediu verificação em duas etapas.\n"
                  "1. Abra este link no navegador e conclua a verificação:\n   " + ex.url +
                  "\n2. Depois rode `py -m h50console.cloud login` de novo.")
            return 3
        cloud.save()
        print(f"Login ok (userId {cloud.s['user_id']}). Sessão salva em {SESSION_FILE} (sem a senha).")
        return 0

    cloud = Cloud.load()
    did, obj = _obj_name_from_robot()
    if args.cmd == "devices":
        dev = cloud.find_device(did)
        cloud.save()
        print(f"Robô encontrado no servidor '{cloud.s['country']}': {dev.get('name')} ({dev.get('model')})")
        return 0

    if not cloud.s.get("country"):
        cloud.find_device(did)
        cloud.save()
    obj = args.obj or obj
    url = cloud.map_url(obj)
    raw = cloud.download(url)
    out = Path("data/map")
    out.mkdir(parents=True, exist_ok=True)
    f = out / f"raw-{obj.replace('/', '_')}-{int(time.time())}.bin"
    f.write_bytes(raw)
    print(f"Mapa baixado: {f} ({len(raw)} bytes). Início: {raw[:60]!r}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except CloudError as ex:
        print(f"Erro: {ex}", file=sys.stderr)
        sys.exit(1)
