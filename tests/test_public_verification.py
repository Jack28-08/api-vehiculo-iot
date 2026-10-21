"""Verificadores con dobles y archivos temporales: nunca usan .env, red ni RDS."""

import argparse
import asyncio
from copy import deepcopy
from contextlib import redirect_stderr
from contextlib import asynccontextmanager
from io import StringIO
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from scripts import check_http_reads, check_websocket_reads
from scripts.verification_common import (
    HttpClient, NoRedirect, Target, VerificationFailure, fingerprint,
    history_is_ordered, initial_matches, normalize_url, positive,
)
from scripts.verify_public import (
    CaptureGroup, PublicVerifier, check_individual_persistence, history_unchanged,
    rds_snapshot, validated_args,
)
from scripts.verify_demo_real import EvidenceWriter, validate_client


BASE = 'https://api.example.invalid'


class TargetAndAuthorizationTests(unittest.TestCase):
    def test_https_derives_wss_and_preserves_base_path(self):
        target = Target.create(BASE + '/iot/')
        self.assertEqual(target.http('/health'), BASE + '/iot/health')
        self.assertEqual(target.ws(19), 'wss://api.example.invalid/iot/ws/vehiculos/19')

    def test_insecure_mixed_or_other_authority_and_credentials_are_rejected(self):
        for base, ws in [(BASE, 'ws://api.example.invalid'), (BASE, 'wss://other.example.invalid'),
                         ('https://user:private@example.invalid', None),
                         (BASE + '?key=private', None), (BASE + '/a/../b', None),
                         (BASE + '\n', None), ('file:///tmp/test', None)]:
            with self.subTest(base=base), self.assertRaises(ValueError):
                Target.create(base, ws)

    def test_post_redirect_is_never_followed(self):
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, '', {}, 'https://other.invalid'))

    def test_real_ids_must_be_positive_and_bounded(self):
        for value in ('0', '-1', '2147483648', 'abc'):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                positive(value)
        self.assertEqual(positive('57'), 57)

    def test_writes_require_opt_in_confirmed_target_and_direct_readback(self):
        base = ['--scenario', 'demo', '--base-url', BASE, '--vehicle-id', '9', '--demo-id', '7',
                '--output', 'new.json']
        for missing in ('--allow-writes', '--rds-readback', '--confirm-target'):
            complete = base + ['--allow-writes', '--rds-readback', '--confirm-target', BASE]
            index = complete.index(missing)
            del complete[index:index+2 if missing == '--confirm-target' else index+1]
            with self.subTest(missing=missing), redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                validated_args(complete)

    def test_remote_http_cannot_send_api_key_even_with_allow_http(self):
        args = ['--scenario', 'demo', '--base-url', 'http://api.example.invalid', '--vehicle-id', '9',
                '--demo-id', '7', '--output', 'new.json', '--allow-writes', '--rds-readback',
                '--confirm-target', 'http://api.example.invalid', '--allow-http']
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            validated_args(args)

    def test_all_requires_a_different_real_vehicle(self):
        args = ['--scenario', 'all', '--base-url', BASE, '--vehicle-id', '9', '--other-vehicle-id', '9',
                '--command-id', '77', '--demo-id', '7', '--output', 'new.json', '--allow-writes',
                '--rds-readback', '--confirm-target', BASE]
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            validated_args(args)


class EvidenceAndReadbackTests(unittest.TestCase):
    def test_existing_output_stops_reads_before_any_network(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'old.json'
            path.write_text('previous evidence', encoding='utf-8')
            with patch('scripts.check_http_reads.HttpClient') as http:
                self.assertEqual(check_http_reads.main(['--base-url', BASE, '--output', str(path)]), 1)
                http.assert_not_called()
            with patch('scripts.check_websocket_reads.check', new_callable=AsyncMock) as ws:
                self.assertEqual(check_websocket_reads.main(['--base-url', BASE, '--output', str(path)]), 1)
                ws.assert_not_called()
            self.assertEqual(path.read_text(encoding='utf-8'), 'previous evidence')

    def test_history_order_limit_and_untruncated_comparison(self):
        rows = [{'id_historial': i, 'id_vehiculo': 9, 'fecha_hora': '2026-10-20'} for i in range(22, 10, -1)]
        self.assertTrue(history_is_ordered(rows[:10], 9))
        self.assertFalse(history_is_ordered(rows, 9))
        self.assertFalse(history_is_ordered(rows[:10][::-1], 9))
        before = {'count': 12, 'max_id': 22, 'rows': rows, 'sha256': fingerprint(rows)}
        after = deepcopy(before)
        after['rows'][-1]['id_historial'] = 1  # Cambio fuera del GET de diez.
        self.assertFalse(history_unchanged(before, after))

    def test_initial_includes_real_state_and_timestamp(self):
        state = {'id_comando': 77, 'comando': 'DERECHA', 'codigo': 4, 'conectado': False,
                 'fecha_actualizacion': '2026-10-20 10:00:00'}
        event = {**state, 'tipo': 'estado_inicial', 'id_vehiculo': 9,
                 'fecha_hora': '2026-10-20T10:00:00+00:00'}
        self.assertTrue(initial_matches(event, state, 9))
        event['id_comando'] = 88
        self.assertFalse(initial_matches(event, state, 9))

    def test_rds_snapshot_uses_bound_real_id_count_and_readonly_transaction(self):
        manager, repository = MagicMock(), Mock()
        cursor = manager.cursor.return_value.__enter__.return_value
        rows = [{'id_historial': 71, 'id_vehiculo': 9}]
        cursor.fetchone.side_effect = [{'schema_name': 'vehiculo_iot'}, {'count': 1, 'max_id': 71}]
        cursor.fetchall.side_effect = [[], rows, []]
        repository.obtener_vehiculo.return_value = {'id_vehiculo': 9}
        repository.obtener_estado.return_value = None
        repository.obtener_historial.return_value = []
        repository.obtener_demos.return_value = []
        repository.obtener_movimientos_demo.return_value = []
        settings = SimpleNamespace(mysql_database='vehiculo_iot', mysql_host='test-only' + '.rds.amazonaws.com')
        with patch('scripts.verify_public.Settings', return_value=settings), \
             patch('scripts.verify_public.MySQLConnectionManager', return_value=manager), \
             patch('scripts.verify_public.VehiculoRepository', return_value=repository):
            snapshot = rds_snapshot(9, 7)
        manager.transaction.assert_called_once_with(readonly=True)
        self.assertEqual(snapshot['rows'], rows)
        self.assertEqual(snapshot['max_id'], 71)
        self.assertEqual([c.args[1] for c in cursor.execute.call_args_list[1:]], [(9,), (9,)])
        self.assertNotIn('host', snapshot)


class ScenarioBackend:
    """Simulador exclusivo de tests; el verificador real nunca lo importa."""
    def __init__(self):
        self.rows = [{'id_historial': i, 'id_vehiculo': 9, 'id_comando': 77,
                      'fecha_hora': '2026-10-20T10:00:00+00:00', 'origen': 'TEST'} for i in range(100, 115)]
        self.states = {i: {'id_vehiculo': i, 'vehiculo': 'TEST', 'id_comando': 77,
                          'comando': 'DERECHA', 'codigo': 4, 'conectado': False,
                          'fecha_actualizacion': '2026-10-20T10:00:00+00:00'} for i in (9, 21)}
        self.expected = [dict(orden=i+1, id_comando=cmd, comando=name, codigo=code,
                              duracion_ms=1000, pausa_ms=0, id_demo_movimiento=30+i)
                         for i, (cmd, name, code) in enumerate(((77, 'DERECHA', 4), (88, 'IZQUIERDA', 3)))]
        self.start_count, self.posts = 0, []
        self.active = None
        self.capture = None

    def history_join(self):
        return [{k: v for k, v in {**r, 'comando': 'DERECHA', 'codigo': 4}.items() if k != 'id_comando'}
                for r in self.rows[::-1]]

    def snapshot(self, vehicle, demo=None):
        rows = self.rows if vehicle == 9 else []
        return deepcopy({'rds_endpoint_confirmed': True, 'schema': 'vehiculo_iot',
                         'vehicle': {'id_vehiculo': vehicle, 'activo': True}, 'state': self.states[vehicle],
                         'rows': rows, 'count': len(rows), 'max_id': rows[-1]['id_historial'] if rows else None,
                         'sha256': fingerprint(rows), 'history_join': self.history_join() if rows else [],
                         'demos': [{'id_demo': 7, 'activo': True, 'id_vehiculo': vehicle}],
                         'movements': self.expected if demo else []})

    def emit(self, kind, movement=None, received=None):
        payload = {'tipo': kind, 'id_vehiculo': 9, 'id_demo': 7,
                   'id_ejecucion': self.active, 'total_movimientos': 2,
                   'fecha_hora': '2026-10-20T10:00:00+00:00'}
        payload.update(movement or {})
        for client in self.capture.clients[:2]:
            client['events'].append({'payload': deepcopy(payload),
                                     'received_monotonic_ms': received or time.monotonic()*1000 - 10})

    async def aget(self, path):
        if path.endswith('/estado'):
            return deepcopy(self.states[9])
        if path.endswith('/historial'):
            return deepcopy(self.history_join()[:10])
        if path.endswith('/comandos'):
            return [dict(id_comando=c, activo=True, id_vehiculo=9) for c in (77, 88)]
        if path.endswith('/movimientos'):
            return deepcopy(self.expected)
        raise AssertionError('unexpected_fake_get')

    def request(self, method, path, body, expect=None):
        self.posts.append(path)
        if path.endswith('/movimientos'):
            self.rows.append(dict(id_historial=115, id_vehiculo=9, id_comando=77,
                                  fecha_hora='2026-10-20T10:00:00+00:00', origen=body['origen']))
            payload = {'tipo': 'movimiento_individual', 'id_vehiculo': 9,
                       'id_comando': 77, 'comando': 'DERECHA', 'codigo': 4, 'conectado': False,
                       'fecha_hora': self.states[9]['fecha_actualizacion']}
            for client in self.capture.clients[:2]:
                client['events'].append({'payload': deepcopy(payload), 'received_monotonic_ms': time.monotonic()*1000})
            return 200, dict(id_vehiculo=9, id_comando=77, origen=body['origen'], conectado=False,
                             estado=deepcopy(self.states[9]), historial=deepcopy(self.history_join()[0]))
        if path.endswith('/iniciar'):
            if self.active:
                # El segundo inicio se rechaza y luego completa el primer UUID.
                self.emit('demo_movimiento', self.expected[1], self.base_time+1000)
                self.emit('demo_fin', received=self.base_time+2000)
                self.states[9].update({k: self.expected[-1][k] for k in ('id_comando', 'comando', 'codigo')})
                self.active = None
                return 409, None
            self.start_count += 1
            self.active = f'00000000-0000-4000-8000-{self.start_count:012d}'
            self.base_time = time.monotonic()*1000 - 50
            self.emit('demo_inicio', received=self.base_time-1)
            self.emit('demo_movimiento', self.expected[0], self.base_time)
            self.states[9].update({k: self.expected[0][k] for k in ('id_comando', 'comando', 'codigo')})
            return 202, dict(id_vehiculo=9, id_demo=7, id_ejecucion=self.active,
                             total_movimientos=2, estado='iniciada')
        if path.endswith('/cancelar'):
            execution = self.active
            self.emit('demo_cancelada')
            self.active = None
            return 200, dict(id_vehiculo=9, id_demo=7, id_ejecucion=execution,
                             total_movimientos=2, estado='cancelada')
        raise AssertionError('unexpected_fake_post')


class FakeCapture(CaptureGroup):
    def __init__(self, backend, *args):
        super().__init__(*args)
        self.backend = backend
        backend.capture = self
        self.closed = False

    async def open(self, state, other_state=None):
        for number, vehicle, reference in ((1, 9, state), (2, 9, state), (3, 21, other_state)):
            self.clients.append({'client': number, 'id_vehiculo': vehicle, 'initial_ok': True,
                                 'initial_state': deepcopy(reference), 'events': []})
        return self

    async def wait(self, predicate, timeout):
        if not predicate():
            raise AssertionError('fake_required_event_missing')

    async def close(self):
        self.closed = True


class FullScenarioTests(unittest.TestCase):
    def test_all_with_two_clients_other_vehicle_full_history_and_cancel(self):
        backend = ScenarioBackend()
        args = SimpleNamespace(vehicle_id=9, other_vehicle_id=21, command_id=77, demo_id=7,
                               scenario='all', origin='TEST_PUBLIC', quiet_seconds=1)
        target = Target.create(BASE)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'new.json'
            writer = EvidenceWriter(path)
            verifier = PublicVerifier(args, target, writer, backend)
            @asynccontextmanager
            async def reconnect_socket(*args, **kwargs):
                state = backend.states[9]
                payload = {**state, 'tipo': 'estado_inicial',
                           'fecha_hora': state['fecha_actualizacion']}
                yield SimpleNamespace(recv=AsyncMock(return_value=json.dumps(payload)))
            with patch('scripts.verify_public.rds_snapshot', side_effect=backend.snapshot), \
                 patch('scripts.verify_public.dotenv_values', return_value={}), \
                 patch('scripts.verify_public.CaptureGroup', side_effect=lambda *a: FakeCapture(backend, *a)), \
                 patch('scripts.verify_public.connect', side_effect=reconnect_socket), \
                 patch('scripts.verify_public.asyncio.sleep', new_callable=AsyncMock):
                result = asyncio.run(verifier.run())
            writer.close()
            self.assertEqual(result['status'], 'success', result['errors'])
            self.assertTrue(backend.capture.closed)
            self.assertEqual(len(result['post_log']), 5)
            self.assertEqual([p['http_status'] for p in result['post_log']], [200, 202, 409, 202, 200])
            self.assertEqual(result['movement']['after']['count'], 16)
            self.assertTrue(result['demo']['history_identical'])
            self.assertTrue(result['cancellation']['no_steps_after_response'])
            self.assertTrue(result['checks']['other_vehicle_isolated'])
            self.assertFalse(result['thunder_client_verified'])
            saved = json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual(saved['demo']['history_before']['rows'], saved['demo']['history_after']['rows'])

    def test_ambiguous_post_is_journaled_once_without_retry(self):
        args = SimpleNamespace(vehicle_id=9, demo_id=7, scenario='demo')
        target = Target.create(BASE)
        http = Mock()
        http.request.side_effect = TimeoutError('private-server-details-must-not-be-logged')
        with tempfile.TemporaryDirectory() as folder:
            writer = EvidenceWriter(Path(folder)/'new.json')
            verifier = PublicVerifier(args, target, writer, http)
            with self.assertRaises(TimeoutError):
                asyncio.run(verifier.post('complete_demo_start', '/vehiculos/9/demos/7/iniciar', expect=202))
            writer.file.seek(0)
            journal = json.load(writer.file)
            writer.close()
        self.assertEqual(http.request.call_count, 1)
        self.assertEqual(len(journal['post_log']), 1)
        self.assertEqual(journal['status'], 'post_attempted')
        self.assertNotIn('private-server-details', json.dumps(journal))

    def test_redaction_covers_nested_evidence_fields(self):
        args = SimpleNamespace(vehicle_id=9, demo_id=7, scenario='demo')
        verifier = PublicVerifier(args, Target.create(BASE), Mock(preflight={}), Mock())
        verifier.private_values = ['unit-test-secret-value']
        self.assertEqual(verifier.redact({'events': [{'mensaje': 'unit-test-secret-value'}]}),
                         {'events': [{'mensaje': '[REDACTADO]'}]})

    def test_unexpected_acceptance_keeps_uuid_and_is_not_retried(self):
        args = SimpleNamespace(vehicle_id=9, demo_id=7, scenario='all')
        http = Mock()
        http.request.return_value = (202, {'id_ejecucion': '00000000-0000-4000-8000-000000000003'})
        with tempfile.TemporaryDirectory() as folder:
            writer = EvidenceWriter(Path(folder)/'new.json')
            verifier = PublicVerifier(args, Target.create(BASE), writer, http)
            try:
                with self.assertRaises(VerificationFailure):
                    asyncio.run(verifier.post('second_demo_start_must_be_rejected',
                                             '/vehiculos/9/demos/7/iniciar', expect=409))
                self.assertEqual(verifier.report['post_log'][0]['response']['id_ejecucion'],
                                 '00000000-0000-4000-8000-000000000003')
                http.request.assert_called_once()
            finally:
                writer.close()

    def test_late_movement_after_cancellation_invalidates_closed_capture(self):
        args = SimpleNamespace(vehicle_id=9, other_vehicle_id=None, demo_id=7, scenario='all')
        verifier = PublicVerifier(args, Target.create(BASE), Mock(preflight={}), Mock())
        verifier.capture = CaptureGroup(Target.create(BASE), 9)
        execution = '00000000-0000-4000-8000-000000000002'
        events = [dict(payload={'tipo': kind, 'id_ejecucion': execution}, received_monotonic_ms=when)
                  for kind, when in [('demo_cancelada', 50), ('demo_movimiento', 101)]]
        verifier.capture.clients = [{'events': deepcopy(events)}, {'events': deepcopy(events)}]
        verifier.report['cancellation'] = {'execution_id': execution}
        verifier.report['post_log'] = [{'operation': 'cancel_own_demo', 'response_received_monotonic_ms': 100}]
        with self.assertRaises(VerificationFailure):
            verifier.validate_closed_capture()


if __name__ == '__main__':
    unittest.main()
