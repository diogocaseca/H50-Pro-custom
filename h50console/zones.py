"""Limpeza por zona: zonas com nome (salvas no painel) e zonas aprendidas das rotinas do app Xiaomi Home.

A ação 2/37 (start-zone-sweep) recebe as zonas (ver decoders.zone_param). A lista de rotinas (2/42) não traz as zonas
de uma rotina por zonas: o robô só as informa no 2/40 enquanto a rotina roda. O painel guarda essa cópia (aprendida)
por rotina, em app_setting, porque o prop_change é apagado pela retenção.
"""

from __future__ import annotations

import secrets

from . import catalog, decoders
from .store import Store, iso

SAVED_KEY = "zones_saved"            # [{"id", "name", "zones": [[x1,y1,x2,y2], ...]}]
LEARNED_KEY = "routine_zones"        # {"<id da rotina>": {"name", "zones", "ts"}}
MAX_SAVED = 30
MAX_NAME = 40


class ZoneError(ValueError):
    """Pedido inválido (vira 400) ou conflito de nome (vira 409, ver `conflict`)."""

    def __init__(self, message: str, conflict: bool = False):
        super().__init__(message)
        self.conflict = conflict


# -- aprendidas das rotinas ------------------------------------------------------------------------------------------
def learn(store: Store, raw_2_40) -> bool:
    """2/40 de uma rotina por zonas em andamento -> guarda as zonas da rotina. True se mudou algo."""
    custom = decoders.running_custom(raw_2_40)
    if not custom or not custom.get("zones") or custom.get("id") is None:
        return False
    learned = store.get_setting(LEARNED_KEY) or {}
    key = str(custom["id"])
    zones = [list(z) for z in custom["zones"]]
    if (learned.get(key) or {}).get("zones") == zones:
        return False
    learned[key] = {"name": custom.get("name", ""), "zones": zones, "ts": iso()}
    store.set_setting(LEARNED_KEY, learned)
    return True


def learned(store: Store) -> dict[str, dict]:
    """Zonas aprendidas por rotina. Na primeira vez, importa o que já estiver no histórico de mudanças do 2/40."""
    data = store.get_setting(LEARNED_KEY)
    if data is None:
        data = {}
        for row in reversed(store.prop_history("2/40", 5000)):            # do mais antigo ao mais novo
            custom = decoders.running_custom(row["new_value"])
            if custom and custom.get("zones") and custom.get("id") is not None:
                data[str(custom["id"])] = {"name": custom.get("name", ""), "zones": [list(z) for z in custom["zones"]],
                                           "ts": row["ts"]}
        store.set_setting(LEARNED_KEY, data)
    return data


def routine_plan(routine: dict, learned_zones: dict[str, dict]) -> dict:
    """Como o painel roda uma rotina: {"runnable", "reason", "mode", "rooms", "zones"}.

    O campo "mop" do 2/42 lista os modos (2/4) que a rotina usa; com mais de um, não dá para saber qual cômodo usa qual.
    Rotina sem cômodos é por zonas: só roda depois que o painel aprendeu as zonas dela.
    """
    plan = {"runnable": False, "reason": None, "mode": None, "rooms": list(routine["room_ids"]), "zones": None}
    modes = sorted(set(routine["mop"]))
    if len(modes) > 1:
        plan["reason"] = "modos diferentes por cômodo: use o app Xiaomi Home"
        return plan
    if modes and modes[0] not in catalog.ENUM_PT["2/4"]:
        plan["reason"] = f"modo {modes[0]} desconhecido: use o app Xiaomi Home"
        return plan
    plan["mode"] = modes[0] if modes else None
    if not plan["rooms"]:
        known = learned_zones.get(str(routine["id"]))
        if not known:
            plan["reason"] = "rode uma vez pelo app Xiaomi Home para o painel aprender as zonas"
            return plan
        plan["zones"] = known["zones"]
    plan["runnable"] = True
    return plan


# -- zonas com nome --------------------------------------------------------------------------------------------------
def saved(store: Store) -> list[dict]:
    return store.get_setting(SAVED_KEY) or []


def _norm(name: str) -> str:
    return " ".join(name.split()).casefold()


def check_name(name, store: Store, routine_names: list[str], ignore_id: str | None = None) -> str:
    """Nome limpo, ou ZoneError: vazio, longo demais, igual ao de outra zona ou ao de uma rotina do app."""
    if not isinstance(name, str) or not name.strip():
        raise ZoneError("informe um nome para a zona")
    name = " ".join(name.split())
    if len(name) > MAX_NAME:
        raise ZoneError(f"nome com mais de {MAX_NAME} caracteres")
    if any(_norm(r) == _norm(name) for r in routine_names):
        raise ZoneError(f'já existe uma rotina chamada "{name}"', conflict=True)
    if any(_norm(z["name"]) == _norm(name) and z["id"] != ignore_id for z in saved(store)):
        raise ZoneError(f'já existe uma zona chamada "{name}"', conflict=True)
    return name


def add(store: Store, name, rects, routine_names: list[str]) -> dict:
    name = check_name(name, store, routine_names)
    decoders.zone_param(rects)                                       # valida (ValueError -> 400)
    zones = saved(store)
    if len(zones) >= MAX_SAVED:
        raise ZoneError(f"limite de {MAX_SAVED} zonas salvas")
    zone = {"id": secrets.token_hex(4), "name": name, "zones": [[round(v) for v in r] for r in rects]}
    store.set_setting(SAVED_KEY, zones + [zone])
    return zone


def rename(store: Store, zid: str, name, routine_names: list[str]) -> dict:
    zones = saved(store)
    zone = next((z for z in zones if z["id"] == zid), None)
    if zone is None:
        raise KeyError(zid)
    zone["name"] = check_name(name, store, routine_names, ignore_id=zid)
    store.set_setting(SAVED_KEY, zones)
    return zone


def delete(store: Store, zid: str) -> None:
    zones = saved(store)
    if not any(z["id"] == zid for z in zones):
        raise KeyError(zid)
    store.set_setting(SAVED_KEY, [z for z in zones if z["id"] != zid])
