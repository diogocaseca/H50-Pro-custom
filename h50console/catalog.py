"""Catálogo do H50 Pro (xiaomi.vacuum.ov42gl): spec oficial + o que foi observado no aparelho.

Fonte primária: spec/ov42gl-2.json (miot-spec.org, versão 2, a mais recente).
Por cima dela, este módulo aplica o que o app da Xiaomi faz e a spec não diz
(PLANO-MVP.md §6): rótulos do app, props fora da spec, variantes de hardware,
ordem de opções, grupos de polling e tamanho das respostas (limite de ~1,4 KB por UDP).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

MODEL = "xiaomi.vacuum.ov42gl"
SPEC_FILE = Path(__file__).with_name("spec") / "ov42gl-2.json"


@dataclass(frozen=True)
class Prop:
    siid: int
    piid: int
    name: str                  # nome da spec (ex.: "drying-time")
    service: str               # ex.: "vacuum"
    format: str                # bool, uint8, uint16, uint32, int32, string
    access: tuple[str, ...]    # read/write/notify
    values: tuple[tuple[int, str], ...] = ()   # value-list: (valor, descrição)
    range: tuple[int, int, int] | None = None  # (min, max, passo)
    unit: str | None = None
    in_spec: bool = True

    @property
    def key(self) -> str:
        return f"{self.siid}/{self.piid}"

    @property
    def readable(self) -> bool:
        return "read" in self.access

    @property
    def writable(self) -> bool:
        return "write" in self.access

    @property
    def is_json(self) -> bool:
        return self.key in JSON_PROPS


@dataclass(frozen=True)
class Action:
    siid: int
    aiid: int
    name: str
    service: str
    params: tuple[int, ...] = ()   # piids de entrada
    out: tuple[int, ...] = ()

    @property
    def key(self) -> str:
        return f"{self.siid}/{self.aiid}"


def _short(urn: str) -> str:
    # urn:miot-spec-v2:property:drying-time:0000...:xiaomi-ov42gl:1 -> drying-time
    return urn.split(":")[3]


def _load_spec(path: Path = SPEC_FILE) -> tuple[dict[str, Prop], dict[str, Action]]:
    spec = json.loads(path.read_text(encoding="utf-8"))
    props: dict[str, Prop] = {}
    actions: dict[str, Action] = {}
    for svc in spec["services"]:
        sname = _short(svc["type"])
        for p in svc.get("properties", []):
            prop = Prop(
                siid=svc["iid"], piid=p["iid"], name=_short(p["type"]), service=sname,
                format=p.get("format", "string"), access=tuple(p.get("access", [])),
                values=tuple((v["value"], v["description"]) for v in p.get("value-list", [])),
                range=tuple(p["value-range"]) if "value-range" in p else None,
                unit=p.get("unit"),
            )
            props[prop.key] = prop
        for a in svc.get("actions", []):
            act = Action(siid=svc["iid"], aiid=a["iid"], name=_short(a["type"]), service=sname,
                         params=tuple(a.get("in", [])), out=tuple(a.get("out", [])))
            actions[act.key] = act
    return props, actions


# ---------------------------------------------------------------------------
# Props fora da spec, encontradas com `probe scan` (o firmware é mais novo que a spec)
# ---------------------------------------------------------------------------
_EXTRA = [
    Prop(23, 1, "room-surface", "x-rooms", "string", ("read",), in_spec=False),  # cômodos + tipo de piso
    Prop(23, 2, "x-23-2", "x-rooms", "string", ("read",), in_spec=False),
    Prop(23, 3, "x-23-3", "x-rooms", "string", ("read",), in_spec=False),
    *[Prop(2, p, f"x-2-{p}", "vacuum", "string", ("read",), in_spec=False) for p in (103, 104, 105, 106)],
    *[Prop(21, p, f"x-21-{p}", "x-21", "string", ("read",), in_spec=False) for p in range(1, 9)],
    Prop(20, 4, "x-20-4", "custom", "string", ("read",), in_spec=False),
]

PROPS, ACTIONS = _load_spec()
for _p in _EXTRA:
    PROPS.setdefault(_p.key, _p)

# props que são strings com JSON dentro (D4)
JSON_PROPS = {"2/1", "2/16", "2/18", "2/19", "2/40", "2/42", "2/66", "2/67", "2/102", "10/1", "10/2",
              "10/3", "10/5", "10/13", "20/3", "23/1", "23/2", "23/3"}

# piids marcados como leitura na spec que, na prática, são só parâmetro de ação (não respondem a get)
_NOT_READABLE = {"2/15", "2/24", "2/39", "2/65"}
READABLE = [k for k, p in PROPS.items() if p.readable and k not in _NOT_READABLE] + \
           [p.key for p in _EXTRA]
READABLE = list(dict.fromkeys(READABLE))  # sem duplicatas, ordem estável

# ---------------------------------------------------------------------------
# Tamanho das respostas (bytes do item JSON), medido com `probe dump`.
# O robô não entrega resposta UDP > ~1,4 KB; device.py monta lotes por este orçamento.
# ---------------------------------------------------------------------------
SIZE_DEFAULT = 70
SIZE_HINT = {
    "2/1": 110, "2/16": 330, "2/18": 90, "2/19": 260, "2/40": 450, "2/42": 450, "2/66": 120,
    "2/67": 120, "2/102": 110, "10/1": 140, "10/2": 140, "10/3": 1500, "10/5": 230, "10/13": 230,
    "10/15": 400, "20/3": 140, "22/2": 400, "23/1": 330, "1/5": 90,
}
# itens que, sozinhos, passam do limite: não adianta pedir (P7)
OVERSIZED = {"10/3"}

# ---------------------------------------------------------------------------
# Enums em português. Onde o app usa um texto diferente da spec, vale o app.
# ---------------------------------------------------------------------------
STATUS = {
    1: "Parado", 2: "Carregando", 3: "Carregando para retomar", 4: "Limpando", 5: "Pausado",
    6: "Voltando para a base", 7: "Lavando o mop", 8: "Controle remoto", 9: "Carga completa",
    10: "Mapeando", 11: "Atualizando firmware", 12: "Base trabalhando (limpeza)",
    13: "Trocando de tarefa", 14: "Base trabalhando (carga)", 15: "Anormal",
    16: "Aspirando e passando pano", 17: "Passando pano", 18: "Mapeamento pausado",
    19: "Voltando para retomar", 20: "Voltando no meio da rota", 21: "Voltando do mapeamento",
    22: "Enxágue auxiliar da base", 23: "Lavagem auxiliar concluída", 24: "Voltando para carregar",
}
# status em que o robô está trabalhando (poll rápido)
ACTIVE_STATUS = {4, 5, 6, 7, 10, 12, 13, 16, 17, 18, 19, 20, 21, 22, 24}
# status em que NÃO se deve iniciar limpeza (spec §8)
BLOCK_START = {7: "lavando o mop", 11: "atualizando firmware", 12: "base trabalhando", 14: "base trabalhando"}


def start_block_reason(values: dict) -> str | None:
    """Motivo para NÃO iniciar limpeza agora, ou None.

    Com status 14, o que pesa é o que a base faz (2/18): lavando o mop (3) ou esvaziando o pó (2)
    bloqueiam; secando (1, até 4 h) ou ociosa não, como no app Xiaomi Home.
    """
    status = values.get("2/2")
    if status == 14:
        try:
            mode = json.loads(values.get("2/18") or "{}").get("mode", 0)
        except (TypeError, ValueError):
            mode = 0
        return {2: "base esvaziando o pó", 3: "base lavando o mop"}.get(mode)
    return BLOCK_START.get(status)

ENUM_PT: dict[str, dict[int, str]] = {
    "2/2": STATUS,
    # textos do app Xiaomi Home (prints de 11/10/2026)
    "2/4": {1: "Aspirando", 2: "Esfregando", 3: "Aspirando e esfregando", 4: "Aspirar antes de esfregar"},
    "2/5": {1: "Casa toda", 2: "Zona", 3: "Cômodos", 4: "Bordas", 5: "Personalizada", 6: "Ponto",
            7: "Personalizada", 8: "Agendada", 9: "Vinculada", 10: "Mapeamento rápido", 11: "Gerenciada por IA"},
    "2/8": {1: "Uma vez", 2: "Duas vezes", 3: "Três vezes"},
    "2/9": {1: "Silencioso", 2: "Padrão", 3: "Intenso", 4: "Turbo"},
    "2/10": {0: "Desligada", 1: "Nível 1", 2: "Nível 2", 3: "Nível 3"},
    "2/33": {1: "Uma vez", 2: "Duas vezes", 3: "Três vezes"},
    "2/29": {0: "Por cômodo", 1: "Por área", 2: "Por tempo"},
    "2/30": {0: "Minuciosa", 1: "Diária", 2: "Economia"},
    "2/31": {1: "2 h", 2: "3 h", 3: "4 h"},
    "2/61": {0: "5 m²", 1: "10 m²", 2: "8 m²", 3: "15 m²", 4: "20 m²", 5: "25 m²"},
    "2/68": {1: "A cada 7 limpezas", 2: "Toda limpeza"},
    "2/73": {0: "Adaptativo", 1: "Evitar", 2: "Ignorar", 3: "Atravessar"},
    "2/74": {1: "Rápido", 2: "Padrão", 3: "Profunda"},
    "2/75": {0: "Menos colisões", 1: "Mais cobertura"},
    "2/80": {1: "A cada 7 limpezas", 2: "Toda limpeza"},
    "2/82": {0: "Silencioso", 1: "Diário", 2: "Forte"},
    "2/83": {0: "Limpeza de baixo ruído", 1: "Limpeza minuciosa", 2: "Configurações gerais", 3: "Mordomo", 4: "Pet"},
    "2/84": {0: "Ambiente", 1: "Morna", 2: "Quente", 3: "Inteligente"},
    "3/2": {1: "Carregando", 2: "Não carregando", 3: "Não carregável"},
    "18/3": {0: "Pouco", 1: "Padrão", 2: "Muito", 3: "Extra"},
}
# ordem de exibição das opções, quando a ordem dos códigos não é a natural
ENUM_ORDER = {"2/61": [0, 2, 1, 3, 4, 5],   # 5, 8, 10, 15, 20, 25 m²
              "2/4": [1, 3, 4, 2],            # ordem do app: Aspirando … Esfregando
              "2/83": [2, 0, 1, 3, 4]}        # ordem do app: Configurações gerais, baixo ruído, minuciosa

# ---------------------------------------------------------------------------
# Ajustes exibidos na tela "Ajustes": (prop, grupo, rótulo do app).
# ✔ = confirmado observando o app (PLANO-MVP.md §6.1).
# ---------------------------------------------------------------------------
SETTINGS: list[tuple[str, str, str]] = [
    ("2/4", "Limpeza", "Modo de limpeza"),
    ("2/9", "Limpeza", "Potência de sucção"),
    ("2/10", "Limpeza", "Nível de água do mop"),
    ("2/8", "Limpeza", "Número de passadas"),
    ("2/74", "Limpeza", "Rota de limpeza"),
    ("2/75", "Limpeza", "Estratégia de obstáculos"),
    ("2/79", "Limpeza", "Foco em cantos e bordas"),
    ("2/80", "Limpeza", "Foco em cantos e bordas: frequência"),  # ✔
    ("2/83", "Limpeza", "Modo sem preocupação"),
    ("2/77", "Tapete", "Reconhecimento de tapete"),               # ✔
    ("2/73", "Tapete", "Limpeza de tapete"),
    ("2/76", "Tapete", "Limpeza profunda de tapete"),
    ("2/78", "Tapete", "Aspirar tapete primeiro"),
    ("2/20", "Tapete", "Turbo no tapete"),
    ("2/28", "Base", "Lavar o mop"),
    ("2/61", "Base", "Frequência de lavagem do mop"),             # ✔ (sem troca automática de água)
    ("2/29", "Base", "Frequência de lavagem do mop"),             # (com troca automática de água)
    ("2/81", "Base", "Lavagem do mop por tempo (min)"),
    ("2/30", "Base", "Modo de lavagem do mop"),                   # ✔
    ("2/84", "Base", "Temperatura da água de lavagem"),
    ("2/41", "Base", "Lavagem com água quente"),
    ("2/34", "Base", "Secagem automática"),
    ("2/31", "Base", "Duração da secagem"),                       # ✔
    ("2/32", "Base", "Esvaziamento automático do pó"),
    ("2/33", "Base", "Frequência do esvaziamento"),
    ("2/82", "Base", "Potência do esvaziamento"),
    ("2/36", "Base", "Usar detergente"),
    ("18/3", "Base", "Dosagem do detergente"),
    ("4/2", "Sistema", "Volume da voz"),                          # ✔
    ("5/1", "Sistema", "Trava para crianças"),
    ("11/1", "Sistema", "Não perturbe"),                          # ✔
    ("11/2", "Sistema", "Não perturbe: horário"),                 # ✔
    ("2/23", "Sistema", "Retomar limpeza após recarga"),
]

# Variantes de hardware: o app escolhe qual prop mostrar conforme outra prop.
# 2/62 auto-water-change-installed = False (caso do aparelho) -> 2/61 em vez de 2/29 + 2/81.
VARIANTS = {
    "2/62": {False: {"hide": {"2/29", "2/81"}}, True: {"hide": {"2/61"}}},
}


def visible_settings(state: dict) -> list[tuple[str, str, str]]:
    """Filtra SETTINGS conforme as variantes de hardware do estado atual."""
    hidden: set[str] = set()
    for key, rules in VARIANTS.items():
        if key in state and state[key] in rules:
            hidden |= rules[state[key]]["hide"]
    return [s for s in SETTINGS if s[0] not in hidden]


def options(key: str) -> list[tuple[int, str]]:
    """Opções de um enum, já em português e na ordem de exibição."""
    prop = PROPS[key]
    pt = ENUM_PT.get(key, {})
    opts = [(v, pt.get(v, d)) for v, d in prop.values] or list(pt.items())
    order = ENUM_ORDER.get(key)
    if order:
        opts.sort(key=lambda o: order.index(o[0]) if o[0] in order else 99)
    return opts


def label(key: str, value) -> str:
    """Valor legível: enum traduzido, ou o próprio valor."""
    if isinstance(value, bool) or not isinstance(value, int):
        return str(value)
    if value in ENUM_PT.get(key, {}):
        return ENUM_PT[key][value]
    prop = PROPS.get(key)
    return dict(prop.values).get(value, str(value)) if prop else str(value)


# ---------------------------------------------------------------------------
# Ações usadas pela UI: nome -> (siid, aiid). Os nomes coincidem com h50pro.py.
# ---------------------------------------------------------------------------
SIMPLE_ACTIONS = {
    "start": (2, 1), "stop": (2, 2), "home": (2, 3), "pause": (2, 7), "resume": (2, 8),
    "sweep": (2, 4), "mop": (2, 5), "sweep-mop": (2, 6), "sweep-then-mop": (2, 50),
    "build-map": (2, 17), "dust": (2, 18), "wash-mop": (2, 19), "dry": (2, 20),
    "stop-wash": (2, 31), "stop-dry": (2, 32), "try-listen": (2, 27), "spot": (2, 59),
    "self-clean-base": (2, 63), "charge": (3, 1), "locate": (6, 1),
}
# ações que tiram o robô da base: pedem confirmação na UI
LEAVES_BASE = {"start", "resume", "sweep", "mop", "sweep-mop", "sweep-then-mop", "build-map", "spot"}
# reset de consumíveis: item -> (siid, aiid)
CONSUMABLES = {
    "mop": (9, 1), "main_brush": (12, 1), "side_brush": (13, 1), "filter": (14, 1),
    "detergent": (18, 1), "dust_bag": (19, 1),
}
CONSUMABLE_PROPS = {  # item -> (prop de %, prop de horas restantes)
    "mop": ("9/1", "9/2"), "main_brush": ("12/1", "12/2"), "side_brush": ("13/1", "13/2"),
    "filter": ("14/1", "14/2"), "detergent": ("18/1", "18/4"), "dust_bag": ("19/1", "19/2"),
}

# Móveis (2/102 "type"). 1740 confirmado removendo a liteira no app; 1220/1400 inferidos pela
# posição (sofá fora da borda da Sala -> rid 248; mesa dentro -> rid 3; 1400 = mesa, confirmado pelo dono). rid 248 = fora dos cômodos.
FURNITURE_TYPES = {1220: "Sofá", 1400: "Mesa", 1740: "Liteira"}
FURNITURE_OUTSIDE_RID = 248

# ---------------------------------------------------------------------------
# Linhas da tabela "Estado" do Painel: (prop, rótulo, formato). Formatos tratados em web.py.
# ---------------------------------------------------------------------------
STATE_ROWS: list[tuple[str, str, str]] = [
    ("2/2", "Status", "enum"),
    ("3/1", "Bateria", "pct"),
    ("3/2", "Carga", "enum"),
    ("2/89", "Progresso da limpeza", "progress"),
    ("2/6", "Área limpa", "area"),
    ("2/7", "Tempo de limpeza", "seconds"),
    ("2/5", "Tipo de limpeza", "enum"),
    ("2/40", "Limpando agora", "rooms"),
    ("2/96", "Escova/mop em uso", "raw"),
    ("2/18", "Base", "base"),
    ("2/88", "Progresso da secagem", "drying"),         # calculado com 2/31 e 2/90 (ver decoders)
    ("2/90", "Secagem restante", "minutes"),
    ("2/97", "Tanque de água suja cheio", "bool"),
    ("2/98", "Tanque de água limpa vazio", "bool"),
    ("2/3", "Falha", "fault"),
    ("2/66", "Falhas ativas", "faults"),
    ("2/17", "Início da última limpeza", "epoch"),
    ("11/3", "Não perturbe ativo agora", "bool"),
    ("2/26", "Localizando no mapa", "bool"),
]

# Propriedades que mudam sozinhas durante a limpeza (escondidas por padrão na observação de mudanças)
NOISY_PROPS = {"2/6", "2/7", "2/89", "3/1", "2/88", "2/90", "10/1", "10/2", "5/2", "2/26"}

_ACT_DESTRUCTIVE = ("clear", "delete", "restore", "merge", "split", "reset", "modify", "save", "set-", "add-")
_ACT_PHYSICAL = ("start", "continue", "gocharge", "sweep", "clean", "zone", "remote", "charge", "build-map", "spot",
                 "furniture", "eject", "cut-hair", "wash", "dry", "dust", "stop", "pause", "identify", "listen")


def action_risk(name: str) -> str:
    """safe = só consulta · acts = faz algo físico ou não se sabe o que faz (prudência) · destructive = altera/apaga
    configuração salva. Consultas (`get-…`) são avaliadas primeiro: `get-zone-configs` não move nada."""
    n = name.lower()
    if n.startswith(("get-", "query-", "check-")):
        return "safe"
    if any(w in n for w in _ACT_DESTRUCTIVE):
        return "destructive"
    return "acts"


# Grupos de status para a linha do tempo do dashboard (ordem = ordem das cores da paleta)
STATUS_GROUPS: list[tuple[str, set[int]]] = [
    ("Limpando", {4, 10, 16, 17, 18}),
    ("Voltando", {6, 19, 20, 21, 24}),
    ("Base trabalhando", {7, 12, 14, 22, 23}),
    ("Pausado", {5}),
    ("Na base", {2, 3, 9}),
    ("Outros", {1, 8, 11, 13, 15}),
]


def status_group(status) -> str:
    return next((g for g, codes in STATUS_GROUPS if status in codes), "Outros")


# ---------------------------------------------------------------------------
# Grupos de polling (spec §5). Intervalos em segundos: (ativo, ocioso).
# ---------------------------------------------------------------------------
POLL_GROUPS: dict[str, tuple[list[str], tuple[int, int]]] = {
    "fast": (["2/2", "2/3", "2/96", "2/89", "2/6", "2/7", "3/1", "3/2", "2/18", "2/88", "2/90",
              "2/97", "2/98", "2/5", "2/26", "2/40", "2/66", "2/67", "2/17", "11/3"], (5, 30)),
    # ajustes: o usuário pode mudar pelo app oficial a qualquer momento; ~45 props = 3 lotes (<200 ms)
    "settings": ([s[0] for s in SETTINGS] + ["2/62", "2/68", "2/21", "2/22"], (30, 30)),
    # o que se edita no app e é maior (JSON): rotinas, agenda, móveis, mapa
    "edits": (["2/42", "2/19", "2/102", "10/13", "10/1", "10/2"], (60, 60)),
    "consumables": ([k for pair in CONSUMABLE_PROPS.values() for k in pair], (1800, 1800)),
    "structure": (["2/16", "10/5", "10/6", "2/1", "23/1", "15/2"], (3600, 3600)),
}
