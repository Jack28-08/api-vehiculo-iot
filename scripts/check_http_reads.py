"""GET reales parametrizados; no envia POST, claves API ni escrituras RDS."""

import argparse
import json

from scripts.verification_common import (
    HttpClient, Target, VerificationFailure, add_target_arguments,
    history_is_ordered, now, output_writer, positive, require,
)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    add_target_arguments(parser)
    parser.add_argument('--demo-id', type=positive, help='DEMO del vehiculo elegido; si falta se busca una existente.')
    args = parser.parse_args(argv)
    try:
        target = Target.create(args.base_url, args.ws_base_url)
    except ValueError:
        parser.error('Destino HTTP/WS invalido; no se admiten credenciales en URL.')
    report = {'checked_at_utc': now(), 'status': 'failed', 'base_url': target.base,
              'read_only': True, 'writes': 0, 'requests': [], 'warnings': []}
    writer = None
    kinds = set()
    try:
        # Antes de hacer red: crear/probar salida exclusiva.
        writer = output_writer(args.output)
        report['writer_preflight'] = writer.preflight
        http = HttpClient(target)

        def get(path, expected=200, is_json=True):
            status, value = http.request('GET', path, expect=expected, as_json=is_json)
            entry = {'method': 'GET', 'path': path, 'status': status}
            if isinstance(value, list):
                entry['items'] = len(value)
            report['requests'].append(entry)
            return value

        require(get('/health') == {'status': 'ok'}, 'api_health_invalid')
        require(get('/health/db') == {'status': 'ok'}, 'db_health_invalid')
        require('SwaggerUIBundle' in get('/docs', is_json=False), 'swagger_invalid')
        schema = get('/openapi.json')
        require(schema['components']['securitySchemes']['ApiKey']['name'] == 'X-API-Key', 'api_key_schema_invalid')
        require(all(op['post'].get('security') for op in schema['paths'].values() if 'post' in op),
                'unprotected_post_in_openapi')
        vehicles = get('/vehiculos')
        kinds.add('vehicles')
        ids = {v['id_vehiculo'] for v in vehicles}
        if args.vehicle_id is not None:
            require(args.vehicle_id in ids, 'requested_vehicle_missing')
            selected = [args.vehicle_id]
        else:
            selected = [v['id_vehiculo'] for v in vehicles[:3]]
        demo_owner = None
        for identifier in selected:
            prefix = f'/vehiculos/{identifier}'
            require(get(prefix)['id_vehiculo'] == identifier, 'vehicle_id_mismatch')
            kinds.add('vehicle')
            require(all(c['id_vehiculo'] == identifier for c in get(prefix+'/comandos')), 'commands_not_owned')
            kinds.add('commands')
            state = get(prefix+'/estado')
            require(state is None or state['id_vehiculo'] == identifier, 'state_not_owned')
            kinds.add('state')
            history = get(prefix+'/historial')
            require(history_is_ordered(history, identifier), 'history_not_top_ten_descending')
            kinds.add('history')
            demos = get(prefix+'/demos')
            require(all(d['id_vehiculo'] == identifier for d in demos), 'demos_not_owned')
            kinds.add('demos')
            if args.demo_id is not None:
                require(any(d['id_demo'] == args.demo_id for d in demos), 'requested_demo_not_owned')
                demo_owner = (identifier, args.demo_id)
                break
            if demos and demo_owner is None:
                demo_owner = (identifier, demos[0]['id_demo'])
        if demo_owner:
            owner, demo = demo_owner
            rows = get(f'/vehiculos/{owner}/demos/{demo}/movimientos')
            require([r['orden'] for r in rows] == sorted(r['orden'] for r in rows), 'demo_unordered')
            kinds.add('demo_movements')
            other = next((identifier for identifier in ids if identifier != owner), None)
            if other is not None:
                get(f'/vehiculos/{other}/demos/{demo}/movimientos', expected=404)
        else:
            report['warnings'].append('Sin DEMO disponible: no se simulan datos.')
        missing = next(i for i in range(1, len(ids)+2) if i not in ids)
        get(f'/vehiculos/{missing}', expected=404)
        get(f'/vehiculos/{missing}/historial', expected=404)
        if not selected:
            report['warnings'].append('Sin vehiculos existentes.')
        report['status'] = 'partial' if report['warnings'] else 'ok'
    except Exception as error:
        report['warnings'].append(str(error) if isinstance(error, VerificationFailure)
                                  else 'No se completo la lectura o una respuesta no cumple el contrato.')
    report['functional_get_kinds_checked'] = len(kinds)
    try:
        if writer is None:
            print('Salida no disponible; no se sobrescribio ninguna evidencia.')
            return 1
        writer.write(report)
    finally:
        if writer:
            writer.close()
    print(f"HTTP real: {report['status']}; GET de negocio {len(kinds)}/7; escrituras 0.")
    print('Archivo:', str(args.output.resolve()))
    return 0 if report['status'] == 'ok' else 1


if __name__ == '__main__':
    raise SystemExit(main())
