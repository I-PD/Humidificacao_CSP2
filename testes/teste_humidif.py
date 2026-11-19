#!/usr/bin/env python3
"""Diagnóstico FINS/EM0, só leitura. Não importa nem liga ao PostgreSQL.

Usar com a MESMA instalação de `fins` dos coletores existentes.
O formato da resposta (14 bytes antes dos dados) segue os exemplos enviados.
As descodificações são candidatas, a confirmar contra o HMI/programa PLC.
"""
import argparse
from datetime import datetime, timezone
import ipaddress
import json
import math
import struct
import sys
import time

DATA_OFFSET = 14
NUM_WORDS = 25  # E0_00 até E0_24, inclusive.

def validate_response(response):
    """Valida a resposta FINS crua: comando 0101 e end code 0000.

    O helper original verifica só o comprimento. Uma resposta com erro
    também tem cabeçalho; nunca se devem interpretar esses bytes como valores.
    """
    if not isinstance(response, (bytes, bytearray)):
        raise ConnectionError('Resposta ausente ou formato diferente de bytes.')
    if response[:4] == b'FINS':
        raise ConnectionError('Recebido envelope FINS/TCP; confirmar API/versão da biblioteca.')
    if len(response) < DATA_OFFSET:
        raise ConnectionError(f'Cabeçalho incompleto: {len(response)} bytes.')
    if response[10:12] != b'\x01\x01':
        raise ConnectionError(f'Comando inesperado: {response[10:12].hex()}.')
    if response[12:14] != b'\x00\x00':
        raise ConnectionError(f'PLC devolveu end code FINS 0x{response[12:14].hex()}.')
    expected = DATA_OFFSET + NUM_WORDS * 2
    if len(response) != expected:
        raise ConnectionError(f'Resposta com {len(response)} bytes; esperados {expected}.')
    return bytes(response[DATA_OFFSET:])


def date_candidate(raw, encoding):
    """6 bytes: ano/mês/dia/hora/minuto/segundo, byte alto primeiro.

    Não atribui timezone: relógio do PLC ainda por confirmar.
    Suposição explícita: ano de 2 algarismos corresponde a 2000..2099.
    Zeros são tratados como data não preenchida, nunca como início de 2000.
    """
    if raw == b'\x00' * 6:
        return None
    try:
        parts = list(raw)
        if encoding == 'bcd':
            if any((v >> 4) > 9 or (v & 15) > 9 for v in parts):
                raise ValueError('algarismos BCD inválidos')
            parts = [(v >> 4) * 10 + (v & 15) for v in parts]
        if parts[0] > 99:
            raise ValueError('ano fora de 00..99')
        return datetime(2000 + parts[0], *parts[1:]).isoformat(sep=' ')
    except ValueError as exc:
        return f'INVÁLIDO: {exc}'


def float_candidate(raw, low_word_first):
    """IEEE754 de 32 bits: testa as duas ordens de WORDs (2 bytes/WORD).

    Cada WORD chega em ordem de rede, como nos exemplos recebidos.
    Um número plausível por si só NÃO confirma a ordem correta.
    """
    if low_word_first:
        raw = raw[2:4] + raw[0:2]
    value = struct.unpack('>f', raw)[0]
    return value if math.isfinite(value) else str(value)


def decode_block(payload):
    if len(payload) != NUM_WORDS * 2:
        raise ValueError('Bloco EM0 deve conter exatamente 50 bytes.')
    words = list(struct.unpack('>25H', payload))  # WORD unsigned: 0..65535.
    result = {
        'aviso': 'Interpretações candidatas; validar com HMI. Datas sem timezone.',
        'raw_words': [
            {'address': f'E0_{i:02d}', 'hex': f'{w:04X}', 'unsigned': w}
            for i, w in enumerate(words)
        ],
        'operation_id_unsigned': words[16],
        'injector_count_unsigned': words[17],
        'speed_pct_raw_unsigned': words[18],
    }
    for name, offset in (('op_start', 0), ('op_end', 3)):
        raw = payload[offset * 2:(offset + 3) * 2]
        result[name] = {
            'raw_hex': raw.hex(' '),
            'bcd_ano_2000_byte_alto_primeiro': date_candidate(raw, 'bcd'),
            'binario_ano_2000_byte_alto_primeiro': date_candidate(raw, 'binary'),
        }
    raw = payload[6 * 2:16 * 2]  # 10 WORDs = 20 bytes, sem byte de comprimento presumido.
    swapped = b''.join(raw[i:i+2][::-1] for i in range(0, len(raw), 2))
    # repr no terminal JSON torna visíveis zeros internos e bytes de controlo.
    result['program_name'] = {
        'raw_hex': raw.hex(' '),
        'latin1_byte_alto_primeiro': raw.decode('latin-1').rstrip('\x00 '),
        'latin1_bytes_trocados_por_word': swapped.decode('latin-1').rstrip('\x00 '),
    }
    for name, offset in (('water_qty', 19), ('humidity_in', 21), ('humidity_out', 23)):
        raw = payload[offset * 2:(offset + 2) * 2]
        result[name] = {
            'raw_hex': raw.hex(' '),
            'word_menor_endereco_e_alta': float_candidate(raw, False),
            'word_menor_endereco_e_baixa': float_candidate(raw, True),
        }
    return result


def get_humidificacao_data(connection):
    """Lê as 25 WORDs num único pedido, reduzindo o desfasamento entre campos.

    Um pedido não garante snapshot atómico do programa PLC; confirmar se
    o bloco é publicado de forma coerente durante a mudança de operação.
    Import local permite verificar os descodificadores sem instalar fins.
    """
    import fins
    started = datetime.now(timezone.utc)
    response = connection.memory_area_read(
        fins.FinsPLCMemoryAreas().EM0_WORD, b'\x00\x00\x00', NUM_WORDS
    )
    result = decode_block(validate_response(response))
    result['read_started_at_utc'] = started.isoformat()
    result['read_completed_at_utc'] = datetime.now(timezone.utc).isoformat()
    return result


def close_connection(connection):
    if connection is None:
        return
    # Algumas versões não expõem close() na ligação, mas expõem fins_socket.
    try:
        closer = getattr(connection, 'close', None)
        if callable(closer):
            closer()
    finally:
        sock = getattr(connection, 'fins_socket', None)
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def self_test():
    """Ensaios offline com frames sintéticos; não substituem teste no PLC."""
    payload = bytearray(NUM_WORDS * 2)
    payload[0:6] = bytes.fromhex('26 10 29 11 45 30')
    payload[12:32] = b'PROGRAMA TESTE'.ljust(20, b'\x00')
    payload[32:38] = struct.pack('>3H', 65535, 8, 75)
    payload[38:42] = struct.pack('>f', 12.5)
    raw = struct.pack('>f', 8.25)
    payload[42:46] = raw[2:] + raw[:2]
    result = decode_block(payload)
    assert result['op_start']['bcd_ano_2000_byte_alto_primeiro'] == '2026-10-29 11:45:30'
    assert result['op_end']['bcd_ano_2000_byte_alto_primeiro'] is None
    assert result['operation_id_unsigned'] == 65535
    assert result['water_qty']['word_menor_endereco_e_alta'] == 12.5
    assert result['humidity_in']['word_menor_endereco_e_baixa'] == 8.25
    assert result['program_name']['latin1_byte_alto_primeiro'] == 'PROGRAMA TESTE'
    assert date_candidate(bytes.fromhex('26 02 30 11 00 00'), 'bcd').startswith('INVÁLIDO')
    good = b'\xC0' + bytes(9) + b'\x01\x01\x00\x00' + payload
    assert validate_response(good) == payload
    for bad in (None, good[:-1], good[:12] + b'\x11\x03' + payload,
                good[:10] + b'\x01\x02' + good[12:]):
        try:
            validate_response(bad)
        except ConnectionError:
            pass
        else:
            raise AssertionError('Frame inválido aceite')
    print('OK: testes offline de datas, floats, WORD unsigned, texto e frames.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('ip', nargs='?', default='10.0.0.60', help='IP completo do PLC')
    parser.add_argument('--count', type=int, default=1, help='Número de leituras; predefinição 1')
    parser.add_argument('--interval', type=float, default=60, help='Intervalo em segundos: 60 ou 120')
    parser.add_argument('--attempts', type=int, default=3, help='Tentativas máximas por leitura')
    parser.add_argument('--timeout', type=float, default=10, help='Timeout TCP em segundos')
    parser.add_argument('--self-test', action='store_true', help='Testes sintéticos, sem rede')
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return 0
    ipaddress.IPv4Address(args.ip)
    if min(args.count, args.attempts, args.interval, args.timeout) <= 0:
        parser.error('count, attempts, interval e timeout devem ser positivos')
    try:
        import fins.tcp
    except ImportError:
        parser.error('Executar no ambiente dos coletores, com a mesma biblioteca fins instalada.')
    connection = None
    try:
        for sample in range(args.count):
            started = time.monotonic()
            for attempt in range(args.attempts):
                try:
                    if connection is None:
                        connection = fins.tcp.TCPFinsConnection()
                        connection.fins_socket.settimeout(args.timeout)
                        connection.connect(args.ip)
                    data = get_humidificacao_data(connection)
                except Exception as exc:
                    print(f'[ERRO] {type(exc).__name__}: {exc}', file=sys.stderr, flush=True)
                    close_connection(connection)
                    connection = None  # Nunca reutilizar uma ligação falhada.
                    if attempt + 1 == args.attempts:
                        return 1
                    time.sleep(min(5 * 2**attempt, 60))
                else:
                    print(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), flush=True)
                    break
            if sample + 1 < args.count:
                # Evita acrescentar o tempo de leitura aos 60/120 segundos.
                time.sleep(max(0, args.interval - (time.monotonic() - started)))
    except KeyboardInterrupt:
        return 130
    finally:
        close_connection(connection)
    return 0


if __name__ == '__main__':
    sys.exit(main())
