"""Dos clientes Python WS/WSS de lectura, reconexion y cierre 4404; cero POST."""

import argparse
import asyncio
import json

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from scripts.verification_common import (
    HttpClient, Target, VerificationFailure, add_target_arguments, initial_matches,
    now, output_writer, require,
)


async def check(args, target):
    report = {'checked_at_utc': now(), 'status': 'failed', 'base_url': target.base,
              'ws_base_url': target.ws_base, 'client': 'Python websockets', 'writes': 0,
              'control_posts': 0, 'simultaneous_clients': 0, 'reconnection': False,
              'unknown_vehicle_close': None, 'clients': [], 'warnings': []}
    try:
        http = HttpClient(target)
        vehicles = await http.aget('/vehiculos')
        ids = {v['id_vehiculo'] for v in vehicles}
        if not ids:
            report['status'] = 'partial'
            report['warnings'].append('Sin vehiculos para un monitor real; no se crean datos.')
            return report
        vehicle = args.vehicle_id if args.vehicle_id is not None else vehicles[0]['id_vehiculo']
        require(vehicle in ids, 'requested_vehicle_missing')
        state = await http.aget(f'/vehiculos/{vehicle}/estado')

        async def initial(socket):
            raw = json.loads(await asyncio.wait_for(socket.recv(), 30))
            require(initial_matches(raw, state, vehicle), 'initial_state_does_not_match_http')
            return raw

        async with connect(target.ws(vehicle), open_timeout=30, close_timeout=3) as first:
            async with connect(target.ws(vehicle), open_timeout=30, close_timeout=3) as second:
                report['clients'] = await asyncio.gather(initial(first), initial(second))
                report['simultaneous_clients'] = 2
                await first.close()
                # Relee estado: la reconexion debe representar el estado persistido actual.
                state = await http.aget(f'/vehiculos/{vehicle}/estado')
                async with connect(target.ws(vehicle), open_timeout=30, close_timeout=3) as fresh:
                    report['reconnected_initial'] = await initial(fresh)
                    report['reconnection'] = True
        missing = next(i for i in range(1, len(ids)+2) if i not in ids)
        async with connect(target.ws(missing), open_timeout=30, close_timeout=3) as invalid:
            try:
                await asyncio.wait_for(invalid.recv(), 30)
                require(False, 'unknown_vehicle_received_initial')
            except ConnectionClosed as error:
                code = error.rcvd.code if error.rcvd else None
                require(code == 4404, 'unknown_vehicle_close_not_4404')
                report['unknown_vehicle_close'] = code
        report['status'] = 'ok'
    except Exception as error:
        report['warnings'].append(str(error) if isinstance(error, VerificationFailure)
                                  else 'No se completo la verificacion de lectura WS/WSS.')
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    add_target_arguments(parser)
    args = parser.parse_args(argv)
    try:
        target = Target.create(args.base_url, args.ws_base_url)
    except ValueError:
        parser.error('Destino HTTP/WS invalido; no se admiten credenciales en URL.')
    writer = None
    try:
        writer = output_writer(args.output)
        report = asyncio.run(check(args, target))
        report['writer_preflight'] = writer.preflight
        writer.write(report)
    except Exception:
        print('Salida no disponible o comprobacion incompleta; no se sobrescribio evidencia anterior.')
        return 1
    finally:
        if writer:
            writer.close()
    print(f"Monitor Python: {report['status']}; clientes simultaneos: {report['simultaneous_clients']}.")
    print('POST 0; escrituras RDS 0. Archivo:', str(args.output.resolve()))
    return 0 if report['status'] == 'ok' else 1


if __name__ == '__main__':
    raise SystemExit(main())
