"""Pruebas reales optativas 2.6 con clientes Python; no acreditan Thunder Client.

Los POST requieren --allow-writes, --confirm-target y --rds-readback.
No se reintenta ningun POST, ni siquiera si se pierde su respuesta. Cada archivo
es nuevo, se prueba antes de la red y registra intentos durables antes de enviar.
Los lectores WS acumulan en memoria; nunca guardan evidencia dentro de recv().
"""

import argparse
import asyncio
from contextlib import AsyncExitStack
import json
import os
from pathlib import Path
import time
from uuid import UUID
from urllib.parse import urlsplit

from dotenv import dotenv_values
from websockets.asyncio.client import connect

from app.config.settings import Settings
from app.database.connection import MySQLConnectionManager
from app.repositories.vehiculos import VehiculoRepository
from app.security import ApiKeySettings
from scripts.verify_demo_real import validate_client
from scripts.verification_common import (
    HttpClient, Target, VerificationFailure, add_target_arguments, fingerprint,
    history_is_ordered, initial_matches, now, output_writer, positive, require,
    serializable, timestamp,
)


def rds_snapshot(vehicle_id, demo_id=None):
    """Conexion nueva y READ ONLY; COUNT y filas completas en la misma instantanea.

    Solo estas consultas auxiliares SELECT y los SP de lectura existentes.
    No exporta host, cuenta ni opciones de conexion.
    """
    settings = Settings()
    require(settings.mysql_database == 'vehiculo_iot', 'unexpected_rds_schema')
    require(settings.mysql_host.lower().rstrip('.').endswith(
        ('.rds.amazonaws.com', '.rds.amazonaws.com.cn')), 'not_an_rds_endpoint')
    manager = MySQLConnectionManager(settings)
    repository = VehiculoRepository(manager)
    with manager.transaction(readonly=True) as connection:
        with manager.cursor(connection, dictionary=True) as cursor:
            cursor.execute('SELECT DATABASE() AS schema_name')
            schema = cursor.fetchone()['schema_name']
            cursor.fetchall()
            cursor.execute('SELECT * FROM historial_comandos WHERE id_vehiculo=%s ORDER BY id_historial',
                           (vehicle_id,))
            rows = cursor.fetchall()
            cursor.execute('SELECT COUNT(*) AS count, MAX(id_historial) AS max_id '
                           'FROM historial_comandos WHERE id_vehiculo=%s', (vehicle_id,))
            counts = cursor.fetchone()
            cursor.fetchall()
        vehicle = repository.obtener_vehiculo(connection, vehicle_id)
        state = repository.obtener_estado(connection, vehicle_id)
        history_join = repository.obtener_historial(connection, vehicle_id)
        demos = repository.obtener_demos(connection, vehicle_id)
        movements = repository.obtener_movimientos_demo(connection, demo_id) if demo_id else []
    require(schema == 'vehiculo_iot', 'unexpected_selected_schema')
    require(counts['count'] == len(rows), 'rds_count_does_not_match_rows')
    return serializable({'rds_endpoint_confirmed': True, 'schema': schema, 'vehicle': vehicle,
                         'state': state, 'rows': rows, 'count': counts['count'],
                         'max_id': counts['max_id'], 'sha256': fingerprint(serializable(rows)),
                         'history_join': history_join, 'demos': demos, 'movements': movements})


def canonical_history(rows):
    return [{**r, 'fecha_hora': timestamp(r['fecha_hora']).isoformat() if r.get('fecha_hora') else None}
            for r in rows]


def state_matches(left, right):
    if left is None or right is None:
        return left is right
    fields = ('id_vehiculo', 'id_comando', 'comando', 'codigo', 'conectado')
    return (all(left.get(f) == right.get(f) for f in fields)
            and ((not left.get('fecha_actualizacion') and not right.get('fecha_actualizacion'))
                 or (left.get('fecha_actualizacion') and right.get('fecha_actualizacion')
                     and timestamp(left['fecha_actualizacion']) == timestamp(right['fecha_actualizacion']))))


def check_individual_persistence(before, after, response, vehicle_id, command_id, origin):
    previous = {r['id_historial']: r for r in before['rows']}
    current = {r['id_historial']: r for r in after['rows']}
    added = [r for r in after['rows'] if r['id_historial'] not in previous]
    connected = bool(before['state']['conectado']) if before['state'] else False
    return (after['count'] == before['count'] + 1 and len(added) == 1
            and all(current.get(identifier) == row for identifier, row in previous.items())
            and added[0]['id_historial'] == response['historial']['id_historial']
            and added[0]['id_vehiculo'] == vehicle_id and added[0]['id_comando'] == command_id
            and added[0]['origen'] == origin and response['origen'] == origin
            and response['id_vehiculo'] == vehicle_id and response['id_comando'] == command_id
            and state_matches(response['estado'], after['state'])
            and after['state']['id_comando'] == command_id
            and after['state']['conectado'] == connected and response['conectado'] == connected
            and canonical_history([response['historial']]) == canonical_history(after['history_join'][:1]))


def history_unchanged(before, after):
    return (before['count'] == after['count'] and before['max_id'] == after['max_id']
            and before['rows'] == after['rows'] and before['sha256'] == after['sha256'])


class CaptureGroup:
    """Dos clientes objetivo y uno opcional ajeno; eventos solo en memoria."""

    def __init__(self, target, vehicle_id, other_vehicle_id=None):
        self.target, self.vehicle_id, self.other_id = target, vehicle_id, other_vehicle_id
        self.clients, self.tasks = [], []
        self.changed = asyncio.Event()
        self.stack = AsyncExitStack()

    async def open(self, state, other_state=None):
        try:
            for number, vehicle_id, reference in [(1, self.vehicle_id, state), (2, self.vehicle_id, state)] + (
                [(3, self.other_id, other_state)] if self.other_id else []
            ):
                socket = await self.stack.enter_async_context(connect(
                    self.target.ws(vehicle_id), open_timeout=30, close_timeout=3, max_queue=64))
                payload = json.loads(await asyncio.wait_for(socket.recv(), 30))
                client = {'client': number, 'id_vehiculo': vehicle_id, 'initial_state': payload,
                          'initial_ok': initial_matches(payload, reference, vehicle_id), 'events': []}
                self.clients.append(client)
                require(client['initial_ok'], 'initial_state_not_equal_to_rds')
                self.tasks.append(asyncio.create_task(self.collect(client, socket)))
        except BaseException:
            await self.close()
            raise
        return self

    async def collect(self, client, socket):
        try:
            while True:
                payload = json.loads(await socket.recv())
                require(len(client['events']) < 10000, 'event_capture_limit_exceeded')
                client['events'].append({'payload': payload, 'received_at_utc': now(),
                                         'received_monotonic_ms': time.monotonic() * 1000})
                self.changed.set()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            client['reader_error'] = type(error).__name__
            self.changed.set()

    async def wait(self, predicate, timeout):
        deadline = time.monotonic() + timeout
        while not predicate():
            require(not any(c.get('reader_error') for c in self.clients), 'websocket_reader_failed')
            self.changed.clear()
            if predicate():
                break
            await asyncio.wait_for(self.changed.wait(), max(0.01, deadline - time.monotonic()))

    def events(self, client_index, execution_id=None, kind=None):
        return [e for e in self.clients[client_index]['events']
                if (execution_id is None or e['payload'].get('id_ejecucion') == execution_id)
                and (kind is None or e['payload'].get('tipo') == kind)]

    async def terminal(self, execution_id, timeout):
        kinds = {'demo_fin', 'demo_error', 'demo_cancelada'}
        await self.wait(lambda: all(any(e['payload'].get('tipo') in kinds
                                       for e in self.events(i, execution_id)) for i in (0, 1)), timeout)

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.stack.aclose()


class PublicVerifier:
    def __init__(self, args, target, writer, http):
        self.args, self.target, self.writer, self.http = args, target, writer, http
        self.prefix = f'/vehiculos/{args.vehicle_id}'
        self.report = {'started_at_utc': now(), 'status': 'prepared', 'client': 'Python websockets',
                       'thunder_client_verified': False, 'base_url': target.base, 'ws_base_url': target.ws_base,
                       'scenario': args.scenario, 'id_vehiculo': args.vehicle_id, 'id_demo': args.demo_id,
                       'writer_preflight': writer.preflight, 'post_log': [], 'checks': {}, 'errors': [],
                       'capture_storage': 'memory_only_until_listeners_closed', 'websocket_clients': []}
        self.private_values = []
        self.capture = None

    async def snapshot(self):
        return await asyncio.to_thread(rds_snapshot, self.args.vehicle_id, self.args.demo_id)

    async def post(self, phase, path, body=None, *, expect=200):
        # Diario durable antes del envio, fuera de los lectores y fuera del bucle.
        entry = {'operation': phase, 'method': 'POST', 'path': path,
                 'attempted_at_utc': now(), 'expected_http': expect, 'http_status': None}
        self.report['post_log'].append(entry)
        journal = {'status': 'post_attempted', 'started_at_utc': self.report['started_at_utc'],
                   'base_url': self.target.base, 'post_log': serializable(self.report['post_log']),
                   'message': 'No reintentar; los eventos se conservan en memoria hasta terminar.'}
        await asyncio.to_thread(self.writer.write, self.redact(journal))
        status, value = await asyncio.to_thread(self.http.request, 'POST', path, body, expect=None)
        entry['http_status'] = status
        entry['response_received_monotonic_ms'] = time.monotonic() * 1000
        if value is not None:
            entry['response'] = value
        # Una aceptacion inesperada puede haber iniciado una tarea real. Conservar
        # su UUID antes de fallar; nunca ocultarla ni repetir/cancelar automaticamente.
        require(status == expect, f'http_post_{status}_expected_{expect}')
        return value, entry

    def redact(self, value):
        # Respaldo defensivo si un campo de datos contuviera accidentalmente un secreto.
        if isinstance(value, dict):
            return {k: self.redact(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.redact(v) for v in value]
        if isinstance(value, str):
            for secret in self.private_values:
                value = value.replace(secret, '[REDACTADO]')
        return value

    async def check_history_get(self, snapshot, phase):
        rows = await self.http.aget(self.prefix + '/historial')
        ok = (history_is_ordered(rows, self.args.vehicle_id)
              and canonical_history(rows) == canonical_history(snapshot['history_join'][:10]))
        self.report['checks'][phase] = {'ok': ok, 'http_rows': rows, 'rds_total_count': snapshot['count']}
        require(ok, 'history_get_not_top_ten_descending')

    async def movement(self, before):
        command = self.args.command_id
        payload, _ = await self.post('individual_existing_command', self.prefix + '/movimientos',
                                     {'id_comando': command, 'origen': self.args.origin})
        await self.capture.wait(lambda: all(self.capture.events(i, kind='movimiento_individual')
                                            for i in (0, 1)), 30)
        after = await self.snapshot()
        ok = check_individual_persistence(before, after, payload, self.args.vehicle_id, command, self.args.origin)
        require(ok, 'individual_state_or_full_history_not_persisted')
        events = [self.capture.events(i, kind='movimiento_individual') for i in (0, 1)]
        require(all(len(e) == 1 for e in events), 'individual_event_missing_or_duplicated')
        require(events[0][0]['payload'] == events[1][0]['payload'], 'individual_events_differ')
        require(all(e[0]['payload'].get('id_comando') == command
                    and e[0]['payload'].get('conectado') == after['state']['conectado']
                    and e[0]['payload'].get('id_vehiculo') == self.args.vehicle_id
                    and e[0]['payload'].get('comando') == after['state']['comando']
                    and e[0]['payload'].get('codigo') == after['state']['codigo']
                    and timestamp(e[0]['payload']['fecha_hora']) == timestamp(after['state']['fecha_actualizacion'])
                    for e in events), 'individual_event_not_confirmed_state')
        self.report['movement'] = {'before': before, 'after': after, 'state_and_history_persisted': ok,
                                    'same_event_both_clients': True}
        await self.check_history_get(after, 'history_after_individual')
        return after

    async def start_demo(self, phase, expected):
        body, _ = await self.post(phase, self.prefix + f'/demos/{self.args.demo_id}/iniciar', expect=202)
        execution_id = str(UUID(body['id_ejecucion']))
        require(body['id_vehiculo'] == self.args.vehicle_id and body['id_demo'] == self.args.demo_id
                and body['total_movimientos'] == len(expected) and body['estado'] == 'iniciada',
                'demo_start_metadata_mismatch')
        print(f'{phase}: HTTP 202; UUID {execution_id}; N={len(expected)}.', flush=True)
        return execution_id

    async def demo(self, before, expected):
        execution_id = await self.start_demo('complete_demo_start', expected)
        deadline = sum(m['duracion_ms'] + m['pausa_ms'] for m in expected) / 1000 + len(expected) * 15 + 60
        if self.args.scenario == 'all':
            require(not any(self.capture.events(i, execution_id, 'demo_fin') for i in (0, 1)),
                    'demo_finished_before_second_start_test')
            await self.post('second_demo_start_must_be_rejected', self.prefix + f'/demos/{self.args.demo_id}/iniciar',
                            expect=409)
            self.report['checks']['second_start_rejected_409'] = True
        await self.capture.terminal(execution_id, deadline)
        validations = []
        for client in self.capture.clients[:2]:
            validation = {'initial_ok': client['initial_ok'], 'events': client['events']}
            validate_client(validation, execution_id, expected,
                            vehicle_id=self.args.vehicle_id, demo_id=self.args.demo_id)
            validations.append(validation)
        require(all(c['ok'] for c in validations), 'demo_sequence_fields_or_waits_failed')
        require([e['payload'] for e in validations[0]['execution_events']]
                == [e['payload'] for e in validations[1]['execution_events']], 'demo_events_differ')
        after = await self.snapshot()
        require(history_unchanged(before, after), 'demo_changed_full_history')
        require(after['state'] and after['state']['id_comando'] == expected[-1]['id_comando'], 'demo_final_state_mismatch')
        connected = bool(before['state']['conectado']) if before['state'] else False
        require(after['state']['conectado'] == connected, 'demo_changed_connected')
        self.report['demo'] = {'execution_id': execution_id, 'expected_movements': expected,
                               'clients': validations, 'history_before': before, 'history_after': after,
                               'history_identical': True, 'last_state_matches': True}
        await self.check_history_get(after, 'history_after_demo')
        return after

    async def cancel_demo(self, before, expected):
        execution_id = await self.start_demo('cancellation_demo_start', expected)
        await self.capture.wait(lambda: all(self.capture.events(i, execution_id, 'demo_movimiento') for i in (0, 1)), 60)
        require(not any(self.capture.events(i, execution_id, 'demo_fin') for i in (0, 1)),
                'demo_finished_before_cancellation_test')
        body, entry = await self.post('cancel_own_demo', self.prefix + f'/demos/{self.args.demo_id}/cancelar')
        require(body['id_ejecucion'] == execution_id and body['estado'] == 'cancelada', 'demo_not_cancelled')
        await self.capture.terminal(execution_id, 60)
        after_response = await self.snapshot()
        # Ventana finita explícita. No se presenta como prueba de tiempo infinito.
        observation = self.args.quiet_seconds
        await asyncio.sleep(observation)
        after = await self.snapshot()
        for index in (0, 1):
            events = self.capture.events(index, execution_id)
            require(events and events[-1]['payload']['tipo'] == 'demo_cancelada', 'cancelled_terminal_missing')
            movements = [e['payload'] for e in events if e['payload']['tipo'] == 'demo_movimiento']
            require([e['payload']['tipo'] for e in events] == ['demo_inicio']
                    + ['demo_movimiento'] * len(movements) + ['demo_cancelada'], 'cancelled_sequence_invalid')
            require(all(e['payload']['id_vehiculo'] == self.args.vehicle_id
                        and e['payload']['id_demo'] == self.args.demo_id
                        and e['payload']['total_movimientos'] == len(expected) for e in events),
                    'cancelled_metadata_invalid')
            require(0 < len(movements) <= len(expected) and all(
                all(actual.get(f) == reference[f] for f in
                    ('orden', 'id_comando', 'comando', 'codigo', 'duracion_ms', 'pausa_ms'))
                for actual, reference in zip(movements, expected)), 'cancelled_movements_not_plan_prefix')
            require(not any(e['payload']['tipo'] == 'demo_movimiento'
                            and e['received_monotonic_ms'] > entry['response_received_monotonic_ms'] for e in events),
                    'movement_received_after_cancellation_response')
            require([e['payload']['tipo'] for e in events].count('demo_cancelada') == 1,
                    'cancelled_terminal_duplicated')
        require(state_matches(after_response['state'], after['state']), 'state_changed_after_cancellation')
        require(history_unchanged(before, after), 'cancellation_changed_full_history')
        require(after['state']['id_comando'] == movements[-1]['id_comando'], 'cancelled_final_state_mismatch')
        require([e['payload'] for e in self.capture.events(0, execution_id)]
                == [e['payload'] for e in self.capture.events(1, execution_id)], 'cancelled_events_differ')
        self.report['cancellation'] = {'execution_id': execution_id, 'quiet_seconds': observation,
                                      'no_steps_after_response': True, 'history_identical': True,
                                      'before': before, 'after_response': after_response, 'after': after}
        return after

    async def reconnect(self, state):
        async with connect(self.target.ws(self.args.vehicle_id), open_timeout=30, close_timeout=3) as socket:
            initial = json.loads(await asyncio.wait_for(socket.recv(), 30))
        require(initial_matches(initial, state, self.args.vehicle_id), 'reconnection_not_persisted_state')
        self.report['reconnection'] = {'ok': True, 'initial_state': initial}

    def validate_closed_capture(self):
        """Incluye mensajes tardios recibidos durante lecturas/reconexion posteriores."""
        require(not any(c.get('reader_error') for c in self.capture.clients), 'websocket_reader_failed')
        if self.args.other_vehicle_id:
            require(not self.capture.clients[2]['events'], 'other_vehicle_received_unexpected_events')
        if 'demo' in self.report:
            complete = self.report['demo']
            for source, validation in zip(self.capture.clients[:2], complete['clients']):
                validation['events'] = source['events']
                validate_client(validation, complete['execution_id'], complete['expected_movements'],
                                vehicle_id=self.args.vehicle_id, demo_id=self.args.demo_id)
                require(validation['ok'], 'late_demo_event_invalidates_sequence')
        if 'cancellation' in self.report:
            execution = self.report['cancellation']['execution_id']
            response_time = next(e['response_received_monotonic_ms'] for e in self.report['post_log']
                                 if e['operation'] == 'cancel_own_demo')
            for index in (0, 1):
                events = self.capture.events(index, execution)
                require(events and events[-1]['payload']['tipo'] == 'demo_cancelada',
                        'late_event_after_cancelled_terminal')
                require(not any(e['payload']['tipo'] == 'demo_movimiento'
                                and e['received_monotonic_ms'] > response_time for e in events),
                        'movement_received_after_cancellation_response')

    async def run(self):
        self.report['status'] = 'running'
        try:
            private = dotenv_values(Path(__file__).resolve().parents[1] / '.env')
            for name in ('MYSQL_HOST', 'MYSQL_PASSWORD', 'API_KEY_OPERATOR', 'API_KEY_ADMIN'):
                value = os.environ.get(name) or private.get(name)
                if value:
                    self.private_values.append(value)
            before = await self.snapshot()
            self.report['preflight_rds'] = before
            require(before['vehicle'] and before['vehicle']['activo'], 'vehicle_not_active')
            require(state_matches(await self.http.aget(self.prefix + '/estado'), before['state']),
                    'public_api_not_same_state_as_configured_rds')
            await self.check_history_get(before, 'initial_history_top_ten')
            commands = await self.http.aget(self.prefix + '/comandos')
            if self.args.scenario in ('all', 'movement'):
                require(any(c['id_comando'] == self.args.command_id and c['activo']
                            and c['id_vehiculo'] == self.args.vehicle_id for c in commands), 'command_not_owned_or_active')
            expected = before['movements']
            if self.args.scenario in ('all', 'demo'):
                require(any(d['id_demo'] == self.args.demo_id and d['activo'] for d in before['demos']),
                        'demo_not_owned_or_active')
                require(expected and len(expected) <= 5000, 'empty_or_excessive_demo')
                require([r['orden'] for r in expected] == sorted(set(r['orden'] for r in expected)), 'unordered_demo')
                require(all(m['orden'] > 0 and m['duracion_ms'] > 0 and m['pausa_ms'] >= 0
                            and any(c['id_comando'] == m['id_comando'] and c['activo'] for c in commands)
                            for m in expected), 'invalid_demo_movement_or_command')
                require(await self.http.aget(self.prefix + f'/demos/{self.args.demo_id}/movimientos') == expected,
                        'public_demo_plan_not_same_as_rds')
                if self.args.scenario == 'all':
                    require(sum(m['duracion_ms'] + m['pausa_ms'] for m in expected) >= 2000,
                            'demo_too_short_for_control_tests')
            other_state = None
            if self.args.other_vehicle_id:
                other = await asyncio.to_thread(rds_snapshot, self.args.other_vehicle_id)
                require(other['vehicle'] is not None, 'other_vehicle_missing')
                other_state = other['state']
            self.capture = CaptureGroup(self.target, self.args.vehicle_id, self.args.other_vehicle_id)
            await self.capture.open(before['state'], other_state)
            self.report['websocket_clients'] = self.capture.clients
            if self.args.scenario in ('all', 'movement'):
                before = await self.movement(before)
            if self.args.scenario in ('all', 'demo'):
                before = await self.demo(before, expected)
            if self.args.scenario == 'all':
                before = await self.cancel_demo(before, expected)
            await self.reconnect(before['state'])
            if self.args.other_vehicle_id:
                require(not self.capture.clients[2]['events'], 'other_vehicle_received_unexpected_events')
                other_after = await asyncio.to_thread(rds_snapshot, self.args.other_vehicle_id)
                require(state_matches(other_state, other_after['state']), 'other_vehicle_state_changed')
                require(history_unchanged(other, other_after), 'other_vehicle_history_changed')
                self.report['checks']['other_vehicle_isolated'] = True
            require(not any(c.get('reader_error') for c in self.capture.clients), 'websocket_reader_failed')
            self.report['status'] = 'success'
        except Exception as error:
            self.report['status'] = 'failed'
            self.report['errors'].append({'code': str(error) if isinstance(error, VerificationFailure)
                                         else type(error).__name__,
                                         'message': 'Verificacion incompleta. No se reintenta ningun POST.'})
        finally:
            if self.capture:
                await self.capture.close()
            self.report['finished_at_utc'] = now()
        if self.report['status'] == 'success':
            try:
                self.validate_closed_capture()
            except Exception as error:
                self.report['status'] = 'failed'
                self.report['errors'].append({'code': str(error) if isinstance(error, VerificationFailure)
                                             else type(error).__name__,
                                             'message': 'La captura final no permite declarar exito.'})
        # Todos los lectores están cerrados antes del guardado completo.
        self.writer.write(self.redact(serializable(self.report)))
        return self.report


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    add_target_arguments(p)
    p.add_argument('--scenario', choices=('movement', 'demo', 'all'), required=True,
                   help='all: individual, DEMO completa, rechazo de segundo inicio y otra DEMO cancelada.')
    p.add_argument('--other-vehicle-id', type=positive, help='Otro vehiculo real; obligatorio para all.')
    p.add_argument('--command-id', type=positive, help='Comando existente; obligatorio para movement/all.')
    p.add_argument('--demo-id', type=positive, help='DEMO real; obligatorio para demo/all.')
    p.add_argument('--origin', default='EXAMEN_2_6', help='Origen real del POST individual, 1-30 caracteres.')
    p.add_argument('--allow-writes', action='store_true', help='Autoriza las operaciones reales del escenario elegido.')
    p.add_argument('--confirm-target', help='Repite exactamente --base-url para confirmar el destino autorizado.')
    p.add_argument('--rds-readback', action='store_true', help='Autoriza SELECT directos READ ONLY con MYSQL_* locales.')
    p.add_argument('--allow-http', action='store_true', help=argparse.SUPPRESS)
    p.add_argument('--quiet-seconds', type=float, default=3, help='Observacion sin pasos tras cancelar, minimo 1 s.')
    return p


def validated_args(argv=None):
    p = parser()
    args = p.parse_args(argv)
    try:
        target = Target.create(args.base_url, args.ws_base_url)
        require(args.vehicle_id is not None, 'vehicle_id_required')
        require(args.allow_writes and args.rds_readback, 'explicit_writes_and_rds_readback_required')
        require(args.confirm_target == target.base, 'confirm_exact_target_required')
        require(target.base.startswith('https://') or urlsplit(target.base).hostname in ('localhost', '127.0.0.1', '::1'),
                'https_required_for_remote_authenticated_posts')
        require(args.other_vehicle_id != args.vehicle_id, 'other_vehicle_must_differ')
        require(1 <= args.quiet_seconds <= 120, 'quiet_seconds_out_of_range')
        require(0 < len(args.origin.strip()) <= 30, 'invalid_origin')
        if args.scenario in ('all', 'movement'):
            require(args.command_id is not None, 'command_id_required')
        if args.scenario in ('all', 'demo'):
            require(args.demo_id is not None, 'demo_id_required')
        if args.scenario == 'all':
            require(args.other_vehicle_id is not None, 'other_vehicle_id_required')
    except (ValueError, VerificationFailure) as error:
        p.error(str(error))
    return args, target


def main(argv=None):
    args, target = validated_args(argv)
    writer = None
    try:
        writer = output_writer(args.output)
        key = ApiKeySettings().api_key_operator.get_secret_value()
        require(bool(key.strip()), 'private_operator_key_missing')
        verifier = PublicVerifier(args, target, writer, HttpClient(target, key))
        report = asyncio.run(verifier.run())
        print(f"Resultado Python: {report['status']}; POST intentados: {len(report['post_log'])}.")
        print('Archivo:', str(args.output.resolve()))
        print('Thunder Client: no verificado por este script.')
        return 0 if report['status'] == 'success' else 1
    except Exception as error:
        # No mostrar opciones de conexion, errores de Settings ni URLs con secretos.
        print('No se completo la verificacion (' + type(error).__name__ + '); no repetir POST automaticamente.')
        return 1
    finally:
        if writer:
            writer.close()


if __name__ == '__main__':
    raise SystemExit(main())
