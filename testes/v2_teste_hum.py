import sys
import fins.tcp
from datetime import datetime
from zoneinfo import ZoneInfo

# ---------- FUNÇÕES BASE ----------

def read_timestamp(fins_instance, start_addr):
    values = read_words_safe(fins_instance, start_addr, 3)

    # Cada word contém dois componentes em BCD
    raw = b"".join(v.to_bytes(2, "big", signed=False)
                   for v in values)

    def bcd_to_int(byte):
        return ((byte >> 4) * 10) + (byte & 0x0F)

    yy, mm, dd, hh, minute, ss = [
        bcd_to_int(b) for b in raw
    ]

    return datetime(
        2000 + yy, mm, dd, hh, minute, ss
    )

def read_words(fins_instance, start_addr, num_words):
    mem = fins_instance.memory_area_read(
        fins.FinsPLCMemoryAreas().EM0_WORD,
        start_addr,
        num_words
    )
    values = []
    pos = 7
    for _ in range(num_words):
        word = mem[pos*2:pos*2+2]
        values.append(int.from_bytes(word, "big", signed= True)) #último parametro para ler valores negativos
        pos += 1
    return values

DATA_OFFSET = 14 #bytes (header FINS)

def read_words_safe(fins_instance, start_addr, num_words):
    mem = fins_instance.memory_area_read(
        fins.FinsPLCMemoryAreas().EM0_WORD,
        start_addr,
        num_words
    )

    values = []
    for i in range(num_words):
        base = DATA_OFFSET + i*2
        word = mem[base:base+2]
        values.append(int.from_bytes(word, "big", signed =False))  # último parametro para ler valores negativos
    return values

def read_string_safe(fins_instance, start_addr, num_words):
    mem = fins_instance.memory_area_read(
        fins.FinsPLCMemoryAreas().EM0_WORD,
        start_addr,
        num_words
    )

    chars = []
    for i in range(num_words):
        base = DATA_OFFSET + i*2
        word = mem[base:base+2]

        if word in (b'\x00\x00', b'\xff\xff'):
            continue

        try:
            chars.append(word.decode("latin-1"))
        except UnicodeDecodeError:
            pass

    return "".join(chars).strip()

import struct

def read_float32(fins_instance, start_addr):
    values = read_words_safe(fins_instance, start_addr, 2)

    # O PLC guarda primeiro a word menos significativa
    low_word, high_word = values

    # Converter para bytes na ordem IEEE 754 correta
    raw = (
        high_word.to_bytes(2, "big", signed=False) +
        low_word.to_bytes(2, "big", signed=False)
    )

    return round(struct.unpack(">f", raw)[0], 3)


def dump_em0(fins_instance, start_word, num_words):
    print(f"\n===== DUMP EM0 desde E0_{start_word} ({num_words} WORDS) =====\n")

    start_addr = start_word.to_bytes(2, "big") + b'\x00'
    mem = fins_instance.memory_area_read(
        fins.FinsPLCMemoryAreas().EM0_WORD,
        start_addr,
        num_words
    )

    for i in range(num_words):
        base = DATA_OFFSET + i*2   # bytes
        word = mem[base:base+2]
        val = int.from_bytes(word, "big")
        print(f"E0_{start_word + i:03d} → {val}")

    print("\n============================================\n")

import time
from datetime import datetime
from zoneinfo import ZoneInfo

def monitorizar_humidificacao(fins_instance):

    ultimo_estado = None

    for i in range(30):

        inicio = read_timestamp(
            fins_instance, b'\x00\x00\x00'
        )

        fim = read_timestamp(
            fins_instance, b'\x00\x03\x00'
        )

        operacao = read_words_safe(
            fins_instance, b'\x00\x10\x00', 1
        )[0]

        programa = read_string_safe(fins_instance, b'\x00\x06\x00', 10), #E_06 a E_15

        agua = read_float32(
            fins_instance, b'\x00\x13\x00'
        )

        hum_entrada = read_float32(fins_instance, b'\x00\x15\x00'),
        hum_saida = read_float32(fins_instance, b'\x00\x17\x00'),

        duracao = (fim - inicio).total_seconds()

        estado = (inicio, fim, operacao, agua)

        if estado != ultimo_estado:

            print(
                f"\nLeitura: "
                f"{datetime.now(ZoneInfo('Europe/Lisbon'))}"
            )

            print(f"Operação : {operacao}")
            print(f"Início   : {inicio}")
            print(f"Fim      : {fim}")
            print(f"Programa : {programa}")
            print(f"Duração  : {duracao} segundos")
            print(f"Água     : {agua}")
            print(f"Humidade entrada: {hum_entrada}")
            print(f"Humidade saída  : {hum_saida}")

            ultimo_estado = estado
            print(f"Estado alterado! ({i+1}/30)")

        time.sleep(10)

# ---------- FUNÇÃO DE TESTE ----------

def teste_humidificacao(fins_instance):
    print("\n================ HUMIDIFICAÇÃO ================\n")
    print(f"Timestamp: {datetime.now(ZoneInfo('Europe/Lisbon'))}\n")

    data = {}

    # -------- TEMPERATURAS --------

    data["Timestamp"] = {
        "inicio" : read_timestamp(fins_instance, b'\x00\x00\x00'), #E_00 a E_02
        "fim"    : read_timestamp(fins_instance, b'\x00\x03\x00'), #E_03 a E_05
    }

    data["Programa"] = {
        "nome" : read_string_safe(fins_instance, b'\x00\x06\x00', 10), #E_06 a E_15
    }

    data["Operação"] = {
        "número" : read_words_safe(fins_instance, b'\x00\x10\x00', 1)[0], #E_16
    }

    data["Injetores"] = {
        "número" : read_words_safe(fins_instance, b'\x00\x11\x00', 1)[0], #E0_17
    }

    data["Velocidade tapete"] = {
        "percentagem": read_words_safe(fins_instance, b'\x00\x12\x00', 1), #E0_18
    }

    data["Água"] = {
        "qtd": read_float32(fins_instance, b'\x00\x13\x00'), #E0_19 e E0_20
    }

    data["Humidade"] = {
        "entrada": read_float32(fins_instance, b'\x00\x15\x00'), #E0_21 e E0_22
        "saída": read_float32(fins_instance, b'\x00\x17\x00'), #E0_23 e E0_24
    }

    # -------- PRINT ORGANIZADO --------
    for k, v in data.items():
        print(f"{k}:")
        if isinstance(v, dict):
            for sk, sv in v.items():
                print(f"  {sk:<18}: {sv}")
        print()

    print("================================================\n")

# ---------- MAIN ----------
if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Uso: python v2_teste_hum.py <ultimo_octeto_ip>")
        sys.exit(1)

    ip_octet = sys.argv[1]
    plc_ip = f"10.0.0.{ip_octet}"

    try:
        fins_instance = fins.tcp.TCPFinsConnection()
        fins_instance.connect(plc_ip)
        print(f"Ligado ao PLC {plc_ip}")
    except Exception as e:
        print(f"Erro ao ligar ao PLC: {e}")
        sys.exit(1)

    dump_em0(fins_instance, start_word=0, num_words=25) #  DUMP EM0 desde E0_000 a E0_024
    teste_humidificacao(fins_instance)
    monitorizar_humidificacao(fins_instance)
    #dump_em0(fins_instance, start_word=1000, num_words=100) #  DUMP EM0 desde E0_1000 a E0_1100 (buffer registo operações)