"""py -m h50console [serve]            sobe a frota de robôs e o painel (http://127.0.0.1:8050)
py -m h50console backup ARQUIVO     gera um backup criptografado (senha em H50_BACKUP_PASSPHRASE ou perguntada)
py -m h50console restore ARQUIVO    restaura um backup (com o app PARADO)
py -m h50console reset-password     esqueci a senha do painel (com o app PARADO): o próximo início gera um código novo
py -m h50console mcp                servidor MCP (stdio) para um assistente de IA consultar e gerenciar o robô
py -m h50console version
"""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import sys
from pathlib import Path

from . import __version__, backup, maint
from .auth import Auth, is_loopback
from .cloud import CloudService
from .config import Config
from .fleet import Fleet
from .hub import Hub
from .notify import Notifier, NotifyStore
from .web import make_server


def _passphrase(confirm: bool = False) -> str:
    pw = os.environ.get("H50_BACKUP_PASSPHRASE") or getpass.getpass("Senha do backup: ")
    if confirm and not os.environ.get("H50_BACKUP_PASSPHRASE") and getpass.getpass("Repita a senha: ") != pw:
        raise SystemExit("As senhas não são iguais.")
    return pw


def _fleet(cfg: Config, start_threads: bool) -> Fleet:
    cloud = CloudService(cfg.data_dir / "cloud-session.json", cfg.data_dir / "map", country=cfg.cloud_country)
    fleet = Fleet(cfg.data_dir, cloud, timeout=cfg.timeout, start_threads=start_threads)
    fleet.load(seed=cfg.seed())
    return fleet


def serve(cfg: Config) -> int:
    ring = maint.RingLog()
    logging.getLogger().addHandler(ring)
    log = logging.getLogger("h50console")
    if not cfg.auth_enabled and not is_loopback(cfg.host):
        raise SystemExit(f"Recusado: H50_AUTH=off só é permitido em localhost (H50_HOST={cfg.host}). "
                         "Ative a senha (padrão) ou use H50_HOST=127.0.0.1.")
    auth = Auth(cfg.data_dir, enabled=cfg.auth_enabled, env_password=cfg.password, viewer_password=cfg.viewer_password)
    fleet = _fleet(cfg, start_threads=True)     # robots.json; migra instalação antiga ou H50_IP/H50_TOKEN na 1ª vez
    notifier = Notifier(NotifyStore(cfg.data_dir / "notify.json"))
    hub = Hub(fleet, fleet.cloud, cfg.raw_enabled, auth=auth, ring=ring, notifier=notifier)
    srv = make_server(hub, cfg.host, cfg.port, auth=auth, trust_proxy=cfg.trust_proxy, cookie_secure=cfg.cookie_secure,
                      extra_hosts=cfg.allowed_hosts, metrics_token=cfg.metrics_token)

    fleet.start_all()
    names = ", ".join(f"{r.cfg.name} ({r.cfg.transport})" for r in fleet.robots.values()) or "nenhum (cadastre na interface)"
    log.info("H50 Console %s | dados em %s | robôs: %s", __version__, cfg.data_dir, names)
    log.info("painel em http://%s:%s  (Ctrl+C para sair)", cfg.host, cfg.port)
    if cfg.cloud_country:
        log.info("nuvem Xiaomi: servidor fixo '%s' (H50_CLOUD_COUNTRY)", cfg.cloud_country)
    if auth.enabled and auth.setup_code:
        log.warning("PRIMEIRO ACESSO: abra o painel e crie a senha com o código  %s", auth.setup_code)
    elif not auth.enabled:
        log.warning("autenticação DESLIGADA (H50_AUTH=off): qualquer pessoa que alcance %s:%s controla o robô", cfg.host, cfg.port)
    try:
        srv.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        log.info("encerrando…")
        srv.server_close()
        notifier.stop()
        fleet.stop_all()
    return 0


def main(argv: list[str] | None = None) -> int:
    for st in (sys.stdout, sys.stderr):
        if hasattr(st, "reconfigure"):
            st.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    ap = argparse.ArgumentParser(prog="h50console", description="Painel local para robôs Xiaomi")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="sobe o painel (padrão)")
    b = sub.add_parser("backup", help="gera um backup criptografado")
    b.add_argument("file")
    b.add_argument("--sem-nuvem", action="store_true", help="não inclui a sessão da conta Xiaomi")
    r = sub.add_parser("restore", help="restaura um backup (com o app parado)")
    r.add_argument("file")
    sub.add_parser("reset-password", help="esqueci a senha do painel (com o app parado)")
    sub.add_parser("mcp", help="servidor MCP por stdin/stdout (H50_MCP_URL, H50_MCP_PASSWORD)")
    sub.add_parser("version")
    args = ap.parse_args(argv)
    if args.cmd == "mcp":                               # não carrega a config do painel: só precisa de URL e senha
        from . import mcp
        return mcp.main()
    cfg = Config.from_env()

    if args.cmd == "version":
        print(__version__)
        return 0
    if args.cmd == "reset-password":
        auth = Auth(cfg.data_dir, enabled=True, env_password=cfg.password)
        if cfg.password:
            raise SystemExit("A senha vem de H50_PASSWORD: troque a variável de ambiente.")
        auth.reset_password()
        print("Senha apagada e sessões encerradas. Ao iniciar o app, use o código de primeiro acesso que aparece no console.")
        return 0
    if args.cmd == "backup":
        passphrase = _passphrase(confirm=True)
        fleet = _fleet(cfg, start_threads=False)
        try:
            blob = backup.create(fleet, passphrase, include_cloud=not args.sem_nuvem, app_version=__version__)
        except backup.BackupError as ex:
            raise SystemExit(f"Falhou: {ex}")
        finally:
            fleet.stop_all()
        Path(args.file).write_bytes(blob)
        print(f"backup gravado em {args.file} ({len(blob) / 1024:.0f} KB)")
        return 0
    if args.cmd == "restore":
        try:                                           # lê e confere ANTES de abrir qualquer banco
            files = backup.read(Path(args.file).read_bytes(), _passphrase())
        except backup.BackupError as ex:
            raise SystemExit(f"Falhou: {ex}")
        fleet = _fleet(cfg, start_threads=False)
        try:
            res = backup.apply(fleet, files, start=False)
        finally:
            fleet.stop_all()
        print(f"restaurado: {len(res['written'])} arquivo(s); robôs: {', '.join(res['robots']) or 'nenhum'}")
        return 0
    return serve(cfg)


if __name__ == "__main__":
    sys.exit(main())
