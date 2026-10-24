"""Operações MIoT de alto nível sobre o MiioClient: get em lotes, set validado, ações.

Regras do aparelho (spec §4 + PLANO-MVP.md §5):
- a resposta UDP não pode passar de ~1,4 KB: os lotes são montados pelo tamanho estimado
  das respostas (catalog.SIZE_HINT, ajustado com o tamanho real visto), não pela quantidade;
- se um lote falhar, lê item a item; item que falha sozinho vira code "timeout";
- os resultados são casados por (siid, piid), nunca pelo did (D2);
- só métodos MIoT (D3).
"""

from __future__ import annotations

import json
from collections.abc import Iterable

from . import catalog, decoders
from .miio import MiioClient, MiioDeviceError, MiioError

# soma dos itens de um lote; deixa folga para o envelope {"id":…,"result":[…]} e o padding AES
DEFAULT_BUDGET = 1150


def _pair(key: str) -> tuple[int, int]:
    s, p = key.split("/")
    return int(s), int(p)


def plan_batches(keys: Iterable[str], sizes: dict[str, int], budget: int = DEFAULT_BUDGET,
                 max_items: int | None = None) -> list[list[str]]:
    """Agrupa chaves em lotes cuja soma de tamanhos estimados cabe no orçamento (ordem preservada)
    e que não passam de `max_items` itens (limite da nuvem em tempo real)."""
    batches: list[list[str]] = []
    cur: list[str] = []
    total = 0
    for k in keys:
        size = sizes.get(k, catalog.SIZE_DEFAULT)
        if cur and (total + size > budget or (max_items and len(cur) >= max_items)):
            batches.append(cur)
            cur, total = [], 0
        cur.append(k)
        total += size
    if cur:
        batches.append(cur)
    return batches


class H50:
    def __init__(self, client: MiioClient, budget: int = DEFAULT_BUDGET):
        self.client = client
        self.budget = budget
        self.sizes: dict[str, int] = dict(catalog.SIZE_HINT)

    # -- leitura --------------------------------------------------------------
    def _get_raw(self, keys: list[str], retry: int = 0) -> dict[str, dict]:
        req = [{"did": k, "siid": s, "piid": p} for k in keys for s, p in [_pair(k)]]
        res = self.client.send("get_properties", req, retry=retry)
        out = {}
        for r in res:
            k = f"{r.get('siid')}/{r.get('piid')}"
            out[k] = {"code": r.get("code"), **({"value": r["value"]} if "value" in r else {})}
            self.sizes[k] = max(len(json.dumps(r, ensure_ascii=False).encode()), catalog.SIZE_DEFAULT // 2)
        return out

    def get(self, keys: Iterable[str]) -> dict[str, dict]:
        """Lê props. Devolve {"2/2": {"code": 0, "value": 4}, "10/3": {"code": "oversized"}, …}.

        Lança MiioError só se NADA respondeu (robô offline); falhas parciais vêm por item.
        """
        keys = list(dict.fromkeys(keys))
        prepare = getattr(self.client, "prepare", None)
        if prepare:
            prepare()                           # AutoClient decide LAN x nuvem ANTES de planejar os lotes
        # LAN: respostas > ~1,4 KB não chegam (10/3); nuvem: sem esse limite e com lotes grandes
        skip = set() if getattr(self.client, "oversize_ok", False) else catalog.OVERSIZED
        budget = getattr(self.client, "max_batch_bytes", None) or self.budget
        max_items = getattr(self.client, "max_batch_items", None)
        out: dict[str, dict] = {k: {"code": "oversized"} for k in keys if k in skip}
        todo = [k for k in keys if k not in skip]
        answered = 0
        last_err: MiioError | None = None
        for batch in plan_batches(todo, self.sizes, budget, max_items):
            try:
                got = self._get_raw(batch)
                out.update(got)
                answered += 1
                continue
            except MiioError as ex:
                last_err = ex
            if len(batch) == 1:
                out[batch[0]] = {"code": "timeout"}
                continue
            for k in batch:  # lote sem resposta: item a item
                try:
                    out.update(self._get_raw([k]))
                    answered += 1
                except MiioError as ex:
                    last_err = ex
                    out[k] = {"code": "timeout"}
        if todo and not answered:
            raise last_err or MiioError("sem resposta")
        for k in todo:
            out.setdefault(k, {"code": "missing"})
        return {k: out[k] for k in keys}

    def values(self, keys: Iterable[str]) -> dict[str, object]:
        """Como get(), mas só os valores lidos com sucesso: {"2/2": 4, …}."""
        return {k: r["value"] for k, r in self.get(keys).items() if r.get("code") == 0 and "value" in r}

    # -- escrita --------------------------------------------------------------
    @staticmethod
    def validate(key: str, value):
        """Confere o valor contra a spec e devolve-o normalizado. Lança ValueError."""
        prop = catalog.PROPS.get(key)
        if prop is None:
            raise ValueError(f"{key} não existe no catálogo")
        if not prop.writable:
            raise ValueError(f"{key} ({prop.name}) não é gravável")
        if prop.format == "bool":
            if isinstance(value, str):
                value = value.lower() in ("1", "true", "on", "sim")
            return bool(value)
        if prop.format == "string":
            return str(value)
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise ValueError(f"{key} espera inteiro")
        value = int(value)
        if prop.values and value not in {v for v, _ in prop.values}:
            raise ValueError(f"{key}: {value} fora das opções {[v for v, _ in prop.values]}")
        if prop.range:
            lo, hi, step = prop.range
            if not lo <= value <= hi or (value - lo) % (step or 1):
                raise ValueError(f"{key}: {value} fora da faixa {lo}..{hi} (passo {step})")
        return value

    def set(self, key: str, value) -> dict:
        """Grava uma prop validada. Devolve {"code": 0, "value": <valor gravado>}."""
        value = self.validate(key, value)
        s, p = _pair(key)
        res = self.client.send("set_properties", [{"did": key, "siid": s, "piid": p, "value": value}])
        code = res[0].get("code") if res else None
        return {"code": code, "value": value}

    # -- ações ----------------------------------------------------------------
    def action(self, key: str, params: list | None = None) -> dict:
        """Executa siid/aiid. `params` = valores na ordem dos piids de entrada da spec."""
        act = catalog.ACTIONS.get(key)
        if act is None:
            raise ValueError(f"ação {key} não existe na spec")
        params = list(params or [])
        if len(params) != len(act.params):
            raise ValueError(f"ação {key} ({act.name}) espera {len(act.params)} parâmetro(s)")
        ins = [{"piid": piid, "value": v} for piid, v in zip(act.params, params)]
        s, a = _pair(key)
        try:
            return self.client.send("action", {"did": key, "siid": s, "aiid": a, "in": ins})
        except MiioDeviceError as ex:
            return {"code": ex.code, "error": str(ex)}

    def run(self, name: str) -> dict:
        """Ação simples pelo nome (catalog.SIMPLE_ACTIONS): start, home, dust…"""
        s, a = catalog.SIMPLE_ACTIONS[name]
        return self.action(f"{s}/{a}")

    def clean_rooms(self, ids: Iterable[int]) -> dict:
        """Ação 2/16 com os ids separados por vírgula: [6, 7] -> "6,7"."""
        ids = [int(i) for i in ids]
        if not ids:
            raise ValueError("informe ao menos um cômodo")
        return self.action("2/16", [",".join(map(str, ids))])

    def clean_zones(self, zones: list) -> dict:
        """Ação 2/37 (start-zone-sweep) com as zonas em mm. Formato do parâmetro em teste (ver decoders.zone_param)."""
        return self.action("2/37", [decoders.zone_param(zones)])

    def reset_consumable(self, item: str) -> dict:
        s, a = catalog.CONSUMABLES[item]
        return self.action(f"{s}/{a}")

    def info(self) -> dict:
        return self.client.send("miIO.info")
