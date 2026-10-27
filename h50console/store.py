"""Persistência em SQLite (biblioteca padrão). Uma conexão, protegida por lock; modo WAL.

Migrações: lista MIGRATIONS, versão em PRAGMA user_version (sem Alembic).
Datas gravadas como texto ISO-8601 em UTC.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

MIGRATIONS = [
    # v1: esquema da spec §7, com extras para o histórico derivado (P7) e ajustes do app
    """
    CREATE TABLE prop_change (
      id        INTEGER PRIMARY KEY,
      ts        TEXT NOT NULL,
      prop      TEXT NOT NULL,
      old_value TEXT,
      new_value TEXT
    );
    CREATE INDEX ix_prop_change_prop_ts ON prop_change (prop, ts);

    CREATE TABLE cleaning_record (
      stime       INTEGER PRIMARY KEY,      -- epoch UTC do início (2/17 ou stime do 10/3)
      duration_s  INTEGER,
      area_m2     REAL,
      sweep_type  INTEGER,
      rooms       TEXT,                     -- JSON [ids], quando conhecido (2/40)
      source      TEXT NOT NULL,            -- 'robot' (10/3) | 'derived' (2/17 + 2/6 + 2/7)
      raw_label   TEXT,
      updated_at  TEXT NOT NULL
    );

    CREATE TABLE consumable_snapshot (
      day   TEXT NOT NULL,
      item  TEXT NOT NULL,
      pct   INTEGER,
      hours INTEGER,
      PRIMARY KEY (day, item)
    );

    CREATE TABLE command_log (
      id          INTEGER PRIMARY KEY,
      ts          TEXT NOT NULL,
      source      TEXT NOT NULL,            -- ui | api | scheduler
      command     TEXT NOT NULL,            -- action:2/16 | set:2/9 | run:home
      params      TEXT,
      result_code TEXT,
      latency_ms  INTEGER,
      error       TEXT
    );
    CREATE INDEX ix_command_log_ts ON command_log (ts);

    CREATE TABLE fault_event (
      id      INTEGER PRIMARY KEY,
      ts      TEXT NOT NULL,
      code    INTEGER NOT NULL,
      cleared TEXT
    );

    CREATE TABLE room (
      map_uid INTEGER NOT NULL,
      id      INTEGER NOT NULL,
      name    TEXT NOT NULL,
      surface INTEGER,
      PRIMARY KEY (map_uid, id)
    );

    CREATE TABLE app_setting (
      key   TEXT PRIMARY KEY,
      value TEXT NOT NULL
    );
    """,
    # v2: Laboratório (anotações para ligar "mudei X no app" a propriedades; propriedades novas achadas na varredura)
    """
    CREATE TABLE note (
      id   INTEGER PRIMARY KEY,
      ts   TEXT NOT NULL,
      text TEXT NOT NULL
    );
    CREATE TABLE discovery (
      key        TEXT PRIMARY KEY,
      first_seen TEXT NOT NULL,
      last_seen  TEXT NOT NULL,
      last_value TEXT
    );
    """,
    # v3: caminho percorrido em cada limpeza (pontos [x, y, tipo] em mm, do mapa da nuvem)
    """
    CREATE TABLE cleaning_path (
      stime      INTEGER PRIMARY KEY,
      points     TEXT NOT NULL,
      n_points   INTEGER NOT NULL,
      final      INTEGER NOT NULL DEFAULT 0,
      updated_at TEXT NOT NULL
    );
    """,
    # v4: por onde o comando foi (lan | cloud)
    """
    ALTER TABLE command_log ADD COLUMN via TEXT;
    """,
]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or utcnow()).isoformat(timespec="seconds")


def _txt(v) -> str | None:
    """Valor de prop -> texto (JSON para não-strings, para distinguir 1 de "1" e true)."""
    if v is None:
        return None
    return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)


class Store:
    def __init__(self, path: str | Path = "data/h50.db"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA foreign_keys=ON")
            self._migrate()

    def _migrate(self) -> None:
        ver = self.db.execute("PRAGMA user_version").fetchone()[0]
        for i, sql in enumerate(MIGRATIONS[ver:], start=ver + 1):
            self.db.executescript(f"BEGIN; {sql}; PRAGMA user_version = {i}; COMMIT;")

    @property
    def version(self) -> int:
        return self.db.execute("PRAGMA user_version").fetchone()[0]

    def _exec(self, sql: str, args=()) -> sqlite3.Cursor:
        with self.lock:
            return self.db.execute(sql, args)

    def _query(self, sql: str, args=()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def close(self) -> None:
        with self.lock:
            self.db.close()

    # -- mudanças de propriedade -------------------------------------------------
    def save_changes(self, changes: dict[str, tuple[object, object]], ts: datetime | None = None) -> None:
        """changes = {"2/2": (antigo, novo)}; grava só o que mudou (quem chama já fez o diff)."""
        if not changes:
            return
        t = iso(ts)
        rows = [(t, k, _txt(old), _txt(new)) for k, (old, new) in changes.items()]
        with self.lock:
            self.db.execute("BEGIN")
            self.db.executemany("INSERT INTO prop_change (ts, prop, old_value, new_value) VALUES (?,?,?,?)", rows)
            self.db.execute("COMMIT")

    def prop_history(self, prop: str, limit: int = 200) -> list[dict]:
        return self._query("SELECT ts, old_value, new_value FROM prop_change WHERE prop=? ORDER BY id DESC LIMIT ?",
                           (prop, limit))

    def purge_changes(self, props: list[str] | None, days: int = 90) -> int:
        """Retenção: apaga mudanças de propriedades antigas (`props=None` = todas). O histórico de limpezas,
        os consumíveis e as falhas nunca são apagados por aqui."""
        cutoff = iso(utcnow() - timedelta(days=days))
        if props is None:
            return self._exec("DELETE FROM prop_change WHERE ts < ?", (cutoff,)).rowcount
        if not props:
            return 0
        marks = ",".join("?" * len(props))
        return self._exec(f"DELETE FROM prop_change WHERE ts < ? AND prop IN ({marks})", (cutoff, *props)).rowcount

    def changes_since(self, since_iso: str, limit: int = 600) -> list[dict]:
        return self._query("SELECT ts, prop, old_value, new_value FROM prop_change WHERE ts >= ? ORDER BY id DESC LIMIT ?",
                           (since_iso, limit))

    def add_note(self, text: str) -> None:
        self._exec("INSERT INTO note (ts, text) VALUES (?, ?)", (iso(), text))

    def notes(self, since_iso: str = "", limit: int = 200) -> list[dict]:
        return self._query("SELECT id, ts, text FROM note WHERE ts >= ? ORDER BY id DESC LIMIT ?", (since_iso, limit))

    def upsert_discovery(self, key: str, value) -> None:
        now = iso()
        self._exec("""INSERT INTO discovery (key, first_seen, last_seen, last_value) VALUES (?,?,?,?)
                      ON CONFLICT(key) DO UPDATE SET last_seen=excluded.last_seen, last_value=excluded.last_value""",
                   (key, now, now, _txt(value)))

    def discoveries(self) -> list[dict]:
        return self._query("SELECT key, first_seen, last_seen, last_value FROM discovery ORDER BY key")

    def purge_commands(self, days: int = 365) -> int:
        return self._exec("DELETE FROM command_log WHERE ts < ?", (iso(utcnow() - timedelta(days=days)),)).rowcount

    # -- histórico de limpezas ---------------------------------------------------
    def upsert_cleaning(self, stime: int, duration_s: int | None, area_m2: float | None, sweep_type: int | None,
                        source: str, rooms: list[int] | None = None, raw_label: str | None = None) -> None:
        """Idempotente por stime. Um registro 'robot' nunca é sobrescrito por um 'derived', e um valor
        desconhecido (None) nunca apaga um conhecido (ex.: ao reiniciar o app, o tipo já foi zerado)."""
        self._exec(
            """INSERT INTO cleaning_record (stime, duration_s, area_m2, sweep_type, rooms, source, raw_label, updated_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(stime) DO UPDATE SET
                 duration_s=COALESCE(excluded.duration_s, cleaning_record.duration_s),
                 area_m2=COALESCE(excluded.area_m2, cleaning_record.area_m2),
                 sweep_type=COALESCE(excluded.sweep_type, cleaning_record.sweep_type),
                 rooms=COALESCE(excluded.rooms, cleaning_record.rooms), source=excluded.source,
                 raw_label=COALESCE(excluded.raw_label, cleaning_record.raw_label), updated_at=excluded.updated_at
               WHERE NOT (cleaning_record.source='robot' AND excluded.source='derived')""",
            (stime, duration_s, area_m2, sweep_type, json.dumps(rooms) if rooms else None, source, raw_label, iso()),
        )

    def cleanings(self, start: int | None = None, end: int | None = None, sweep_type: int | None = None) -> list[dict]:
        sql, args = "SELECT * FROM cleaning_record WHERE 1=1", []
        if start is not None:
            sql += " AND stime >= ?"; args.append(start)
        if end is not None:
            sql += " AND stime < ?"; args.append(end)
        if sweep_type is not None:
            sql += " AND sweep_type = ?"; args.append(sweep_type)
        rows = self._query(sql + " ORDER BY stime DESC", args)
        for r in rows:
            r["rooms"] = json.loads(r["rooms"]) if r["rooms"] else []
        return rows

    # -- caminho percorrido ----------------------------------------------------------
    def save_path(self, stime: int, points: list, final: bool = False) -> None:
        """Grava (ou troca) o caminho da limpeza. Um caminho final nunca é trocado por um parcial."""
        self._exec(
            """INSERT INTO cleaning_path (stime, points, n_points, final, updated_at) VALUES (?,?,?,?,?)
               ON CONFLICT(stime) DO UPDATE SET points=excluded.points, n_points=excluded.n_points,
                 final=excluded.final, updated_at=excluded.updated_at
               WHERE excluded.final=1 OR cleaning_path.final=0""",
            (stime, json.dumps(points, separators=(",", ":")), len(points), int(final), iso()))

    def get_path(self, stime: int) -> dict | None:
        rows = self._query("SELECT stime, points, n_points, final, updated_at FROM cleaning_path WHERE stime=?", (stime,))
        if not rows:
            return None
        r = rows[0]
        return {"stime": r["stime"], "points": json.loads(r["points"]), "final": bool(r["final"]), "updated_at": r["updated_at"]}

    def paths_since(self, start: int) -> list[dict]:
        rows = self._query("SELECT stime, points FROM cleaning_path WHERE stime >= ? AND n_points > 1 ORDER BY stime", (start,))
        return [{"stime": r["stime"], "points": json.loads(r["points"])} for r in rows]

    def path_stimes(self) -> set[int]:
        return {r["stime"] for r in self._query("SELECT stime FROM cleaning_path WHERE n_points > 1")}

    def move_path(self, old: int, new: int) -> None:
        """Passa o caminho gravado durante a limpeza (chave provisória) para o início oficial (2/17)."""
        if old != new:
            self._exec("DELETE FROM cleaning_path WHERE stime=? AND EXISTS (SELECT 1 FROM cleaning_path WHERE stime=?)", (old, new))
            self._exec("UPDATE OR IGNORE cleaning_path SET stime=? WHERE stime=?", (new, old))

    # -- consumíveis -------------------------------------------------------------
    def snapshot_consumables(self, items: dict[str, tuple[int | None, int | None]], day: str | None = None) -> None:
        day = day or utcnow().astimezone().date().isoformat()
        with self.lock:
            self.db.execute("BEGIN")
            self.db.executemany("INSERT OR REPLACE INTO consumable_snapshot (day, item, pct, hours) VALUES (?,?,?,?)",
                                [(day, k, p, h) for k, (p, h) in items.items()])
            self.db.execute("COMMIT")

    def consumable_history(self, item: str) -> list[dict]:
        return self._query("SELECT day, pct, hours FROM consumable_snapshot WHERE item=? ORDER BY day", (item,))

    # -- comandos ------------------------------------------------------------------
    def log_command(self, source: str, command: str, params, result_code, latency_ms: int | None,
                    error: str | None = None, via: str | None = None) -> int:
        return self._exec(
            "INSERT INTO command_log (ts, source, command, params, result_code, latency_ms, error, via) VALUES (?,?,?,?,?,?,?,?)",
            (iso(), source, command, _txt(params), _txt(result_code), latency_ms, error, via),
        ).lastrowid

    def commands(self, limit: int = 200) -> list[dict]:
        return self._query("SELECT * FROM command_log ORDER BY id DESC LIMIT ?", (limit,))

    # -- falhas --------------------------------------------------------------------
    def open_fault(self, code: int) -> None:
        """Abre um evento se ainda não houver um aberto para o código."""
        with self.lock:
            open_ = self.db.execute("SELECT 1 FROM fault_event WHERE code=? AND cleared IS NULL", (code,)).fetchone()
            if not open_:
                self.db.execute("INSERT INTO fault_event (ts, code) VALUES (?,?)", (iso(), code))

    def clear_faults(self, keep: set[int] = frozenset()) -> None:
        """Fecha os eventos abertos cujo código não está mais ativo."""
        with self.lock:
            rows = self.db.execute("SELECT id, code FROM fault_event WHERE cleared IS NULL").fetchall()
            for r in rows:
                if r["code"] not in keep:
                    self.db.execute("UPDATE fault_event SET cleared=? WHERE id=?", (iso(), r["id"]))

    def faults(self, limit: int = 200) -> list[dict]:
        return self._query("SELECT * FROM fault_event ORDER BY id DESC LIMIT ?", (limit,))

    # -- cômodos e ajustes do app --------------------------------------------------
    def replace_rooms(self, map_uid: int, rooms: list[dict], surfaces: dict[int, int] | None = None) -> None:
        surfaces = surfaces or {}
        with self.lock:
            self.db.execute("BEGIN")
            self.db.execute("DELETE FROM room WHERE map_uid=?", (map_uid,))
            self.db.executemany("INSERT INTO room (map_uid, id, name, surface) VALUES (?,?,?,?)",
                                [(map_uid, r["id"], r["name"], surfaces.get(r["id"])) for r in rooms])
            self.db.execute("COMMIT")

    def rooms(self, map_uid: int | None = None) -> list[dict]:
        if map_uid is None:
            return self._query("SELECT * FROM room ORDER BY map_uid, id")
        return self._query("SELECT * FROM room WHERE map_uid=? ORDER BY id", (map_uid,))

    def get_setting(self, key: str, default=None):
        row = self._query("SELECT value FROM app_setting WHERE key=?", (key,))
        return json.loads(row[0]["value"]) if row else default

    def set_setting(self, key: str, value) -> None:
        self._exec("INSERT OR REPLACE INTO app_setting (key, value) VALUES (?,?)", (key, json.dumps(value)))
