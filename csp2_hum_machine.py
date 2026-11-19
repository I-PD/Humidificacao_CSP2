#!/usr/bin/env python3
"""Recolha FINS/TCP da humidificação CSP2 — Python >= 3.11.

Lê EM0_0..20 e o histórico E0_1000..9999 a cada 15 segundos; grava alterações.
Uma linha por (operation_id, inicio), com programa associado e fins provisórios
anteriores ao início. O PLC pode exportar resultados de momentos diferentes;
a tabela representa os últimos valores observados, sem inferir dados perdidos.

Horário Europe/Lisbon: segunda a sexta 06:00–20:00; sábado 08:00–08:30
para confirmar atividade, prolongando até às 20:00 se houver início/fim desse
dia. Domingo e restantes horários: ligação PLC fechada, processo em espera.
A fila SQLite durável mantém alterações durante falhas de PostgreSQL.

    python csp2_hum_machine.py --print-schema
    python csp2_hum_machine.py --init-db
    python csp2_hum_machine.py --ignore-schedule  # monitorização sem gravação
    python csp2_hum_machine.py --write-db

Configuração: .env junto ao script ou --env-file; variáveis do ambiente têm
prioridade. Ver .env.example e README.md. Uma instância e um volume para esta máquina.
--init-db cria a estrutura, sem migrar tabelas de versões anteriores.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import math
import os
import signal
import sqlite3
import struct
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

LOG = logging.getLogger("csp2_hum_machine")
WORD_COUNT = 21
DATA_OFFSET = 14  # Cabeçalho da resposta FINS, SEM o envelope FINS/TCP.

# O SQL autónomo é também a única fonte do DDL usado por --init-db.
SCHEMA_SQL = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
KEY_COLUMNS = ("operation_id", "inicio")
RESULT_COLUMNS = ("fim", "programa", "num_inj", "vel_tpt", "qtd_agua")
INSERT_COLUMNS = KEY_COLUMNS + RESULT_COLUMNS
UPSERT_SQL = (
    "INSERT INTO humidificacao_csp2.operacoes AS atual ("
    + ", ".join(INSERT_COLUMNS) + ") VALUES ("
    + ", ".join(f"%({column})s" for column in INSERT_COLUMNS) + ") "
    + "ON CONFLICT (operation_id, inicio) DO UPDATE SET "
    + ", ".join(f"{c} = COALESCE(EXCLUDED.{c}, atual.{c})" for c in RESULT_COLUMNS)
    + " WHERE (" + ", ".join(f"atual.{c}" for c in RESULT_COLUMNS) + ")"
    + " IS DISTINCT FROM ("
    + ", ".join(f"COALESCE(EXCLUDED.{c}, atual.{c})" for c in RESULT_COLUMNS) + ")"
    + " RETURNING record_id"
)


def decode_timestamp(raw: bytes, zone: ZoneInfo) -> datetime | None:
    """3 words = YY MM DD HH mm SS em BCD. Zero/BCD/data inválidos => None."""
    if len(raw) != 6 or raw in (b"\x00" * 6, b"\xff" * 6):
        return None
    if any((byte >> 4) > 9 or (byte & 15) > 9 for byte in raw):
        return None
    yy, month, day, hour, minute, second = (
        (byte >> 4) * 10 + (byte & 15) for byte in raw
    )
    try:
        local = datetime(2000 + yy, month, day, hour, minute, second)
        aware = local.replace(tzinfo=zone)
        # Não inventar um instante numa hora local inexistente ou ambígua.
        if aware.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) != local:
            return None
        if aware.utcoffset() != local.replace(tzinfo=zone, fold=1).utcoffset():
            return None
        return aware.astimezone(timezone.utc)
    except ValueError:
        return None


def decode_float(raw: bytes) -> float | None:
    """Float32 do PLC: word baixa primeiro; não arredondar na aquisição."""
    value = struct.unpack(">f", raw[2:4] + raw[0:2])[0]
    return value if math.isfinite(value) else None


def decode_snapshot(raw: bytes, zone: ZoneInfo) -> dict:
    """EM0_0..20 (42 bytes). Cada novo início identifica outra execução.

    Mapa: início=0..2; fim=3..5; programa=6..15; operação=16;
    injetores=17; velocidade=18; quantidade de água float32=19..20.
    """
    if len(raw) != WORD_COUNT * 2:
        raise ValueError(f"O bloco EM0 deve conter exatamente {WORD_COUNT * 2} bytes.")
    start = decode_timestamp(raw[0:6], zone)
    if start is None:
        raise ValueError("Início PLC inválido, vazio ou com hora local ambígua.")
    end = decode_timestamp(raw[6:12], zone)

    words = struct.unpack(f">{WORD_COUNT}H", raw)
    water = decode_float(raw[38:42])
    return {
        "operation_id": words[16],
        "inicio": start.isoformat(),
        "fim": end.isoformat() if end is not None else None,
        "programa": raw[12:32].replace(b"\xff", b"").replace(b"\x00", b"")
            .decode("latin-1").strip(),
        "num_inj": words[17],
        "vel_tpt": words[18],
        "qtd_agua": round(water, 2) if water is not None else None,
    }


def database_payload(snapshot: dict) -> dict:
    # Humidades são preenchidas manualmente. Nunca enviar os zeros do PLC
    # nem incluir essas colunas no INSERT/UPDATE do coletor.
    return {name: snapshot[name] for name in INSERT_COLUMNS}


def passage_key(snapshot):
    # O início identifica a passagem. Fim e medições podem chegar mais tarde.
    return tuple(snapshot[name] for name in KEY_COLUMNS)


def read_words(connection, fins_module, address, count) -> bytes:
    response = connection.memory_area_read(
        fins_module.FinsPLCMemoryAreas().EM0_WORD,
        address.to_bytes(2, "big") + b"\x00", count,
    )
    if response is None or len(response) < DATA_OFFSET:
        raise ConnectionError("Resposta FINS vazia ou incompleta.")
    if response[9:12] != b"\x60\x01\x01":
        raise ConnectionError("SID/comando inesperado na resposta FINS.")
    if response[12:14] != b"\x00\x00":
        raise ConnectionError(f"Erro FINS: {response[12:14].hex()}")
    if len(response) != DATA_OFFSET + count * 2:
        raise ConnectionError(f"Resposta FINS sem as {count} words completas.")
    return response[DATA_OFFSET:]


def read_block(connection, fins_module):
    return read_words(connection, fins_module, 0, WORD_COUNT)


def read_history(connection, fins_module, zone, slots=360):
    """Buffer: 25 words/registo, newest first observado no PLC.

    Percorrer tudo dispensa conhecer um ponteiro. A primeira posição deve
    manter-se igual durante a leitura, para detetar deslocações do buffer.
    Devolver apenas um conjunto completo; nunca persistir uma leitura parcial.
    """
    anchor = read_words(connection, fins_module, 1000, 25)
    records = {}
    for first in range(0, slots, 10):
        count = min(10, slots - first)
        raw = read_words(connection, fins_module, 1000 + first * 25, count * 25)
        for index in range(count):
            record = raw[index * 50:(index + 1) * 50]
            if record == bytes(50):
                continue
            try:
                snapshot = decode_snapshot(record[:WORD_COUNT * 2], zone)
            except ValueError as exc:
                raise ValueError(f"Registo histórico inválido na posição {first + index}: {exc}") from exc
            records.setdefault(passage_key(snapshot), snapshot)
    if read_words(connection, fins_module, 1000, 25) != anchor:
        raise ConnectionError("Buffer alterou-se durante a leitura; repetir na próxima consulta.")
    return records


def read_cycle(connection, fins_module, zone):
    history = read_history(connection, fins_module, zone)
    live_raw = read_block(connection, fins_module)
    try:
        live = decode_snapshot(live_raw, zone)
    except ValueError as exc:
        LOG.warning("Zona dinâmica sem início válido: %s", exc)
    else:
        key = passage_key(live)
        # Resultados do histórico têm prioridade sobre os valores dinâmicos,
        # que podem já ter sido repostos a zero para o próximo contentor.
        history.setdefault(key, live)
    return sorted(history.values(), key=lambda r: (r["inicio"], r["operation_id"]))


class Outbox:
    """Fila durável com deduplicação por passagem, transacional com a fila."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE IF NOT EXISTS outbox ("
                         "seq INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS metadata ("
                         "key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS seen_passages ("
                         "key TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            # Preservar eventuais pendentes da primeira versão, com os nomes novos.
            for seq, encoded in conn.execute("SELECT seq, payload FROM outbox").fetchall():
                payload = json.loads(encoded)
                if "numero_operacao" in payload:
                    renamed = dict(operation_id=payload["numero_operacao"], inicio=payload["inicio"],
                                   fim=payload["fim"], programa=payload.get("programa") or "",
                                   num_inj=payload.get("numero_injetores"),
                                   vel_tpt=payload.get("velocidade_tapete_pct"),
                                   qtd_agua=payload.get("quantidade_agua"))
                    conn.execute("UPDATE outbox SET payload = ? WHERE seq = ?",
                                 (json.dumps(renamed, ensure_ascii=False, allow_nan=False), seq))

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=2)
        conn.execute("PRAGMA synchronous=FULL")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def put(self, payload: dict):
        key = json.dumps(passage_key(payload))
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True)
        with self.connect() as conn:
            previous = conn.execute("SELECT payload FROM seen_passages WHERE key = ?", (key,)).fetchone()
            if previous and previous[0] == encoded:
                return False
            conn.execute("INSERT INTO outbox(payload) VALUES (?)", (encoded,))
            conn.execute("INSERT OR REPLACE INTO seen_passages(key, payload) VALUES (?, ?)", (key, encoded))
        return True

    def get_metadata(self, key):
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_metadata(self, key, value):
        with self.connect() as conn:
            conn.execute("INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)", (key, value))

    def first(self):
        with self.connect() as conn:
            row = conn.execute("SELECT seq, payload FROM outbox ORDER BY seq LIMIT 1").fetchone()
        return (row[0], json.loads(row[1])) if row else None

    def acknowledge(self, seq: int):
        with self.connect() as conn:
            conn.execute("DELETE FROM outbox WHERE seq = ?", (seq,))

    def count(self):
        with self.connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]


@contextmanager
def single_instance(path: Path):
    """Impede dois processos de consumirem a mesma fila simultaneamente."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0)
        handle.write(b"0")
        handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("Já existe um serviço a usar esta fila local.") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def database_config() -> dict:
    required = ("DB_HOST", "DB_NAME", "DB_USER")
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise ValueError("Configuração em falta: " + ", ".join(missing))
    config = {
        "host": os.environ["DB_HOST"], "dbname": os.environ["DB_NAME"],
        "user": os.environ["DB_USER"], "port": int(os.getenv("DB_PORT", "5432")),
        "connect_timeout": int(os.getenv("DB_CONNECT_TIMEOUT", "5")),
        "application_name": "csp2_hum_machine",
        "sslmode": os.getenv("DB_SSLMODE", "prefer"),
        "options": "-c statement_timeout=5000 -c lock_timeout=2000",
    }
    # Sem DB_PASSWORD explícita, libpq pode usar o método de autenticação
    # configurado pelo IT, incluindo um password file. Nunca imprimir config.
    if "DB_PASSWORD" in os.environ:
        config["password"] = os.environ["DB_PASSWORD"]
    return config


def send_one(connection, payload: dict) -> int | None:
    """Só regressa depois do COMMIT. Falhar mantém a observação na fila."""
    with connection:
        with connection.cursor() as cursor:
            cursor.execute(UPSERT_SQL, payload)
            row = cursor.fetchone()
            return row[0] if row else None


def load_import(path: Path) -> list[dict]:
    """Valida todo o preview antes de abrir uma ligação à BD."""
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list) or not rows:
        raise ValueError("O JSON deve conter uma lista não vazia de passagens.")
    result = []
    seen = set()
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict) or not set(INSERT_COLUMNS).issubset(row):
            raise ValueError(f"Linha {index}: faltam campos da passagem.")
        if any(row.get(c) is not None for c in ("record_id", "created_at", "hum_entrada", "hum_saida")):
            raise ValueError(f"Linha {index}: importar apenas o preview, com IDs/datas/humidades a null.")
        payload = database_payload(row)
        for field in ("inicio", "fim"):
            value = payload[field]
            if value is None and field == "fim":
                continue
            if not isinstance(value, str):
                raise ValueError(f"Linha {index}: {field} deve ser um timestamp com fuso.")
            stamp = datetime.fromisoformat(value)
            if stamp.utcoffset() is None:
                raise ValueError(f"Linha {index}: {field} sem fuso horário.")
            payload[field] = stamp.astimezone(timezone.utc).isoformat()
        for field in ("operation_id", "num_inj", "vel_tpt"):
            value = payload[field]
            if value is None and field != "operation_id":
                continue
            if type(value) is not int or not 0 <= value <= 65535:
                raise ValueError(f"Linha {index}: {field} deve ser inteiro entre 0 e 65535.")
        if not isinstance(payload["programa"], str):
            raise ValueError(f"Linha {index}: programa deve ser texto.")
        water = payload["qtd_agua"]
        if water is not None:
            if type(water) not in (int, float) or not math.isfinite(water) or abs(water) >= 10000000000:
                raise ValueError(f"Linha {index}: quantidade de água inválida.")
            payload["qtd_agua"] = round(water, 2)
            if abs(payload["qtd_agua"]) >= 10000000000:
                raise ValueError(f"Linha {index}: água excede NUMERIC(12,2).")
        key = passage_key(payload)
        if key in seen:
            raise ValueError(f"Linha {index}: passagem duplicada no JSON: {key}.")
        seen.add(key)
        result.append(payload)
    return sorted(result, key=lambda row: (row["inicio"], row["operation_id"]))


def import_rows(connection, rows: list[dict]) -> int:
    """Importação atómica; um erro reverte todo o lote. Não inicia a recolha."""
    changed = 0
    with connection:
        with connection.cursor() as cursor:
            for payload in rows:
                cursor.execute(UPSERT_SQL, payload)
                if cursor.fetchone() is not None:
                    changed += 1
    return changed


def database_summary(connection) -> dict:
    with connection:
        with connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute("SELECT count(*), min(inicio), max(inicio), "
                           "count(DISTINCT operation_id), "
                           "count(hum_entrada), count(hum_saida) "
                           "FROM humidificacao_csp2.operacoes")
            count, first, last, operations, humidity_in, humidity_out = cursor.fetchone()
    return dict(passagens=count, inicio_mais_antigo=first.isoformat() if first else None,
                inicio_mais_recente=last.isoformat() if last else None,
                operacoes_distintas=operations, hum_entrada_preenchidas=humidity_in,
                hum_saida_preenchidas=humidity_out)


def database_worker(outbox: Outbox, config: dict, stop: threading.Event, psycopg2):
    connection = None
    backoff = 5.0
    previous_operation = None
    try:
        while not stop.is_set():
            # Erros locais da fila são fatais; não fingir que a aquisição
            # continua protegida quando o armazenamento local está avariado.
            item = outbox.first()
            if item is None:
                stop.wait(0.5)
                continue
            seq, payload = item
            try:
                if connection is None or connection.closed:
                    connection = psycopg2.connect(**config)
                    LOG.info("Ligação PostgreSQL estabelecida.")
                record_id = send_one(connection, payload)
                outbox.acknowledge(seq)
                operation = passage_key(payload)
                if operation != previous_operation:
                    LOG.info("[BD] record_id=%s; operação=%s", record_id, operation)
                    previous_operation = operation
                backoff = 5.0
            except psycopg2.Error as exc:
                if connection is not None:
                    connection.close()
                    connection = None
                # Mostrar o erro SQL ajuda a identificar tabela/permissão em
                # falta; nunca se imprime o dicionário com as credenciais.
                LOG.error("PostgreSQL indisponível/erro SQL: %s. Retry em %.0fs.",
                          str(exc).strip(), backoff)
                stop.wait(backoff)
                backoff = min(backoff * 2, 60.0)
    except Exception:
        LOG.exception("Falha na fila local/worker; a parar o serviço.")
        stop.set()
        raise
    finally:
        if connection is not None:
            connection.close()


class WorkSchedule:
    """Horário de Lisboa; confirmação de sábado persistida entre reinícios."""

    def __init__(self, outbox=None):
        self.outbox = outbox
        self.active_saturday = outbox.get_metadata("active_saturday") if outbox else None

    def allows(self, now):
        minute = now.hour * 60 + now.minute
        if now.weekday() < 5:
            return 6 * 60 <= minute < 20 * 60
        if now.weekday() == 5:
            return (8 * 60 <= minute < 8 * 60 + 30 or
                    self.active_saturday == now.date().isoformat() and
                    8 * 60 <= minute < 20 * 60)
        return False

    def observe(self, now, snapshot):
        if now.weekday() != 5 or not self.allows(now):
            return
        # Um bloco histórico imóvel não confirma trabalho no sábado.
        for field in ("inicio", "fim"):
            stamp = snapshot[field]
            if stamp:
                local = datetime.fromisoformat(stamp).astimezone(now.tzinfo)
                if local.date() == now.date() and local <= now:
                    day = now.date().isoformat()
                    if self.active_saturday != day:
                        if self.outbox:
                            self.outbox.set_metadata("active_saturday", day)
                        self.active_saturday = day
                        LOG.info("Atividade de sábado confirmada; recolha até às 20:00.")
                    return


def collect(args, stop: threading.Event, outbox: Outbox | None, fins_module, tcp_module):
    connection = None
    last_seen = {}
    last_report = time.monotonic()
    backoff = 5.0
    zone = ZoneInfo(args.plc_timezone)
    schedule_zone = ZoneInfo(args.schedule_timezone)
    schedule = WorkSchedule(outbox)
    waiting = False
    try:
        while not stop.is_set():
            now = datetime.now(schedule_zone)
            if not args.ignore_schedule and not schedule.allows(now):
                if connection is not None:
                    connection.fins_socket.close()
                    connection = None
                if not waiting:
                    LOG.info("Fora do horário; ligação PLC fechada, recolha suspensa.")
                    waiting = True
                stop.wait(30)
                continue
            if waiting:
                LOG.info("Dentro do horário; recolha retomada.")
                waiting = False
            iteration_start = time.monotonic()
            try:
                if connection is None:
                    # Usar a mesma ligação da função de teste já validada na
                    # máquina. Não substituir a negociação TCP da biblioteca.
                    connection = tcp_module.TCPFinsConnection()
                    connection.fins_socket.settimeout(args.plc_timeout)
                    connection.connect(args.plc_ip, port=args.plc_port)
                    LOG.info("Ligado ao PLC %s:%s.", args.plc_ip, args.plc_port)
                snapshots = read_cycle(connection, fins_module, zone)
                backoff = 5.0
            except Exception as exc:
                if connection is not None:
                    connection.fins_socket.close()
                    connection = None
                LOG.error("Leitura PLC falhou: %s. Retry em %.0fs.", exc, backoff)
                stop.wait(backoff)
                backoff = min(backoff * 2, 30.0)
                continue

            for snapshot in snapshots:
                schedule.observe(datetime.now(schedule_zone), snapshot)
                payload = database_payload(snapshot)
                key = passage_key(snapshot)
                if outbox is not None:
                    changed = outbox.put(payload)
                else:
                    changed = last_seen.get(key) != payload
                if changed:
                    LOG.info("[PLC] %s", json.dumps(payload, ensure_ascii=False))
                last_seen[key] = payload

            if time.monotonic() - last_report >= 60:
                pending = outbox.count() if outbox is not None else 0
                LOG.info("Leitura PLC ativa; observações por enviar=%s.", pending)
                if pending >= 10000:
                    LOG.warning("Fila local extensa: verificar PostgreSQL e espaço em disco.")
                last_report = time.monotonic()

            # Event.wait permite terminar prontamente com Ctrl+C/docker stop.
            # O intervalo conta desde o início da leitura, sem somar sempre
            # o intervalo ao tempo gasto na comunicação.
            stop.wait(max(0.0, args.poll_seconds - (time.monotonic() - iteration_start)))
    finally:
        if connection is not None:
            connection.fins_socket.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env-file", type=Path,
                        default=Path(__file__).with_name(".env"))
    parser.add_argument("--plc-ip", help="IPv4 completo do PLC; alternativa: PLC_IP")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write-db", action="store_true", help="Ativa fila e envio PostgreSQL")
    mode.add_argument("--print-schema", action="store_true", help="Imprime SQL e termina")
    mode.add_argument("--init-db", action="store_true", help="Cria schema/tabela na BD configurada e termina")
    mode.add_argument("--import-json", type=Path, help="Importa o preview numa transação e termina, sem ler PLC")
    mode.add_argument("--check-db", action="store_true", help="Mostra resumo da BD e termina, sem ler PLC")
    parser.add_argument("--ignore-schedule", action="store_true", help="Permite teste manual fora do horário")
    args = parser.parse_args()
    if args.print_schema:
        print(SCHEMA_SQL)
        return 0

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    try:
        if args.env_file.exists():
            from dotenv import load_dotenv
            load_dotenv(args.env_file, override=False)
        if args.init_db or args.import_json or args.check_db:
            rows = load_import(args.import_json) if args.import_json else None
            import psycopg2
            conn = psycopg2.connect(**database_config())
            try:
                if args.init_db:
                    with conn:
                        with conn.cursor() as cursor:
                            cursor.execute(SCHEMA_SQL)
                    LOG.info("Schema/tabela preparados na base configurada.")
                elif args.import_json:
                    changed = import_rows(conn, rows)
                    LOG.info("Importação concluída: passagens=%s; gravações=%s; inalteradas=%s.",
                             len(rows), changed, len(rows) - changed)
                else:
                    print(json.dumps(database_summary(conn), ensure_ascii=False, indent=2))
            finally:
                conn.close()
            return 0
        import fins
        import fins.tcp
        args.plc_ip = args.plc_ip or os.getenv("PLC_IP", "10.0.0.60")
        ipaddress.IPv4Address(args.plc_ip)
        args.plc_port = int(os.getenv("PLC_PORT", "9600"))
        args.poll_seconds = float(os.getenv("POLL_SECONDS", "15"))
        args.plc_timeout = float(os.getenv("PLC_TIMEOUT_SECONDS", "3"))
        args.plc_timezone = os.getenv("PLC_TIMEZONE", "Europe/Lisbon")
        ZoneInfo(args.plc_timezone)
        args.schedule_timezone = os.getenv("SCHEDULE_TIMEZONE", "Europe/Lisbon")
        ZoneInfo(args.schedule_timezone)
        if not (math.isfinite(args.poll_seconds) and args.poll_seconds > 0
                and math.isfinite(args.plc_timeout) and args.plc_timeout > 0
                and 1 <= args.plc_port <= 65535):
            raise ValueError("Intervalo/timeout devem ser positivos; porta deve ser válida.")
        if args.write_db:
            import psycopg2
            config = database_config()
    except (ImportError, ValueError, KeyError) as exc:
        parser.error(str(exc))

    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())
    LOG.info("Modo=%s; intervalo=%.1fs; fuso PLC=%s; horário=%s.",
             "PostgreSQL" if args.write_db else "só monitorização",
             args.poll_seconds, args.plc_timezone,
             "sem restrição" if args.ignore_schedule else args.schedule_timezone)
    LOG.info("Uma máquina; leitura dinâmica + histórico de 360 registos; transporte FINS/TCP.")
    if not args.write_db:
        collect(args, stop, None, fins, fins.tcp)
        return 0

    spool_path = Path(os.getenv("SPOOL_PATH", "./csp2_state/outbox.sqlite3")).resolve()
    worker_failed = threading.Event()
    with single_instance(spool_path.with_suffix(".lock")):
        outbox = Outbox(spool_path)

        def run_worker():
            try:
                database_worker(outbox, config, stop, psycopg2)
            except Exception:
                worker_failed.set()
                stop.set()

        worker = threading.Thread(target=run_worker, name="csp2-postgresql", daemon=True)
        worker.start()
        try:
            collect(args, stop, outbox, fins, fins.tcp)
        finally:
            stop.set()
            worker.join(timeout=12)
            if worker.is_alive():
                raise RuntimeError("Worker não terminou dentro do prazo; fila preservada.")
            LOG.info("Serviço parado; observações preservadas na fila=%s.", outbox.count())
    return 1 if worker_failed.is_set() else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        LOG.exception("Serviço terminado devido a erro.")
        raise SystemExit(1)
