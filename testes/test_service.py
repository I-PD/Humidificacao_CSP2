import struct
import json
import threading
import unittest
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import csp2_hum_machine as service

ZONE = ZoneInfo('Europe/Lisbon')


@contextmanager
def test_folder():
    root = Path(__file__).resolve().parent
    folder = root / ('test-state-' + uuid.uuid4().hex)
    folder.mkdir()
    try:
        yield folder
    finally:
        assert folder.resolve().parent == root
        for file in folder.iterdir():
            file.unlink()
        folder.rmdir()


def block(start='261112125809', end='261112125423', operation=228, water=0.0, program='V32B'):
    encoded = struct.pack('>f', water)
    return (bytes.fromhex(start + end) + program.encode('latin-1').ljust(20, b' ') +
            struct.pack('>3H', operation, 1, 94) + encoded[2:] + encoded[:2])


class ServiceTests(unittest.TestCase):
    def test_import_preview_uses_only_plc_fields_and_sorts_rows(self):
        first = service.decode_snapshot(block(), ZONE)
        second = service.decode_snapshot(block(start='261112130100'), ZONE)
        with test_folder() as folder:
            path = folder / 'preview.json'
            path.write_text(json.dumps([dict(second, record_id=None, created_at=None,
                                             hum_entrada=None, hum_saida=None), first]), encoding='utf-8')
            rows = service.load_import(path)
        self.assertEqual(rows, [first, second])
        self.assertEqual(set(rows[0]), set(service.INSERT_COLUMNS))

    def test_import_rejects_duplicate_passages_and_manual_values(self):
        row = service.decode_snapshot(block(), ZONE)
        with test_folder() as folder:
            path = folder / 'preview.json'
            for payload in ([row, row], [dict(row, hum_entrada=12.3)],
                            [dict(row, record_id=1)], [dict(row, inicio='2026-11-12T12:58:09')],
                            [dict(row, qtd_agua=float('nan'))], [dict(row, operation_id=True)]):
                with self.subTest(payload=payload):
                    path.write_text(json.dumps(payload), encoding='utf-8')
                    with self.assertRaises(ValueError):
                        service.load_import(path)

    def test_import_commits_one_transaction_and_counts_unchanged_rows(self):
        connection = Mock()
        connection.__enter__ = Mock(return_value=connection)
        connection.__exit__ = Mock(return_value=False)
        cursor = Mock()
        cursor.__enter__ = Mock(return_value=cursor)
        cursor.__exit__ = Mock(return_value=False)
        connection.cursor.return_value = cursor
        cursor.fetchone.side_effect = [(1,), None]
        rows = [service.decode_snapshot(block(), ZONE), service.decode_snapshot(block(start='261112130100'), ZONE)]
        self.assertEqual(service.import_rows(connection, rows), 1)
        connection.__enter__.assert_called_once()
        connection.__exit__.assert_called_once_with(None, None, None)

    def test_water_has_two_decimal_places_before_deduplication(self):
        a = service.decode_snapshot(block(water=0.801), ZONE)
        b = service.decode_snapshot(block(water=0.804), ZONE)
        c = service.decode_snapshot(block(water=0.816), ZONE)
        self.assertEqual(a['qtd_agua'], 0.80)
        self.assertEqual(a, b)
        self.assertEqual(c['qtd_agua'], 0.82)
        with test_folder() as folder:
            outbox = service.Outbox(folder / 'outbox.sqlite3')
            self.assertTrue(outbox.put(service.database_payload(a)))
            self.assertFalse(outbox.put(service.database_payload(b)))
            self.assertTrue(outbox.put(service.database_payload(c)))

    def test_plc_cannot_overwrite_manual_humidity_fields(self):
        data = service.decode_snapshot(block(water=0.8), ZONE)
        payload = service.database_payload(dict(data, hum_entrada=0, hum_saida=0))
        self.assertNotIn('hum_entrada', payload)
        self.assertNotIn('hum_saida', payload)
        self.assertNotIn('hum_entrada', service.UPSERT_SQL)
        self.assertNotIn('hum_saida', service.UPSERT_SQL)

    def test_stale_end_preserves_all_fields_then_is_replaced(self):
        initial = service.decode_snapshot(block(), ZONE)
        payload = service.database_payload(initial)
        self.assertEqual(payload['fim'], '2026-11-12T12:54:23+00:00')
        self.assertEqual(payload['programa'], 'V32B')
        self.assertEqual(payload['qtd_agua'], 0)
        updated = service.decode_snapshot(block(end='261112130039', water=0.8), ZONE)
        self.assertAlmostEqual(updated['qtd_agua'], 0.8, places=6)
        self.assertEqual(service.passage_key(initial), service.passage_key(updated))

    def test_same_operation_multiple_containers(self):
        a = service.decode_snapshot(block(), ZONE)
        b = service.decode_snapshot(block(start='261112130100'), ZONE)
        self.assertEqual(a['operation_id'], b['operation_id'])
        self.assertNotEqual(a['inicio'], b['inicio'])

    def test_program_changes_with_new_start_and_same_operation(self):
        first = service.decode_snapshot(block(program='V31B'), ZONE)
        second = service.decode_snapshot(block(start='261112130100', program='V32B'), ZONE)
        self.assertNotEqual(service.passage_key(first), service.passage_key(second))
        # O mesmo programa pode voltar a executar noutro início.
        third = service.decode_snapshot(block(start='261112130400', program='V32B'), ZONE)
        self.assertNotEqual(service.passage_key(second), service.passage_key(third))

    def test_program_change_alone_does_not_invent_a_new_start(self):
        first = service.decode_snapshot(block(program='V31B'), ZONE)
        second = service.decode_snapshot(block(program='V32B'), ZONE)
        self.assertEqual(service.passage_key(first), service.passage_key(second))

    def test_history_stride_and_order_does_not_use_slot_as_identity(self):
        a = block(start='261112130100', end='261112130200', operation=228, water=0.8)
        b = block(start='261112125809', end='261112125950', operation=228, water=0.6)
        slots = a + bytes(8) + b + bytes(8)
        with patch.object(service, 'read_words', side_effect=[slots[:50], slots, slots[:50]]) as read:
            history = service.read_history(Mock(), Mock(), ZONE, slots=2)
        self.assertEqual(len(history), 2)
        self.assertEqual(read.call_args_list[1].args[-2:], (1000, 50))
        with patch.object(service, 'read_words', side_effect=[slots[:50], slots[50:] + slots[:50], slots[:50]]):
            shifted = service.read_history(Mock(), Mock(), ZONE, slots=2)
        self.assertEqual(history, shifted)

    def test_buffer_shift_during_scan_is_rejected(self):
        a = block() + bytes(8)
        b = block(start='261112130100') + bytes(8)
        with patch.object(service, 'read_words', side_effect=[a, a, b]):
            with self.assertRaises(ConnectionError):
                service.read_history(Mock(), Mock(), ZONE, slots=1)

    def test_history_water_wins_over_dynamic_zero_and_new_live_passage_is_kept(self):
        final = service.decode_snapshot(block(end='261112125950', water=0.8), ZONE)
        live = block(end='261112125950', water=0)
        history = {service.passage_key(final): final}
        with patch.object(service, 'read_history', return_value=dict(history)), patch.object(service, 'read_block', return_value=live):
            cycle = service.read_cycle(Mock(), Mock(), ZONE)
        self.assertEqual(len(cycle), 1)
        self.assertAlmostEqual(cycle[0]['qtd_agua'], 0.8, places=6)
        new_live = block(start='261112130100', operation=228, water=0)
        with patch.object(service, 'read_history', return_value=dict(history)), patch.object(service, 'read_block', return_value=new_live):
            cycle = service.read_cycle(Mock(), Mock(), ZONE)
        self.assertEqual(len(cycle), 2)

    def test_historical_records_are_not_requeued_each_poll(self):
        with test_folder() as folder:
            outbox = service.Outbox(folder / 'outbox.sqlite3')
            a = service.decode_snapshot(block(start='261112125809'), ZONE)
            b = service.decode_snapshot(block(start='261112130100'), ZONE)
            for row in (a, b):
                self.assertTrue(outbox.put(row))
            for row in (b, a):
                self.assertFalse(outbox.put(row))
            self.assertEqual(outbox.count(), 2)

    def test_legacy_pending_queue_is_preserved_with_new_names(self):
        import sqlite3
        import json
        with test_folder() as folder:
            path = folder / 'outbox.sqlite3'
            conn = sqlite3.connect(path)
            conn.execute('CREATE TABLE outbox (seq INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL)')
            legacy = dict(maquina_id='HUM_01', numero_operacao=228, inicio='2026-11-12T12:58:09+00:00',
                          fim=None, programa='V32B', numero_injetores=1, velocidade_tapete_pct=94, quantidade_agua=0)
            conn.execute('INSERT INTO outbox(payload) VALUES (?)', (json.dumps(legacy),))
            conn.commit()
            conn.close()
            outbox = service.Outbox(path)
            self.assertEqual(outbox.count(), 1)
            payload = outbox.first()[1]
            self.assertEqual(set(payload), set(service.INSERT_COLUMNS))
            self.assertEqual(payload['operation_id'], 228)
            self.assertEqual(payload['qtd_agua'], 0)

    def test_reference_word_dump(self):
        words = [9745, 6166, 18487, 9745, 6166, 20484, 22066, 12866] + [8224] * 8
        data = service.decode_snapshot(struct.pack('>21H', *(words + [347, 2, 78, 0, 0])), ZONE)
        self.assertEqual(data['inicio'], '2026-11-18T16:48:37+00:00')
        self.assertEqual(data['fim'], '2026-11-18T16:50:04+00:00')
        self.assertEqual(data['programa'], 'V22B')
        self.assertEqual(data['num_inj'], 2)
        self.assertEqual(data['vel_tpt'], 78)

    def test_invalid_end_does_not_erase_other_fields(self):
        data = service.database_payload(service.decode_snapshot(block(end='000000000000'), ZONE))
        self.assertIsNone(data['fim'])
        self.assertEqual(data['num_inj'], 1)

    def test_outbox_deduplicates_across_restart_and_preserves_pending(self):
        with test_folder() as folder:
            path = Path(folder) / 'outbox.sqlite3'
            outbox = service.Outbox(path)
            data = service.database_payload(service.decode_snapshot(block(), ZONE))
            self.assertTrue(outbox.put(data))
            restarted = service.Outbox(path)
            self.assertFalse(restarted.put(dict(data)))
            self.assertEqual(restarted.count(), 1)
            restarted.acknowledge(restarted.first()[0])
            self.assertFalse(restarted.put(data))
            changed = service.database_payload(service.decode_snapshot(block(water=0.8), ZONE))
            self.assertTrue(restarted.put(changed))
            self.assertEqual(restarted.first()[1]['qtd_agua'], changed['qtd_agua'])

    def test_weekday_and_sunday_boundaries(self):
        schedule = service.WorkSchedule()
        for date, allowed in [('2026-11-20T05:59', False), ('2026-11-20T06:00', True),
                              ('2026-11-20T19:59', True), ('2026-11-20T20:00', False),
                              ('2026-11-22T09:00', False)]:
            with self.subTest(date=date):
                self.assertEqual(schedule.allows(datetime.fromisoformat(date).replace(tzinfo=ZONE)), allowed)

    def test_saturday_historical_data_does_not_extend_window(self):
        schedule = service.WorkSchedule()
        now = datetime(2026, 11, 21, 8, 15, tzinfo=ZONE)
        self.assertTrue(schedule.allows(now))
        schedule.observe(now, service.decode_snapshot(block(), ZONE))
        self.assertFalse(schedule.allows(now.replace(minute=30)))

    def test_saturday_activity_survives_restart(self):
        with test_folder() as folder:
            outbox = service.Outbox(Path(folder) / 'outbox.sqlite3')
            now = datetime(2026, 11, 21, 8, 15, tzinfo=ZONE)
            schedule = service.WorkSchedule(outbox)
            schedule.observe(now, service.decode_snapshot(block(start='261121080500'), ZONE))
            restarted = service.WorkSchedule(outbox)
            self.assertTrue(restarted.allows(now.replace(hour=19)))
            self.assertFalse(restarted.allows(now.replace(hour=20)))
            self.assertFalse(restarted.allows(datetime(2026, 11, 28, 9, tzinfo=ZONE)))

    def test_collect_only_enqueues_changes(self):
        stop = threading.Event()
        waits = []
        def wait(_):
            waits.append(True)
            if len(waits) == 3:
                stop.set()
        stop.wait = wait
        args = SimpleNamespace(plc_timezone='Europe/Lisbon', schedule_timezone='Europe/Lisbon',
                               ignore_schedule=True, plc_timeout=3, plc_ip='10.0.0.60',
                               plc_port=9600, poll_seconds=15)
        outbox = Mock()
        tcp = Mock()
        cycles = [[service.decode_snapshot(raw, ZONE)] for raw in (block(), block(), block(water=0.8))]
        outbox.put.side_effect = [True, False, True]
        with patch.object(service, 'read_cycle', side_effect=cycles):
            service.collect(args, stop, outbox, Mock(), tcp)
        self.assertEqual(outbox.put.call_count, 3)
        tcp.TCPFinsConnection.return_value.fins_socket.close.assert_called_once()

    def test_fins_rejects_partial_and_error_responses(self):
        connection = Mock()
        fins = Mock()
        header = b'\x00' * 9 + b'\x60\x01\x01\x00\x00'
        connection.memory_area_read.return_value = header + block()
        self.assertEqual(service.read_block(connection, fins), block())
        for response in [header + block()[:-1], header[:12] + b'\x00\x01' + block()]:
            connection.memory_area_read.return_value = response
            with self.assertRaises(ConnectionError):
                service.read_block(connection, fins)

    def test_outside_schedule_does_not_connect_to_plc(self):
        stop = threading.Event()
        stop.wait = lambda _: stop.set()
        args = SimpleNamespace(plc_timezone='Europe/Lisbon', schedule_timezone='Europe/Lisbon',
                               ignore_schedule=False)
        tcp = Mock()
        with patch.object(service, 'datetime') as clock:
            clock.now.return_value = datetime(2026, 11, 22, 9, tzinfo=ZONE)
            service.collect(args, stop, None, Mock(), tcp)
        tcp.TCPFinsConnection.assert_not_called()


if __name__ == '__main__':
    unittest.main()
