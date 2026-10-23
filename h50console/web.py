"""Servidor HTTP (http.server, sem dependências): API REST + SSE + arquivos estáticos.

Rotas (spec §8). Chaves de prop na URL usam hífen: /api/settings/2-31 = prop 2/31.
A UI lê o cache e o banco; só os comandos chegam ao robô, sempre pelo Gateway.
"""

from __future__ import annotations

import hmac
import json
import logging
import mimetypes
import queue
import re
import threading
import time
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from . import catalog, dash, decoders
from .auth import AuthError
from .gateway import CommandBlocked, Gateway
from .miio import MiioError
from .store import Store

log = logging.getLogger(__name__)
STATIC = Path(__file__).with_name("static")

CONSUMABLE_NAMES = {"mop": "Mop", "main_brush": "Escova principal", "side_brush": "Escova lateral",
                    "filter": "Filtro", "detergent": "Detergente", "dust_bag": "Saco de pó"}


class Raw:
    """Resposta binária (download): backup, CSV, pacote de diagnóstico."""

    def __init__(self, data: bytes, ctype: str = "application/octet-stream", filename: str | None = None):
        self.data, self.ctype, self.filename = data, ctype, filename


MAX_UPLOAD = 100 * 1024 * 1024
MAX_STREAMS = 32                  # conexões SSE simultâneas (cada uma ocupa uma thread)
MAX_JSON = 1024 * 1024            # nenhum corpo JSON legítimo do app passa disso
# o que o perfil somente leitura pode consultar (tudo o mais, inclusive qualquer POST/PUT/DELETE, é recusado)
VIEWER_GET = re.compile(
    r"/api/(app|robots|stream|cloud/status)"
    r"|/api/r/[\w-]+/(stream|health|state|rooms|custom|furniture|settings|consumables|history|history/path|schedules"
    r"|export/cleanings\.csv|dash/(cleanings|timeline|coverage)|faults|map|app|cloud/status)")
# único POST do visualizador: atualizar o mapa a partir da nuvem (só baixa; não mexe no robô)
VIEWER_POST = re.compile(r"/api/r/[\w-]+/cloud/map")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _key(url_key: str) -> str:
    key = url_key.replace("-", "/")
    if key not in catalog.PROPS:
        raise ApiError(404, f"prop {key} desconhecida")
    return key


def _local(epoch) -> str | None:
    return datetime.fromtimestamp(epoch, timezone.utc).astimezone().isoformat(timespec="seconds") if epoch else None


class App:
    """Lógica das rotas, separada do HTTP para ser testável."""

    def __init__(self, gw: Gateway, store: Store, raw_enabled: bool = False, device_name: str = "H50 Pro",
                 cloud=None, robot=None):
        self.robot = robot                  # fleet.Robot (nome, did e pasta do mapa); None nos testes simples
        self.cloud = cloud
        self.gw = gw
        self.store = store
        self.raw_enabled = raw_enabled
        self.default_name = device_name
        self.routes = [
            ("GET", r"/api/health", self.health),
            ("GET", r"/api/state", self.state),
            ("GET", r"/api/rooms", self.rooms),
            ("GET", r"/api/custom", self.custom),
            ("GET", r"/api/furniture", self.furniture),
            ("POST", r"/api/clean/rooms", self.clean_rooms),
            ("POST", r"/api/clean/custom/(\d+)", self.clean_custom),
            ("POST", r"/api/actions/([\w-]+)", self.action),
            ("GET", r"/api/settings", self.settings),
            ("PUT", r"/api/settings/([\d-]+)", self.put_setting),
            ("GET", r"/api/consumables", self.consumables),
            ("POST", r"/api/consumables/(\w+)/reset", self.reset_consumable),
            ("GET", r"/api/history", self.history),
            ("GET", r"/api/history/path", self.history_path),
            ("GET", r"/api/schedules", self.schedules),
            ("GET", r"/api/commands", self.commands),
            ("GET", r"/api/export/cleanings\.csv", self.export_cleanings),
            ("GET", r"/api/lab/props", self.lab_props),
            ("POST", r"/api/lab/read", self.lab_read),
            ("GET", r"/api/lab/changes", self.lab_changes),
            ("POST", r"/api/lab/notes", self.lab_note),
            ("POST", r"/api/lab/scan", self.lab_scan),
            ("GET", r"/api/lab/actions", self.lab_actions),
            ("GET", r"/api/lab/cloud-objects", self.lab_cloud_objects),
            ("GET", r"/api/lab/cloud-map", self.lab_cloud_map),
            ("GET", r"/api/dash/cleanings", self.dash_cleanings),
            ("GET", r"/api/dash/timeline", self.dash_timeline),
            ("GET", r"/api/dash/coverage", self.dash_coverage),
            ("GET", r"/api/faults", self.faults),
            ("GET", r"/api/app", self.app_info),
            ("PUT", r"/api/app/name", self.put_name),
            ("POST", r"/api/raw/(get|set|action)", self.raw),
            ("GET", r"/api/cloud/status", self.cloud_status),
            ("POST", r"/api/cloud/qr", self.cloud_qr),
            ("POST", r"/api/cloud/logout", self.cloud_logout),
            ("POST", r"/api/cloud/map", self.cloud_map),
            ("GET", r"/api/map", self.map_latest),
            ("GET", r"/api/map/backups", self.map_backups),
            ("POST", r"/api/map/backup", self.map_backup),
            ("POST", r"/api/map/edit/preview", self.map_edit_preview),
            ("POST", r"/api/map/edit/save", self.map_edit_save),
            ("POST", r"/api/cloud/history", self.cloud_history),
        ]

    def dispatch(self, method: str, path: str, query: dict, body):
        for m, pattern, fn in self.routes:
            match = re.fullmatch(pattern, path)
            if match and m == method:
                return fn(*match.groups(), query=query, body=body)
        if any(re.fullmatch(p, path) for _, p, _ in self.routes):
            raise ApiError(405, "método não permitido")
        raise ApiError(404, "rota não encontrada")

    # -- leitura --------------------------------------------------------------
    def health(self, **_):
        return {"app": "ok", **self.gw.health()}

    def _fmt(self, key: str, kind: str, v):
        if v is None:
            return None
        if kind == "enum":
            return catalog.label(key, v)
        if kind == "pct":
            return f"{v} %"
        if kind == "progress":
            p = decoders.progress(v)
            return "—" if p is None else f"{p} %"
        if kind == "area":
            return f"{decoders.area_m2(v):.2f} m²".replace(".", ",")
        if kind == "seconds":
            return f"{v // 60} min" if v else "0 min"
        if kind == "minutes":
            return f"{v} min"
        if kind == "bool":
            return "sim" if v else "não"
        if kind == "rooms":
            custom = decoders.running_custom(v)
            if custom:
                zones = len(custom["zones"])
                return f'Rotina "{custom["name"]}"' + (f" ({zones} zona{'s' if zones > 1 else ''})" if zones else "")
            ids = decoders.current_config(v).get("rooms") or []
            names = {r["id"]: r["name"] for r in self.store.rooms()}
            return ", ".join(names.get(i, str(i)) for i in ids) or "—"
        if kind == "base":
            b = decoders.base_status(v)
            return decoders.BASE_MODE.get(b["mode"], f"modo {b['mode']}") + (f" ({b['progress']} %)" if b["progress"] else "")
        if kind == "fault":
            return "nenhuma" if v == 0 else str(v)
        if kind == "faults":
            return ", ".join(map(str, decoders.faults(v)["codes"])) or "nenhuma"
        if kind == "epoch":
            return _local(v)
        return v

    def state(self, **_):
        snap = self.gw.cache.snapshot()
        rows = [{"key": k, "label": lbl, "value": self._fmt(k, kind, snap.get(k)), "raw": snap.get(k),
                 "changed_at": self.gw.cache.changed_at.get(k)} for k, lbl, kind in catalog.STATE_ROWS]
        status = snap.get("2/2")
        return {"health": self.gw.health(), "status": status, "status_label": catalog.label("2/2", status),
                "battery": snap.get("3/1"), "active": status in catalog.ACTIVE_STATUS,
                "block_start": catalog.start_block_reason(snap), "rows": rows, "values": snap}

    def rooms(self, **_):
        last: dict[int, int] = {}
        for c in self.store.cleanings():
            for rid in c["rooms"]:
                last.setdefault(rid, c["stime"])
        return [{**r, "last_clean": _local(last.get(r["id"]))} for r in self.store.rooms()]

    def custom(self, **_):
        names = {r["id"]: r["name"] for r in self.store.rooms()}
        out = []
        for c in decoders.custom_cleans(self.gw.cache.get("2/42")):
            out.append({**c, "rooms": [names.get(i, str(i)) for i in c["room_ids"]]})
        return out

    def furniture(self, **_):
        names = {r["id"]: r["name"] for r in self.store.rooms()}
        out = []
        for f in decoders.furnitures(self.gw.cache.get("2/102")):
            room = "fora dos cômodos" if f["rid"] == catalog.FURNITURE_OUTSIDE_RID else names.get(f["rid"], str(f["rid"]))
            out.append({**f, "name": catalog.FURNITURE_TYPES.get(f["type"], f"tipo {f['type']}"), "room": room})
        return out

    def settings(self, **_):
        snap = self.gw.cache.snapshot()
        out = []
        for key, group, lbl in catalog.visible_settings(snap):
            prop = catalog.PROPS[key]
            v = snap.get(key)
            item = {"key": key, "group": group, "label": lbl, "value": v, "format": prop.format}
            if key == "11/2":
                item["type"] = "dnd"
                item["display"] = "–".join(decoders.dnd_period(v)) if v is not None else None
            elif prop.format == "bool":
                item["type"] = "bool"
            elif prop.values:
                item["type"] = "enum"
                item["options"] = catalog.options(key)
                item["display"] = catalog.label(key, v) if v is not None else None
            elif prop.range:
                item["type"] = "range"
                item["range"] = prop.range
            else:
                item["type"] = "text"
            out.append(item)
        return out

    def consumables(self, **_):
        snap = self.gw.cache.snapshot()
        out = []
        for item, (p, h) in catalog.CONSUMABLE_PROPS.items():
            pct = snap.get(p)
            out.append({"item": item, "name": CONSUMABLE_NAMES[item], "pct": pct, "hours": snap.get(h),
                        "alert": pct is not None and pct < 10,
                        "forecast": dash.consumable_forecast(self.store.consumable_history(item))})
        return out

    def history(self, query, **_):
        def epoch(name):
            v = query.get(name, [None])[0]
            if not v:
                return None
            return int(datetime.fromisoformat(v).astimezone().timestamp())

        stype = query.get("type", [None])[0]
        rows = self.store.cleanings(epoch("from"), epoch("to"), int(stype) if stype else None)
        names = {r["id"]: r["name"] for r in self.store.rooms()}
        with_path = self.store.path_stimes()
        for r in rows:
            r["has_path"] = r["stime"] in with_path
            r["start"] = _local(r["stime"])
            r["type_label"] = catalog.label("2/5", r["sweep_type"]) if r["sweep_type"] else None
            r["room_names"] = [names.get(i, str(i)) for i in r["rooms"]]
            label = r["raw_label"] or ""
            if r["source"] == "derived" and label.startswith("custom:"):
                r["routine"] = label[7:]
            elif label.startswith("zones:"):
                r["routine"] = f"{label[6:]} zona(s)"
        totals = {"count": len(rows), "duration_s": sum(r["duration_s"] or 0 for r in rows),
                  "area_m2": round(sum(r["area_m2"] or 0 for r in rows), 2)}
        return {"records": rows, "totals": totals}

    def history_path(self, query, **_):
        """Caminho percorrido numa limpeza (pontos [x, y, tipo] em mm) + o mapa salvo, para desenhar."""
        try:
            stime = int(query.get("stime", [""])[0])
        except ValueError:
            raise ApiError(400, "informe stime (início da limpeza, em epoch)") from None
        rec = self.store.get_path(stime)
        if not rec or len(rec["points"]) < 2:
            raise ApiError(404, "esta limpeza não tem caminho gravado (só limpezas feitas com o app aberto e a nuvem conectada)")
        return {**rec, "map": self.cloud.latest_map(self._map_dir()) if self.cloud else None}

    def schedules(self, **_):
        routines = {c["id"]: c["name"] for c in decoders.custom_cleans(self.gw.cache.get("2/42"))}
        out = []
        for s in decoders.schedules(self.gw.cache.get("2/19")):
            s["routine"] = routines.get(s["cid"]) or catalog.label("2/5", s["mode"])
            out.append(s)
        return out

    def dash_cleanings(self, query, **_):
        days = max(1, min(366, int(query.get("days", [30])[0])))
        return dash.cleanings(self.store, days)

    def dash_coverage(self, query, **_):
        days = max(1, min(366, int(query.get("days", [30])[0])))
        return {**dash.coverage(self.store, days), "map": self.cloud.latest_map(self._map_dir()) if self.cloud else None}

    def dash_timeline(self, query, **_):
        hours = max(1, min(24 * 31, int(query.get("hours", [24])[0])))
        return dash.timeline(self.store, hours, current=self.gw.cache.snapshot())

    def export_cleanings(self, **_):
        import csv
        import io
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["inicio", "duracao_s", "area_m2", "tipo", "comodos", "fonte", "rotulo"])
        names = {r["id"]: r["name"] for r in self.store.rooms()}
        for r in reversed(self.store.cleanings()):
            w.writerow([_local(r["stime"]), r["duration_s"], r["area_m2"],
                        catalog.label("2/5", r["sweep_type"]) if r["sweep_type"] else "",
                        ";".join(names.get(i, str(i)) for i in r["rooms"]), r["source"], r["raw_label"] or ""])
        return Raw(out.getvalue().encode("utf-8-sig"), "text/csv; charset=utf-8", "limpezas.csv")

    # -- Laboratório: explorar o que os manuais não dizem -------------------------------------------------------
    @staticmethod
    def _short(v, n: int = 240):
        s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
        return s if len(s) <= n else s[:n] + "…"

    def lab_props(self, **_):
        snap = self.gw.cache.snapshot()
        out = []
        for key, p in catalog.PROPS.items():
            out.append({"key": key, "name": p.name, "service": p.service, "format": p.format, "access": list(p.access),
                        "in_spec": p.in_spec, "unit": p.unit, "readable": p.readable and key not in catalog._NOT_READABLE,
                        "writable": p.writable, "options": [[v, catalog.ENUM_PT.get(key, {}).get(v, d)] for v, d in p.values],
                        "value": self._short(snap[key]) if key in snap else None})
        known = set(catalog.PROPS)
        for d in self.store.discoveries():                       # achadas na varredura e que não estão no catálogo
            if d["key"] not in known:
                out.append({"key": d["key"], "name": "(descoberta)", "service": "x-descobertas", "format": "?", "access": ["read"],
                            "in_spec": False, "unit": None, "readable": True, "writable": False, "options": [],
                            "value": d["last_value"], "discovered": True})
        return out

    def lab_read(self, body, **_):
        keys = list((body or {}).get("keys") or catalog.READABLE)[:300]
        for k in keys:
            _key(k.replace("/", "-")) if k in catalog.PROPS else None
        self.gw.read(keys)                                       # atualiza o cache (e dispara os efeitos normais)
        snap = self.gw.cache.snapshot()
        return {"via": self.gw.via, "values": {k: self._short(snap[k]) for k in keys if k in snap}}

    def lab_changes(self, query, **_):
        minutes = max(1, min(1440, int(query.get("minutes", [60])[0])))
        noisy = query.get("noisy", ["0"])[0] in ("1", "true")
        since = (datetime.now(timezone.utc) - __import__("datetime").timedelta(minutes=minutes)).isoformat(timespec="seconds")
        # leitura inicial (antes = nada) não é mudança: o app a registra toda vez que sobe
        rows = [r for r in self.store.changes_since(since)
                if noisy or (r["prop"] not in catalog.NOISY_PROPS and r["old_value"] is not None)]
        changes = [{"ts": r["ts"], "prop": r["prop"], "name": catalog.PROPS[r["prop"]].name if r["prop"] in catalog.PROPS else "",
                    "old": self._short(r["old_value"]), "new": self._short(r["new_value"])} for r in rows]
        return {"changes": changes, "notes": self.store.notes(since)}

    def lab_note(self, body, **_):
        text = str((body or {}).get("text", "")).strip()[:200]
        if not text:
            raise ApiError(400, "escreva a anotação")
        self.store.add_note(text)
        return {"ok": True}

    def lab_scan(self, body, **_):
        """Lê uma faixa de piids (SOMENTE LEITURA). O que responder e não estiver no catálogo vira 'descoberta'."""
        b = body or {}
        try:
            siid, first, last = int(b["siid"]), int(b["first"]), int(b["last"])
        except (KeyError, TypeError, ValueError):
            raise ApiError(400, "informe siid, first e last (números)") from None
        if not (1 <= siid <= 99 and 1 <= first <= last <= 999 and last - first < 40):
            raise ApiError(400, "faixa inválida: siid 1–99 e no máximo 40 piids por varredura")
        keys = [f"{siid}/{p}" for p in range(first, last + 1)]
        res = self.gw.raw_get(keys)
        out = []
        for k in keys:
            r = res.get(k, {})
            ok = r.get("code") == 0 and "value" in r
            if ok and k not in catalog.PROPS:
                self.store.upsert_discovery(k, r["value"])
            out.append({"key": k, "code": r.get("code"), "value": self._short(r["value"]) if ok else None,
                        "known": k in catalog.PROPS, "new": ok and k not in catalog.PROPS})
        return {"via": self.gw.via, "results": out}

    def lab_actions(self, **_):
        out = []
        for key, a in catalog.ACTIONS.items():
            out.append({"key": key, "name": a.name, "service": a.service, "risk": catalog.action_risk(a.name),
                        "params": [{"piid": p, "name": catalog.PROPS[f"{a.siid}/{p}"].name if f"{a.siid}/{p}" in catalog.PROPS else str(p)}
                                   for p in a.params]})
        return out

    def _obj(self, prop: str) -> str | None:
        raw = self.gw.cache.get(prop)
        if raw is None:
            raw = self.gw.raw_get([prop]).get(prop, {}).get("value")
        return decoders.parse_json(raw, {}).get("obj_name") or None

    def _valid_obj(self, obj: str) -> bool:
        """Só objetos DESTE robô: <uid>/<did>/<n> com o mesmo did (nada de baixar caminhos arbitrários)."""
        parts = (obj or "").split("/")
        return len(parts) == 3 and parts[1] == self._did() and parts[2].isdigit() and parts[0].isdigit()

    def _summarize(self, obj: str) -> dict:
        from . import mapdecode
        from .cloud import CloudError
        out = {"obj_name": obj.split("/")[-1]}
        try:
            blob = self._cloud().fetch_object(obj, self._did())
        except CloudError as ex:
            return {**out, "error": str(ex), "hint": "pode existir só durante uma limpeza" if "404" in str(ex) else None}
        out["bytes"] = len(blob)
        try:
            m = mapdecode.decrypt(blob, self.robot.cfg.model if self.robot else catalog.MODEL, self._did())
            out.update(decrypted=True, rooms=len(m.get("room_attrs", [])), walls=len(m.get("fb_walls", [])),
                       furniture=len(m.get("furniture", [])), keys=sorted(m.keys()),
                       path_points=len(json.loads(m.get("paths", {}).get("points") or "[]")))
        except Exception as ex:  # noqa: BLE001 - objetos de outro formato (trajetória, tapetes) só mostram o tamanho
            out.update(decrypted=False, note=f"não é um mapa decifrável ({type(ex).__name__})")
        return out

    def lab_cloud_objects(self, **_):
        self._cloud()
        items = []
        for prop, label in (("10/1", "Mapa atual"), ("10/2", "Trajetória"), ("10/7", "Mapa de tapetes")):
            obj = self._obj(prop)
            items.append({"prop": prop, "label": label, **({"obj": obj, **self._summarize(obj)} if obj else {"empty": True})})
        raw = self.gw.cache.get("10/13") or self.gw.raw_get(["10/13"]).get("10/13", {}).get("value")
        for b in decoders.parse_json(raw, {}).get("backup_map_arr", []):
            items.append({"prop": "10/13", "label": f"Backup do mapa (id {b.get('backup_id')})", "obj": b["obj_name"],
                          **self._summarize(b["obj_name"])})
        return items

    def lab_cloud_map(self, query, **_):
        """Mapa de um objeto da nuvem (ex.: backup) pronto para desenhar. Não grava nada."""
        from . import mapdecode
        from .cloud import CloudError
        obj = query.get("obj", [""])[0]
        if not self._valid_obj(obj):
            raise ApiError(400, "objeto inválido para este robô")
        try:
            blob = self._cloud().fetch_object(obj, self._did())
            return mapdecode.for_ui(mapdecode.decrypt(blob, self.robot.cfg.model if self.robot else catalog.MODEL, self._did()))
        except CloudError as ex:
            raise ApiError(502, str(ex)) from ex
        except ValueError as ex:
            raise ApiError(422, f"não é um mapa: {ex}") from ex

    def commands(self, query, **_):
        return self.store.commands(int(query.get("limit", [200])[0]))

    def faults(self, **_):
        return self.store.faults()

    def _did(self) -> str:
        if self.robot and self.robot.cfg.did:
            return self.robot.cfg.did
        return str(self.store.get_setting("robot_did") or "")

    def _map_dir(self):
        return self.robot.map_dir if self.robot else None

    def app_info(self, **_):
        name = self.robot.cfg.name if self.robot else self.store.get_setting("device_name", self.default_name)
        return {"name": name, "model": self.robot.cfg.model if self.robot else catalog.MODEL,
                "raw_enabled": self.raw_enabled, "map_edited_at": self.store.get_setting("map_edited_at")}

    # -- comandos -------------------------------------------------------------
    def clean_rooms(self, body, **_):
        ids = (body or {}).get("ids") or []
        if not ids:
            raise ApiError(400, "informe ids")
        return self.gw.clean_rooms([int(i) for i in ids])

    def clean_custom(self, cid, **_):
        routine = next((c for c in decoders.custom_cleans(self.gw.cache.get("2/42")) if c["id"] == int(cid)), None)
        if routine is None:
            raise ApiError(404, "rotina não encontrada")
        if not routine["room_ids"]:
            raise ApiError(409, "rotina sem cômodos: disparo nativo ainda não descoberto (P3)")
        # P3 pendente: usa a limpeza por cômodos com os cômodos da rotina. A ação 2/16 não leva o modo: o robô
        # usaria o modo global (2/4), que pode estar com pano numa rotina "só aspirar". Grava antes o modo da rotina.
        modes = sorted(set(routine["mop"]))
        if len(modes) > 1:
            raise ApiError(409, "esta rotina usa modos diferentes por cômodo; rode pelo app Xiaomi Home")
        if modes and modes[0] not in catalog.ENUM_PT["2/4"]:
            raise ApiError(409, f"modo {modes[0]} da rotina desconhecido; rode pelo app Xiaomi Home")
        self.gw.check_start()
        mode = modes[0] if modes else None
        if mode is not None and self.gw.cache.get("2/4") != mode:
            res = self.gw.set("2/4", mode)
            if not isinstance(res, dict) or res.get("code") != 0:
                raise ApiError(502, f"não consegui ajustar o modo da rotina (2/4 = {mode}): {res}")
        return {"fallback": "clean_rooms", "mode": mode, **self.gw.clean_rooms(routine["room_ids"])}

    def action(self, name, **_):
        if name not in catalog.SIMPLE_ACTIONS:
            raise ApiError(404, f"ação {name} desconhecida")
        return self.gw.run(name)

    def put_setting(self, url_key, body, **_):
        key = _key(url_key)
        if key not in {k for k, _, _ in catalog.SETTINGS}:
            raise ApiError(403, f"{key} não está no catálogo de ajustes")
        value = (body or {}).get("value")
        if key == "11/2" and isinstance(value, str):
            start, end = re.split(r"\s*[-–]\s*", value)
            value = decoders.encode_dnd(start, end)
        return self.gw.set(key, value)

    def reset_consumable(self, item, **_):
        if item not in catalog.CONSUMABLES:
            raise ApiError(404, "consumível desconhecido")
        return self.gw.reset_consumable(item)

    def put_name(self, body, **_):
        name = str((body or {}).get("value", "")).strip()[:40]
        if not name:
            raise ApiError(400, "nome vazio")
        if self.robot:
            self.robot.rename(name)
        else:
            self.store.set_setting("device_name", name)
        return {"name": name}

    # -- nuvem (experimental) ---------------------------------------------------
    def _cloud(self):
        if self.cloud is None:
            raise ApiError(503, "módulo de nuvem desligado")
        return self.cloud

    def cloud_status(self, **_):
        return self._cloud().status()

    def cloud_qr(self, **_):
        return self._cloud().start_qr(on_done=lambda st: self.gw.bus.publish("cloud", st))

    def cloud_logout(self, **_):
        return self._cloud().logout()

    def cloud_map(self, **_):
        raw = self.gw.cache.get("10/1")
        if raw is None:  # ainda não lido pelo poller: lê agora, pelo gateway
            raw = self.gw.raw_get(["10/1"]).get("10/1", {}).get("value")
        obj = decoders.parse_json(raw, {}).get("obj_name")
        if not obj:
            raise ApiError(409, "10/1 ainda não lido do robô")
        from .cloud import CloudError
        cloud = self._cloud()
        did = self._did() or obj.split("/")[1]
        if time.time() - cloud.last_fetch(did) < 20:   # evita martelar a nuvem (a UI pede ao ver 10/1 mudar)
            return {"throttled": True}
        try:
            res = cloud.fetch_map(obj, did, self._map_dir(), model=self.robot.cfg.model if self.robot else "")
        except CloudError as ex:
            raise ApiError(502, str(ex)) from ex
        self.gw.bus.publish("mapdata", {"at": int(time.time())})
        return res

    def cloud_history(self, **_):
        from . import history
        from .cloud import CloudError
        did = self._did()
        if not did:
            raise ApiError(409, "DID do robô ainda desconhecido")
        try:
            res = history.sync(self._cloud(), self.store, did)
        except CloudError as ex:
            raise ApiError(502, str(ex)) from ex
        if res["added"] or res["updated"]:
            self.gw.bus.publish("cleaning", {"source": "cloud"})
        return res

    def map_latest(self, **_):
        m = self._cloud().latest_map(self._map_dir())
        if m is None:
            raise ApiError(404, "nenhum mapa baixado ainda")
        return m

    # -- edição do mapa (rascunho; nada é gravado no robô) -----------------------------------------------
    MAP_BACKUPS_KEEP = 20

    def _raw_map(self) -> dict:
        from . import mapdecode
        f = (self._map_dir() or Path("data/map")) / "latest.bin"
        if not f.exists():
            raise ApiError(404, "nenhum mapa baixado ainda: abra o Início com a nuvem conectada")
        return mapdecode.decrypt(f.read_bytes(), self.robot.cfg.model if self.robot else catalog.MODEL, self._did())

    def map_backups(self, **_):
        d = (self._map_dir() or Path("data/map")) / "backups"
        files = sorted(d.glob("map-*.bin"), reverse=True) if d.exists() else []
        return [{"name": f.name, "bytes": f.stat().st_size, "at": int(f.stat().st_mtime)} for f in files]

    def map_backup(self, **_):
        """Copia o mapa baixado (cifrado, como veio da nuvem) para backups/. É a rede de segurança antes de qualquer edição."""
        base = self._map_dir() or Path("data/map")
        src = base / "latest.bin"
        if not src.exists():
            raise ApiError(404, "nenhum mapa baixado ainda: abra o Início com a nuvem conectada")
        d = base / "backups"
        d.mkdir(parents=True, exist_ok=True)
        name = time.strftime("map-%Y%m%d-%H%M%S.bin")
        (d / name).write_bytes(src.read_bytes())
        for old in sorted(d.glob("map-*.bin"), reverse=True)[self.MAP_BACKUPS_KEEP:]:
            old.unlink(missing_ok=True)
        return {"name": name, "backups": self.map_backups()}

    def map_edit_preview(self, body, **_):
        """Aplica as operações a uma cópia do mapa e devolve o resultado + a lista de mudanças. Não grava nada."""
        from . import mapdecode, mapedit
        current = self._raw_map()
        new = mapedit.apply_ops(current, (body or {}).get("ops") or [])
        return {"map": mapdecode.for_ui(new), "changes": mapedit.diff(current, new), "saved_to_robot": False}

    def map_edit_save(self, **_):
        raise ApiError(501, "gravar o mapa no robô ainda não foi validado (formato de escrita desconhecido). "
                            "Por enquanto o editor só monta e mostra o rascunho. Veja docs/BACKLOG.md.")

    def raw(self, op, body, **_):
        if not self.raw_enabled:
            raise ApiError(403, "explorador desligado (RAW_ENABLED=false)")
        body = body or {}
        if op == "get":
            return self.gw.raw_get(body.get("keys") or [])
        if op == "set":
            return self.gw.set(body["key"], body.get("value"), source="api")
        return self.gw.action(body["key"], body.get("params") or [], source="api")


STREAM_PATH = re.compile(r"/api/r/([\w.-]+)/stream")
# acessíveis sem sessão: a tela de login e o necessário para instalar o app (PWA)
PUBLIC_FILES = {"/login.html", "/login.js", "/manifest.webmanifest", "/sw.js", "/icon.svg", "/icon-192.png", "/icon-512.png"}
CSP = ("default-src 'self'; img-src 'self' data: https://*.xiaomi.com; style-src 'self' 'unsafe-inline'; "
       "script-src 'self'; connect-src 'self'; worker-src 'self'; manifest-src 'self'; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


def make_handler(hub, auth=None, trust_proxy: bool = False, cookie_secure: bool = False, local_only: bool = False,
                 extra_hosts=(), metrics_token: str = ""):
    streams = {"n": 0}
    streams_lock = threading.Lock()
    class Handler(BaseHTTPRequestHandler):
        server_version = "H50Console/1.0"

        def log_message(self, fmt, *args):  # silencia o log padrão por requisição
            log.debug("%s %s", self.address_string(), fmt % args)

        # -- utilidades ----------------------------------------------------------------------------
        def _ip(self) -> str:
            if trust_proxy:                          # só atrás de um proxy confiável (senão o cabeçalho é forjável)
                fwd = (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
                if fwd:
                    return fwd
            return self.client_address[0]

        def _secure_cookie(self) -> bool:
            return cookie_secure or (trust_proxy and self.headers.get("X-Forwarded-Proto") == "https")

        def _drain(self) -> None:
            """Recusas antecipadas (403, 413...) respondem sem ler o corpo; fechar a conexão com dados pendentes pode
            gerar um RST que apaga a resposta no cliente. Então descarta o que sobrou (até 1 MB) antes de responder."""
            if getattr(self, "_body_done", False):
                return
            self._body_done = True
            length = self._length()
            if 0 < length <= MAX_JSON:
                try:
                    self.rfile.read(length)
                except OSError:
                    pass

        def _send(self, status: int, payload=None, ctype="application/json; charset=utf-8", raw: bytes | None = None,
                  cookies=(), headers=None):
            self._drain()
            data = raw if raw is not None else json.dumps(payload, ensure_ascii=False, default=str).encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=(), usb=()")
            self.send_header("Cross-Origin-Opener-Policy", "same-origin")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            if self._secure_cookie():
                self.send_header("Strict-Transport-Security", "max-age=31536000")
            for c in cookies:
                self.send_header("Set-Cookie", c)
            for k, v in (headers or {}).items():
                self.send_header(k, str(v))
            self.end_headers()
            self.wfile.write(data)

        def _redirect(self, location: str):
            self.send_response(302)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

        def _length(self) -> int:
            """Content-Length como inteiro >= 0; -1 se for inválido."""
            raw = (self.headers.get("Content-Length") or "0").strip()
            return int(raw) if raw.isdigit() else -1

        def _host_ok(self) -> bool:
            """Servidor só em localhost e sem proxy: recusa Host de outro nome (DNS rebinding: um site que
            aponta o próprio domínio para 127.0.0.1 e tenta falar com o painel pelo navegador da vítima)."""
            if not local_only:
                return True
            host = (self.headers.get("Host") or "").strip().lower()
            name = host.rsplit(":", 1)[0] if not host.endswith("]") else host
            return name in LOCAL_HOSTS or host in extra_hosts or name in extra_hosts

        def _read_json(self):
            length = self._length()
            if length < 0:
                return None, "Content-Length inválido"
            if not length:
                return None, None
            if length > MAX_JSON:
                return None, "corpo grande demais"
            self._body_done = True
            try:
                return json.loads(self.rfile.read(length)), None
            except (json.JSONDecodeError, UnicodeDecodeError):
                return None, "JSON inválido"

        # -- autenticação ----------------------------------------------------------------------------
        def _role(self) -> str | None:
            if auth is None or not auth.enabled:
                return "admin"
            return auth.role_of(auth.token_from_cookie_header(self.headers.get("Cookie")))

        def _authed(self) -> bool:
            return self._role() is not None

        def _csrf_ok(self) -> bool:
            """POST/PUT/DELETE exigem o cabeçalho X-H50 (um formulário de outro site não consegue enviá-lo)
            e, se houver Origin, ele tem de ser o próprio servidor."""
            if self.headers.get("X-H50") != "1":
                return False
            origin = self.headers.get("Origin")
            if origin and urlparse(origin).netloc != self.headers.get("Host"):
                return False
            return True

        def _auth_api(self, method: str, path: str):
            body, err = self._read_json()
            if err:
                return self._send(400, {"error": err})
            body = body or {}
            ip, secure = self._ip(), self._secure_cookie()
            try:
                if path == "/api/auth/status" and method == "GET":
                    return self._send(200, auth.status(self._authed(), self._role()))
                if path == "/api/auth/activity" and method == "GET":
                    if self._role() != "admin":
                        return self._send(401 if not self._authed() else 403, {"error": "só o administrador", "login": not self._authed()})
                    return self._send(200, {"events": list(auth.activity)})
                if method != "POST":
                    return self._send(405, {"error": "método não permitido"})
                if not self._csrf_ok():
                    return self._send(403, {"error": "requisição bloqueada (CSRF)"})
                if path == "/api/auth/login":
                    token = auth.login(str(body.get("password", "")), ip)
                elif path == "/api/auth/setup":
                    token = auth.setup(str(body.get("code", "")), str(body.get("password", "")), ip)
                elif path == "/api/auth/logout":
                    return self._send(200, {"ok": True}, cookies=[auth.cookie("", secure, clear=True)])
                elif path in ("/api/auth/password", "/api/auth/revoke", "/api/auth/viewer"):
                    if not self._authed():
                        return self._send(401, {"error": "login necessário", "login": True})
                    if self._role() != "admin":
                        return self._send(403, {"error": "só o administrador"})
                    if path == "/api/auth/viewer":
                        auth.set_viewer_password(str(body.get("password", "")))
                        return self._send(200, {"ok": True, "viewer_enabled": auth.viewer_enabled})
                    token = (auth.change_password(str(body.get("current", "")), str(body.get("new", "")), ip)
                             if path.endswith("password") else auth.revoke_all(ip))
                else:
                    return self._send(404, {"error": "rota não encontrada"})
                return self._send(200, {"ok": True}, cookies=[auth.cookie(token, secure)])
            except AuthError as ex:
                extra = {"Retry-After": ex.retry_after} if ex.retry_after else None
                return self._send(ex.status, {"error": str(ex), "retry_after": ex.retry_after}, headers=extra)

        # -- roteamento ---------------------------------------------------------------------------------
        def _handle(self, method: str):
            if not self._host_ok():
                return self._send(421, {"error": "nome de host não permitido (use localhost ou defina H50_ALLOWED_HOSTS)"})
            url = urlparse(self.path)
            path = url.path
            if path == "/metrics" and method == "GET":        # coletor Prometheus: token fixo (H50_METRICS_TOKEN) ou sessão
                got = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
                if (metrics_token and hmac.compare_digest(got.encode(), metrics_token.encode())) or self._authed():
                    try:
                        text = hub.metrics_text()
                    except Exception:  # noqa: BLE001
                        log.exception("erro ao montar /metrics")
                        return self._send(500, {"error": "erro interno"})
                    return self._send(200, ctype="text/plain; version=0.0.4; charset=utf-8", raw=text.encode())
                return self._send(401, {"error": "login necessário"}, headers={"WWW-Authenticate": "Bearer"})
            if path.startswith("/api/auth/"):
                if auth is None or not auth.enabled:     # sem senha (só localhost): o status responde que está liberado
                    return self._send(200, {"enabled": False, "configured": False, "setup_required": False,
                                            "authenticated": True})
                return self._auth_api(method, path)
            if auth is not None and auth.enabled and not (method == "GET" and path in PUBLIC_FILES):
                if not self._authed():
                    if path.startswith("/api/"):
                        return self._send(401, {"error": "login necessário", "login": True})
                    return self._redirect("/login.html?next=" + quote(self.path, safe=""))
                if method != "GET" and not self._csrf_ok():
                    return self._send(403, {"error": "requisição bloqueada (CSRF)"})
                if self._role() == "viewer" and not ((method == "GET" and (VIEWER_GET.fullmatch(path) or not path.startswith("/api/")))
                                                     or (method == "POST" and VIEWER_POST.fullmatch(path))):
                    return self._send(403, {"error": "perfil somente leitura"})
            if method == "GET" and path == "/api/stream":
                return self._stream(hub.bus)                       # eventos da conta
            m = STREAM_PATH.fullmatch(path)
            if method == "GET" and m:
                try:
                    return self._stream(hub.robot_bus(m.group(1)))   # eventos de um robô
                except ApiError as ex:
                    return self._send(ex.status, {"error": str(ex)})
            if method == "GET" and not path.startswith("/api/"):
                return self._static(path)
            if method == "POST" and path == "/api/backup/restore":        # upload bruto do arquivo de backup
                length = self._length()
                if length < 0:
                    return self._send(400, {"error": "Content-Length inválido"})
                if length > MAX_UPLOAD:
                    return self._send(413, {"error": "arquivo grande demais"})
                self._body_done = True
                body, err = {"data": self.rfile.read(length), "passphrase": self.headers.get("X-H50-Passphrase", "")}, None
            else:
                body, err = self._read_json()
            if err:
                return self._send(400, {"error": err})
            try:
                result = hub.dispatch(method, path, parse_qs(url.query), body)
                if path.endswith("/cloud/status") and self._role() == "viewer":   # sem QR, conta nem país
                    result = {"state": result.get("state")}
                if path == "/api/robots" and self._role() == "viewer":      # o visualizador não precisa de IP, DID nem pastas
                    keep = ("id", "name", "model", "online", "via", "status", "status_label", "battery", "active", "supported")
                    result = [{k: r[k] for k in keep if k in r} for r in result]
                if isinstance(result, Raw):
                    disp = {"Content-Disposition": f'attachment; filename="{result.filename}"'} if result.filename else None
                    return self._send(200, ctype=result.ctype, raw=result.data, headers=disp)
                self._send(200, result)
            except ApiError as ex:
                self._send(ex.status, {"error": str(ex)})
            except CommandBlocked as ex:
                self._send(409, {"error": str(ex)})
            except (ValueError, KeyError) as ex:
                self._send(400, {"error": str(ex)})
            except MiioError as ex:
                self._send(503, {"error": f"robô sem resposta: {ex}"})
            except Exception as ex:  # noqa: BLE001
                log.exception("erro em %s %s", method, path)
                self._send(500, {"error": f"{type(ex).__name__}: {ex}"})

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def do_PUT(self):
            self._handle("PUT")

        def do_DELETE(self):
            self._handle("DELETE")

        def _static(self, path: str):
            rel = "index.html" if path in ("", "/") else path.lstrip("/")
            f = (STATIC / rel).resolve()
            if STATIC.resolve() not in f.parents or not f.is_file():
                return self._send(404, {"error": "não encontrado"})
            ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
            if f.suffix == ".webmanifest":
                ctype = "application/manifest+json"
            if ctype.startswith("text/") or ctype.endswith("javascript") or ctype.endswith("json"):
                ctype += "; charset=utf-8"
            self._send(200, ctype=ctype, raw=f.read_bytes())

        def _stream(self, bus):
            with streams_lock:
                if streams["n"] >= MAX_STREAMS:
                    return self._send(429, {"error": "conexões de eventos demais abertas; feche outras abas"})
                streams["n"] += 1
            q = bus.subscribe()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Accel-Buffering", "no")     # nginx: não acumular o fluxo
                self.end_headers()
                self.wfile.write(b"retry: 3000\n\n")
                self.wfile.flush()
                while True:
                    try:
                        event, data = q.get(timeout=15)
                        msg = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"
                    except queue.Empty:
                        msg = ": ping\n\n"
                    self.wfile.write(msg.encode())
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                pass
            finally:
                bus.unsubscribe(q)
                with streams_lock:
                    streams["n"] -= 1

    return Handler


def make_server(hub, host: str = "127.0.0.1", port: int = 8050, auth=None, trust_proxy: bool = False,
                cookie_secure: bool = False, extra_hosts=(), metrics_token: str = "") -> ThreadingHTTPServer:
    from .auth import is_loopback
    local_only = is_loopback(host) and not trust_proxy
    srv = ThreadingHTTPServer((host, port), make_handler(hub, auth, trust_proxy, cookie_secure, local_only,
                                                         {h.lower() for h in extra_hosts}, metrics_token))
    srv.daemon_threads = True
    return srv
