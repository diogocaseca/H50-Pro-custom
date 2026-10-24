"""Decodificação das props com formato próprio (spec §6). Funções puras, sem I/O.

Todas aceitam o valor cru do robô (string JSON ou int) e devolvem estruturas Python.
String vazia ou JSON inválido -> resultado vazio (o robô devolve "" quando não há dado).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

WEEKDAYS = ["dom", "seg", "ter", "qua", "qui", "sex", "sáb"]


def parse_json(raw, default=None):
    """'{"a":1}' -> dict; '' / None / inválido -> default."""
    if isinstance(raw, (dict, list)):
        return raw
    if not raw or not isinstance(raw, str):
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


def ts(epoch: int | None) -> datetime | None:
    """Epoch (s, UTC) do robô -> datetime com fuso UTC. 0/None -> None."""
    return datetime.fromtimestamp(epoch, timezone.utc) if epoch else None


# -- cômodos ------------------------------------------------------------------
def rooms(raw) -> dict:
    """2/16 -> {"map_uid": 1, "rooms": [{"id": 3, "name": "Sala de estar"}, …]}"""
    d = parse_json(raw, {})
    return {"map_uid": d.get("map_uid"),
            "rooms": [{"id": int(r["id"]), "name": r.get("name", "")} for r in d.get("rooms", [])]}


def room_surfaces(raw) -> dict[int, int]:
    """23/1 (fora da spec) -> {id_cômodo: surface}. Valores de surface ainda não mapeados."""
    return {int(r["id"]): r.get("surface") for r in parse_json(raw, [])}


def custom_cleans(raw) -> list[dict]:
    """2/42 -> rotinas personalizadas do app."""
    out = []
    for u in parse_json(raw, {}).get("user_labels", []):
        out.append({"id": u["id"], "name": u.get("name", ""), "enabled": bool(u.get("en", 1)),
                    "room_ids": list(u.get("room_ids", [])), "mop": list(u.get("mop", [])),
                    "user_cfg": u.get("user_cfg", [])})
    return out


# -- agenda e não perturbe ------------------------------------------------------
def schedules(raw) -> list[dict]:
    """2/19 -> agendamentos.

    week: bit0 = domingo … bit6 = sábado, bit7 = repetir. time: hora×256 + min (hora local).
    """
    d = parse_json(raw, {})
    out = []
    for i, on, w, t, conf in zip(d.get("id", []), d.get("on", []), d.get("week", []),
                                 d.get("time", []), d.get("clean_conf", [])):
        out.append({"id": i, "on": bool(on), "days": [WEEKDAYS[b] for b in range(7) if w >> b & 1],
                    "repeat": bool(w >> 7 & 1), "time": f"{t >> 8:02d}:{t & 0xFF:02d}",
                    "mode": conf.get("mode"), "cid": conf.get("cid"), "conf": conf})
    return out


def dnd_period(v: int) -> tuple[str, str]:
    """11/2 -> ("23:00", "08:00"). Bytes: [h_ini, m_ini, h_fim, m_fim]."""
    return f"{v >> 24 & 0xFF:02d}:{v >> 16 & 0xFF:02d}", f"{v >> 8 & 0xFF:02d}:{v & 0xFF:02d}"


def encode_dnd(start: str, end: str) -> int:
    """("23:00", "06:00") -> 0x17000600. Inverso de dnd_period."""
    (h1, m1), (h2, m2) = (map(int, s.split(":")) for s in (start, end))
    for h, m in ((h1, m1), (h2, m2)):
        if not (0 <= h < 24 and 0 <= m < 60):
            raise ValueError(f"horário inválido {h:02d}:{m:02d}")
    return h1 << 24 | m1 << 16 | h2 << 8 | m2


# -- estado ---------------------------------------------------------------------
def faults(raw) -> dict:
    """2/66 -> {"ts": datetime|None, "codes": [210013]}"""
    d = parse_json(raw, {})
    return {"ts": ts(d.get("ts")), "codes": list(d.get("fault", []))}


def base_status(raw) -> dict:
    """2/18 -> {"mode": 0, "progress": 0}. Ver BASE_MODE."""
    d = parse_json(raw, {})
    return {"mode": d.get("mode", 0), "progress": d.get("progress", 0)}


# P2 resolvido em 11/10: 1 = secando (2/90 = minutos restantes), 2 = esvaziando o pó, 3 = lavando o mop
BASE_MODE = {0: "Ociosa", 1: "Secando", 2: "Esvaziando o pó", 3: "Lavando o mop"}


def current_config(raw) -> dict:
    """2/40 -> {"rooms": [8], "clean_mode": 3, …}. 'rooms' só existe durante a limpeza."""
    d = parse_json(raw, {})
    d.setdefault("rooms", [])
    return d


def running_custom(raw) -> dict | None:
    """2/40 durante uma limpeza personalizada -> {"id", "name", "params": {...}, "zones": [[x1,y1,…,x4,y4], …]}.

    Observado em 11/10: user_data = [[nomes], [valores]]; mode_data = retângulos em mm (coords do mapa).
    """
    ud = parse_json(raw, {}).get("user_define")
    if not ud:
        return None
    names, values = (ud.get("user_data") or [[], []])[:2]
    params = dict(zip(names, values))
    zones = params.pop("mode_data", None) or []
    return {"id": ud.get("id"), "name": ud.get("name", ""), "params": params, "zones": zones}


ZONE_MIN_MM, ZONE_MAX_MM, ZONE_MAX_COUNT = 300, 30000, 10


def zone_region(z) -> list[int]:
    """Zona do 2/40 ([x1,y1,…,x4,y4]) ou retângulo [x1,y1,x2,y2] (mm) -> cantos no formato da ação 2/37:
    [esq,cima, esq,baixo, dir,baixo, dir,cima] (o mesmo do xiaomi-vac nos modelos irmãos, ov21gl e afins)."""
    if not isinstance(z, (list, tuple)) or len(z) not in (4, 8) or not all(
            isinstance(v, (int, float)) and not isinstance(v, bool) for v in z):
        raise ValueError(f"zona inválida: {z!r} (esperado [x1,y1,x2,y2] ou 4 cantos, em mm)")
    xs, ys = [round(v) for v in z[0::2]], [round(v) for v in z[1::2]]
    left, right, bottom, top = min(xs), max(xs), min(ys), max(ys)
    for size in (right - left, top - bottom):
        if not ZONE_MIN_MM <= size <= ZONE_MAX_MM:
            raise ValueError(f"zona de {size} mm: o lado precisa ter entre {ZONE_MIN_MM} e {ZONE_MAX_MM} mm")
    return [left, top, left, bottom, right, bottom, right, top]


def zone_param(zones) -> str:
    """Lista de zonas -> parâmetro (piid 12) da ação 2/37 start-zone-sweep. FORMATO EM TESTE no H50 Pro."""
    if not isinstance(zones, list) or not 1 <= len(zones) <= ZONE_MAX_COUNT:
        raise ValueError(f"informe de 1 a {ZONE_MAX_COUNT} zonas")
    return json.dumps([{"blocks_region": zone_region(z), "blocks_attr": 0} for z in zones], separators=(",", ":"))


def furnitures(raw) -> list[dict]:
    """2/102 -> [{"id", "type", "rid"}]. Tipos (1220, 1400, 1740) e rid 248 ainda não mapeados."""
    return [{"id": f.get("id"), "type": f.get("type"), "rid": f.get("rid")}
            for f in parse_json(raw, {}).get("furnitures", [])]


def action_result(raw) -> dict:
    """2/67 -> {"type": 3, "code": 0, "index": …}. code 0 = ok."""
    return parse_json(raw, {})


def area_m2(v: int | None) -> float | None:
    """2/6 vem em 0,01 m²."""
    return None if v is None else round(v / 100, 2)


def progress(v: int | None) -> int | None:
    """2/89: -1 quando não há limpeza (D5)."""
    return None if v is None or v < 0 else v


# -- histórico ------------------------------------------------------------------
def clean_record(raw) -> dict:
    """10/3 -> totais + registros.

    label = <duração s>_<área m²×1000>_<sweep-type>_<?>_<?>; stime = epoch UTC.
    total_time em min; total_area em m²×1000.
    """
    d = parse_json(raw, {})
    recs = []
    for h in d.get("history_list", []):
        dur, area, stype, *_ = h["label"].split("_")
        recs.append({"stime": h["stime"], "start": ts(h["stime"]), "duration_s": int(dur),
                     "area_m2": int(area) / 1000, "sweep_type": int(stype), "raw_label": h["label"]})
    return {"total_min": d.get("total_time"), "total_area_m2": (d.get("total_area") or 0) / 1000,
            "total_count": d.get("total_count"), "records": recs}
