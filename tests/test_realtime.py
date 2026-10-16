"""Pruebas aisladas de WebSocket y tareas: sin credenciales, red ni escrituras RDS."""

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from app.database.connection import DatabaseUnavailableError
from app.errors import ApplicationError
from app.models.events import VehicleEvent, state_event
from app.models.vehiculos import MovimientoEjecutar
from app.runtime import AppRuntime, CoordinatedVehiculoService
from app.services.coordination import VehicleCoordinator
from app.services.demo_data import DemoDataService, DemoPlan
from app.services.vehiculos import VehiculoService
from app.websocket.manager import ConnectionManager
from tests.test_services import harness, initial_data


async def until(predicate, timeout=2):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.001)


class Socket:
    def __init__(self, blocked=False, failing=False):
        self.sent = []
        self.close_codes = []
        self.gate = asyncio.Event()
        self.entered = asyncio.Event()
        self.failing = failing
        if not blocked:
            self.gate.set()

    async def send_json(self, payload):
        self.entered.set()
        await self.gate.wait()
        if self.failing:
            raise RuntimeError("private client failure")
        self.sent.append(deepcopy(payload))

    async def close(self, code=1000):
        self.close_codes.append(code)


class WebSocketManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.manager = ConnectionManager(max_queue_size=2, send_timeout=0.1)
        self.manager.bind_loop()

    async def asyncTearDown(self):
        await self.manager.shutdown()

    async def add(self, vehicle, socket=None):
        socket = socket or Socket()
        client = await self.manager.register(socket, vehicle, VehicleEvent(tipo="estado_inicial", id_vehiculo=vehicle))
        return socket, client

    async def test_two_clients_and_other_vehicle_isolation_initial_first(self):
        a, _ = await self.add(1)
        b, _ = await self.add(1)
        other, _ = await self.add(2)
        await self.manager.publish(VehicleEvent(tipo="movimiento_individual", id_vehiculo=1))
        await until(lambda: len(a.sent) == len(b.sent) == 2 and len(other.sent) == 1)
        self.assertEqual([x["tipo"] for x in a.sent], ["estado_inicial", "movimiento_individual"])
        self.assertEqual(a.sent[1:], b.sent[1:])
        self.assertEqual(b.sent[0]["tipo"], "estado_inicial")
        self.assertEqual(other.sent[0]["id_vehiculo"], 2)

    async def test_slow_client_overflow_does_not_block_fast_client(self):
        slow, client = await self.add(1, Socket(blocked=True))
        fast, _ = await self.add(1)
        await slow.entered.wait()
        await until(lambda: len(fast.sent) == 1)
        for index in range(5):
            await self.manager.publish(VehicleEvent(tipo="demo_movimiento", id_vehiculo=1, orden=index + 1))
            await until(lambda: len(fast.sent) == index + 2)
        await until(lambda: bool(slow.close_codes))
        self.assertTrue(client.closed)
        self.assertEqual(slow.close_codes, [1013])
        self.assertEqual(len(fast.sent), 6)

    async def test_send_failure_timeout_reconnection_and_shutdown(self):
        bad, client = await self.add(1, Socket(failing=True))
        await until(lambda: client.closed)
        slow, slow_client = await self.add(1, Socket(blocked=True))
        await until(lambda: slow_client.closed)
        reconnected, fresh = await self.add(1)
        await asyncio.to_thread(self.manager.publish_threadsafe, VehicleEvent(tipo="demo_fin", id_vehiculo=1))
        await until(lambda: len(reconnected.sent) == 2)
        self.assertEqual(reconnected.sent[0]["tipo"], "estado_inicial")
        await self.manager.disconnect(fresh)
        await self.manager.shutdown()
        self.assertFalse(self.manager.clients)
        self.assertTrue(all(t.done() for t in self.manager._tasks))

    def test_timestamp_is_explicit_utc_and_missing_state_not_connected(self):
        event = state_event("estado_inicial", 1, None)
        self.assertIsNone(event.conectado)
        self.assertTrue(event.model_dump(mode="json")["fecha_hora"].endswith("Z"))
        event = state_event("estado_inicial", 1, {"conectado": False, "fecha_actualizacion": datetime(2026, 1, 1)})
        self.assertFalse(event.conectado)
        self.assertEqual(event.fecha_hora.tzinfo, timezone.utc)


class DemoDataTests(unittest.TestCase):
    def service(self, base, repository):
        return DemoDataService(base.manager, repository)

    def test_prepare_uses_real_n_sorted_movements_without_writes(self):
        for count in (1, 3, 7):
            data = initial_data()
            template = data["movements"][51]
            data["movements"] = {50 + i: dict(template, id_demo_movimiento=50 + i, orden=i)
                                 for i in range(1, count + 1)}
            with harness(data) as (base, repo, conn, durable):
                plan = self.service(base, repo).prepare(1, 31)
                self.assertEqual(plan.total_movimientos, count)
                self.assertEqual([m["orden"] for m in plan.movimientos], list(range(1, count + 1)))
                self.assertEqual(repo.writes, [])
                self.assertEqual(conn.starts, [True])

    def test_invalid_or_empty_demo_foreign_inactive_and_signed_times(self):
        cases = ("empty", "foreign_demo", "inactive_vehicle", "inactive_demo", "inactive_command",
                 "foreign_command", "bad_duration", "negative_pause", "unsigned_overflow", "bad_order")
        for case in cases:
            with self.subTest(case=case):
                data = initial_data()
                demo = 31
                if case == "empty":
                    data["movements"].clear()
                elif case == "foreign_demo":
                    demo = 41
                elif case.startswith("inactive_"):
                    category = {"inactive_vehicle": "vehicles", "inactive_demo": "demos", "inactive_command": "commands"}[case]
                    key = {"vehicles": 1, "demos": 31, "commands": 11}[category]
                    data[category][key]["activo"] = False
                elif case == "foreign_command":
                    data["movements"][51]["id_comando"] = 21
                else:
                    field, value = {"bad_duration": ("duracion_ms", 0), "negative_pause": ("pausa_ms", -1),
                                    "unsigned_overflow": ("duracion_ms", 2**31), "bad_order": ("orden", 0)}[case]
                    data["movements"][51][field] = value
                with harness(data) as (base, repo, conn, durable):
                    with self.assertRaises(ApplicationError) as raised:
                        self.service(base, repo).prepare(1, demo)
                    self.assertIn(raised.exception.status_code, (404, 409))
                    self.assertFalse(repo.writes)

    def test_step_preserves_connected_commits_without_any_history(self):
        for connected in (True, False, None):
            data = initial_data()
            if connected is None:
                data["states"].clear()
            else:
                data["states"][1]["conectado"] = connected
            with harness(data) as (base, repo, conn, durable):
                before = deepcopy(durable["history"])
                result = self.service(base, repo).step(1, 31, data["movements"][51])
                self.assertEqual(result["conectado"], connected if connected is not None else False)
                self.assertEqual([w[0] for w in repo.writes], ["actualizar_estado"])
                self.assertEqual(conn.commits, 1)
                self.assertTrue(conn.closed)
                self.assertEqual(durable["history"], before)

    def test_failed_step_rolls_back_and_snapshot_does_not_write(self):
        with harness() as (base, repo, conn, durable):
            before = deepcopy(durable)
            original = repo.actualizar_estado

            def fail(*args):
                original(*args)
                raise DatabaseUnavailableError("query")

            repo.actualizar_estado = fail
            with self.assertRaises(DatabaseUnavailableError):
                self.service(base, repo).step(1, 31, initial_data()["movements"][51])
            self.assertEqual(conn.rollbacks, 1)
            self.assertEqual(durable, before)
        with harness() as (base, repo, conn, durable):
            state = self.service(base, repo).snapshot(1)
            self.assertTrue(state["conectado"])
            self.assertEqual(repo.writes, [])


class FakeData:
    def __init__(self, count=3, duration=1):
        self.count = count
        self.duration = duration
        self.pause = 0
        self.calls = []
        self.committed = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.block = False
        self.fail_order = None
        self.state = None

    def prepare(self, vehicle, demo):
        rows = tuple({"id_demo_movimiento": i, "id_comando": i, "orden": i,
                      "comando": f"Command {i}", "codigo": i,
                      "duracion_ms": self.duration, "pausa_ms": self.pause}
                     for i in range(1, self.count + 1))
        return DemoPlan(vehicle, demo, rows)

    def step(self, vehicle, demo, movement):
        self.calls.append((vehicle, movement["orden"]))
        self.started.set()
        if self.block and not self.release.wait(3):
            raise RuntimeError("test RDS worker timeout")
        if self.fail_order == movement["orden"]:
            raise RuntimeError("SENSITIVE_PRIVATE_DATABASE_ERROR")
        self.committed.append((vehicle, movement["orden"]))
        self.state = {"id_vehiculo": vehicle, "vehiculo": "Test", "id_comando": movement["id_comando"],
                      "comando": movement["comando"], "codigo": movement["codigo"], "conectado": False,
                      "fecha_actualizacion": datetime.now(timezone.utc)}
        return dict(self.state)

    def snapshot(self, vehicle):
        return self.state


class DemoRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.data = FakeData()
        self.runtime = AppRuntime(self.data)
        self.runtime.bind_loop()
        self.socket = Socket()
        self.client = await asyncio.to_thread(self.runtime.subscribe, self.socket, 1)

    async def asyncTearDown(self):
        self.data.release.set()
        await self.runtime.shutdown()

    async def start(self, vehicle=1, demo=31):
        return await asyncio.to_thread(self.runtime.demos.start, vehicle, demo)

    async def test_n_ordered_committed_steps_single_terminal_and_no_history_interface(self):
        for count in (1, 3, 5):
            with self.subTest(count=count):
                self.data.count = count
                start = await self.start()
                await until(lambda: any(e["tipo"] == "demo_fin" and e["id_ejecucion"] == start["id_ejecucion"] for e in self.socket.sent))
                events = [e for e in self.socket.sent if e["id_ejecucion"] == start["id_ejecucion"]]
                self.assertEqual([e["tipo"] for e in events], ["demo_inicio"] + ["demo_movimiento"] * count + ["demo_fin"])
                self.assertEqual([e["orden"] for e in events if e["tipo"] == "demo_movimiento"], list(range(1, count + 1)))
                self.assertTrue(all(e["total_movimientos"] == count for e in events))
                self.assertIsNone(self.runtime.coordinator.get_active(1))
                await until(lambda: not self.runtime.demos.tasks)
                self.assertFalse(self.runtime.demos.tasks)
        self.assertFalse(hasattr(self.data, "registrar_historial"))

    async def test_concurrent_starts_only_one_reservation_and_other_vehicle_available(self):
        self.data.duration = 60_000
        results = await asyncio.gather(self.start(), self.start(), return_exceptions=True)
        self.assertEqual(sum(isinstance(r, dict) for r in results), 1)
        error = next(r for r in results if isinstance(r, Exception))
        self.assertIsInstance(error, ApplicationError)
        self.assertEqual(error.status_code, 409)
        with self.assertRaises(ApplicationError):
            self.runtime.coordinator.ensure_available(1)
        self.runtime.coordinator.ensure_available(2)
        second = await self.start(2, 41)
        self.assertNotEqual(next(r["id_ejecucion"] for r in results if isinstance(r, dict)), second["id_ejecucion"])

    async def test_cancel_wait_interrupts_and_never_runs_later_steps(self):
        self.data.duration = 60_000
        start = await self.start()
        await until(lambda: len(self.data.committed) == 1)
        result = await asyncio.wait_for(self.runtime.demos.cancel(1, 31), 1)
        self.assertEqual(result["estado"], "cancelada")
        self.assertEqual(result["id_ejecucion"], start["id_ejecucion"])
        await asyncio.sleep(0.02)
        self.assertEqual(self.data.calls, [(1, 1)])
        await until(lambda: any(e["tipo"] == "demo_cancelada" for e in self.socket.sent))
        terminals = [e for e in self.socket.sent if e["tipo"] in {"demo_fin", "demo_cancelada", "demo_error"}]
        self.assertEqual(len(terminals), 1)

    async def test_cancel_during_rds_waits_for_thread_before_acknowledgement(self):
        self.data.block = True
        await self.start()
        await until(self.data.started.is_set)
        cancellation = asyncio.create_task(self.runtime.demos.cancel(1, 31))
        await asyncio.sleep(0.02)
        self.assertFalse(cancellation.done())
        self.assertEqual(self.data.committed, [])
        self.data.release.set()
        result = await asyncio.wait_for(cancellation, 1)
        self.assertEqual(result["estado"], "cancelada")
        committed_at_ack = list(self.data.committed)
        await asyncio.sleep(0.02)
        self.assertEqual(self.data.committed, committed_at_ack)
        self.assertEqual(self.data.calls, [(1, 1)])
        await until(lambda: any(e["tipo"] == "demo_cancelada" for e in self.socket.sent))
        self.assertEqual([e["tipo"] for e in self.socket.sent][-2:], ["demo_movimiento", "demo_cancelada"])

    async def test_cancel_during_pause_interrupts_before_next_movement(self):
        self.data.duration = 1
        self.data.pause = 60_000
        await self.start()
        await until(lambda: len(self.data.committed) == 1)
        await asyncio.sleep(0.02)
        response = await asyncio.wait_for(self.runtime.demos.cancel(1, 31), 1)
        self.assertEqual(response["estado"], "cancelada")
        self.assertEqual(self.data.calls, [(1, 1)])

    async def test_error_is_safe_single_terminal_and_vehicle_can_restart(self):
        self.data.fail_order = 2
        start = await self.start()
        await until(lambda: any(e["tipo"] == "demo_error" for e in self.socket.sent))
        self.assertNotIn("SENSITIVE", json.dumps(self.socket.sent))
        self.assertEqual(self.data.calls, [(1, 1), (1, 2)])
        self.assertIsNone(self.runtime.coordinator.get_active(1))
        self.data.fail_order = None
        new = await self.start()
        self.assertNotEqual(new["id_ejecucion"], start["id_ejecucion"])
        await until(lambda: any(e["tipo"] == "demo_fin" for e in self.socket.sent))

    async def test_stop_waits_worker_blocks_mutations_and_is_only_logical(self):
        self.data.block = True
        await self.start()
        await until(self.data.started.is_set)
        stopping = asyncio.create_task(self.runtime.demos.stop(1))
        await asyncio.sleep(0.02)
        self.assertFalse(stopping.done())
        facade = CoordinatedVehiculoService(self.runtime, Mock())
        with self.assertRaises(ApplicationError) as error:
            await asyncio.to_thread(facade.eliminar_vehiculo, 1)
        self.assertEqual(error.exception.status_code, 409)
        self.data.release.set()
        response = await asyncio.wait_for(stopping, 1)
        self.assertTrue(response["detencion_logica"])
        self.assertEqual(self.data.calls, [(1, 1)])
        await until(lambda: self.socket.sent[-1]["tipo"] == "detencion_logica")
        self.runtime.coordinator.ensure_available(1)

    async def test_shutdown_drains_running_rds_and_clients(self):
        self.data.block = True
        await self.start()
        await until(self.data.started.is_set)
        closing = asyncio.create_task(self.runtime.shutdown())
        await asyncio.sleep(0.02)
        self.assertFalse(closing.done())
        self.data.release.set()
        await asyncio.wait_for(closing, 1)
        self.assertFalse(self.runtime.demos.tasks)
        self.assertFalse(self.runtime.connections.clients)

    async def test_shutdown_during_preparation_cannot_leave_a_late_execution(self):
        original = self.data.prepare
        entered, release = threading.Event(), threading.Event()

        def prepare(*args):
            entered.set()
            if not release.wait(2):
                raise RuntimeError("test prepare timeout")
            return original(*args)

        self.data.prepare = prepare
        starting = asyncio.create_task(self.start())
        await until(entered.is_set)
        closing = asyncio.create_task(self.runtime.shutdown())
        await asyncio.sleep(0.01)
        release.set()
        with self.assertRaises(ApplicationError) as error:
            await starting
        self.assertEqual(error.exception.status_code, 503)
        await closing
        self.assertFalse(self.runtime.coordinator.active_executions())
        self.assertFalse(self.runtime.demos.tasks)

    def test_old_owner_cleanup_cannot_remove_new_execution(self):
        coord = VehicleCoordinator()
        first = SimpleNamespace(id_ejecucion="old")
        second = SimpleNamespace(id_ejecucion="new")
        coord.reserve(1, first)
        self.assertTrue(coord.clear_if_owner(1, "old"))
        coord.reserve(1, second)
        self.assertFalse(coord.clear_if_owner(1, "old"))
        self.assertIs(coord.get_active(1), second)


class IndividualNotificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_event_only_after_actual_manager_commit_and_no_event_for_rollback(self):
        for failure in (False, True):
            with self.subTest(failure=failure):
                events = []
                with harness(fail_history=failure) as (service, repo, connection, durable):
                    connections = Mock()

                    def publish(event):
                        self.assertEqual(connection.commits, 1)
                        self.assertTrue(connection.closed)
                        events.append(event)

                    connections.publish_threadsafe.side_effect = publish
                    runtime = AppRuntime(FakeData(), connections)
                    facade = CoordinatedVehiculoService(runtime, service)
                    if failure:
                        with self.assertRaises(DatabaseUnavailableError):
                            await asyncio.to_thread(facade.ejecutar_movimiento, 1, MovimientoEjecutar(id_comando=11, origen="HTTP"))
                        self.assertEqual(connection.rollbacks, 1)
                        self.assertEqual(events, [])
                    else:
                        result = await asyncio.to_thread(facade.ejecutar_movimiento, 1, MovimientoEjecutar(id_comando=11, origen="HTTP"))
                        self.assertTrue(result.conectado)
                        self.assertEqual(len(events), 1)
                        self.assertEqual(events[0].tipo, "movimiento_individual")
                        self.assertEqual(events[0].fecha_hora.tzinfo, timezone.utc)

    async def test_broadcast_failure_after_commit_does_not_fail_or_repeat_write(self):
        with harness() as (service, repo, connection, durable):
            connections = Mock()
            connections.publish_threadsafe.side_effect = RuntimeError("disconnected monitor")
            facade = CoordinatedVehiculoService(AppRuntime(FakeData(), connections), service)
            result = await asyncio.to_thread(facade.ejecutar_movimiento, 1, MovimientoEjecutar(id_comando=11, origen="HTTP"))
            self.assertEqual(result.id_comando, 11)
            self.assertEqual(connection.commits, 1)
            self.assertEqual(len(repo.writes), 2)
            self.assertEqual(connection.rollbacks, 0)


if __name__ == "__main__":
    unittest.main()
