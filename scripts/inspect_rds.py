r"""Inspecciona metadatos reales de RDS, sin leer filas ni ejecutar procedimientos.

Ejecutar desde la raíz: .\.venv\Scripts\python.exe -m scripts.inspect_rds
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.config.settings import Settings, get_settings
from app.database.connection import DatabaseUnavailableError, MySQLConnectionManager

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_COUNTS = {"tables": 6, "procedures": 21}

# Solo metadatos. Los filtros de esquema se enlazan como parámetros, nunca SQL.
TABLES_SQL = """
SELECT TABLE_NAME AS name, TABLE_TYPE AS table_type, ENGINE AS engine
FROM information_schema.TABLES
WHERE TABLE_SCHEMA = %s
ORDER BY TABLE_NAME
"""
COLUMNS_SQL = """
SELECT TABLE_NAME AS table_name, COLUMN_NAME AS name,
       ORDINAL_POSITION AS ordinal_position, DATA_TYPE AS data_type,
       COLUMN_TYPE AS full_type, IS_NULLABLE AS is_nullable,
       COLUMN_DEFAULT AS default_value, EXTRA AS extra,
       CHARACTER_SET_NAME AS character_set, COLLATION_NAME AS collation,
       GENERATION_EXPRESSION AS generation_expression
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA = %s
ORDER BY TABLE_NAME, ORDINAL_POSITION
"""
CONSTRAINTS_SQL = """
SELECT tc.TABLE_NAME AS table_name, tc.CONSTRAINT_NAME AS name,
       tc.CONSTRAINT_TYPE AS constraint_type, kcu.COLUMN_NAME AS column_name,
       kcu.ORDINAL_POSITION AS ordinal_position,
       kcu.POSITION_IN_UNIQUE_CONSTRAINT AS position_in_unique_constraint,
       kcu.REFERENCED_TABLE_SCHEMA AS referenced_schema,
       kcu.REFERENCED_TABLE_NAME AS referenced_table,
       kcu.REFERENCED_COLUMN_NAME AS referenced_column,
       rc.UPDATE_RULE AS on_update, rc.DELETE_RULE AS on_delete
FROM information_schema.TABLE_CONSTRAINTS AS tc
LEFT JOIN information_schema.KEY_COLUMN_USAGE AS kcu
  ON kcu.CONSTRAINT_SCHEMA = tc.CONSTRAINT_SCHEMA
 AND kcu.TABLE_NAME = tc.TABLE_NAME AND kcu.CONSTRAINT_NAME = tc.CONSTRAINT_NAME
LEFT JOIN information_schema.REFERENTIAL_CONSTRAINTS AS rc
  ON rc.CONSTRAINT_SCHEMA = tc.CONSTRAINT_SCHEMA
 AND rc.TABLE_NAME = tc.TABLE_NAME AND rc.CONSTRAINT_NAME = tc.CONSTRAINT_NAME
WHERE tc.CONSTRAINT_SCHEMA = %s
  AND tc.CONSTRAINT_TYPE IN ('PRIMARY KEY', 'UNIQUE', 'FOREIGN KEY')
ORDER BY tc.TABLE_NAME, tc.CONSTRAINT_NAME, kcu.ORDINAL_POSITION
"""
PROCEDURES_SQL = """
SELECT ROUTINE_NAME AS name, SQL_DATA_ACCESS AS sql_data_access,
       SECURITY_TYPE AS security_type, IS_DETERMINISTIC AS is_deterministic
FROM information_schema.ROUTINES
WHERE ROUTINE_SCHEMA = %s AND ROUTINE_TYPE = 'PROCEDURE'
ORDER BY ROUTINE_NAME
"""
PARAMETERS_SQL = """
SELECT SPECIFIC_NAME AS procedure_name, PARAMETER_NAME AS name,
       ORDINAL_POSITION AS ordinal_position, PARAMETER_MODE AS mode,
       DATA_TYPE AS data_type, DTD_IDENTIFIER AS full_type,
       CHARACTER_SET_NAME AS character_set, COLLATION_NAME AS collation
FROM information_schema.PARAMETERS
WHERE SPECIFIC_SCHEMA = %s AND ROUTINE_TYPE = 'PROCEDURE'
ORDER BY SPECIFIC_NAME, ORDINAL_POSITION
"""


def quote_identifier(identifier: str) -> str:
    """Escapa identificadores de SHOW CREATE; no admite múltiples sentencias."""
    return "`" + identifier.replace("`", "``") + "`"


def _private_value_pattern(value: str) -> str:
    """Reconoce el valor literal y sus escapes habituales de MySQL."""
    escaped_forms = {
        "\\": ("\\\\", "\\"),
        "'": ("\\'", "''", "'"),
        '"': ('\\"', '""', '"'),
        "`": ("``", "`"),
        "\0": ("\\0", "\0"),
        "\b": ("\\b", "\b"),
        "\n": ("\\n", "\n"),
        "\r": ("\\r", "\r"),
        "\t": ("\\t", "\t"),
        "\x1a": ("\\Z", "\\\x1a", "\x1a"),
    }
    parts = []
    for character in value:
        forms = escaped_forms.get(character, (character,))
        alternatives = "|".join(re.escape(form) for form in forms)
        parts.append(f"(?:{alternatives})" if len(forms) > 1 else alternatives)
    return "".join(parts)


def redact_definition(sql: str, settings: Settings) -> tuple[str, list[str]]:
    """Omite la cuenta DEFINER y cualquier valor privado de conexión presente."""
    account_part = r"(?:`(?:``|[^`])*`|'(?:''|[^'])*'|[^\s@]+)"
    sql, removed = re.subn(
        rf"\bDEFINER\s*=\s*{account_part}\s*@\s*{account_part}\s*",
        "",
        sql,
        flags=re.IGNORECASE,
    )
    redactions = ["Cuenta DEFINER omitida."] if removed else []
    # No se exporta la configuración: solo se usa en memoria para redactar SQL.
    for value in (
        settings.mysql_host,
        settings.mysql_user,
        settings.mysql_password.get_secret_value(),
    ):
        if value:
            sql, removed = re.subn(_private_value_pattern(value), "[REDACTADO]", sql)
            if removed and "Valores privados omitidos." not in redactions:
                redactions.append("Valores privados omitidos.")
    return sql, redactions


def query_rows(manager: MySQLConnectionManager, connection: Any, sql: str,
               parameters: tuple[str, ...]) -> list[dict[str, Any]]:
    with manager.cursor(connection, dictionary=True) as cursor:
        cursor.execute(sql, parameters)
        return cursor.fetchall()


def empty_catalog() -> dict[str, Any]:
    return {
        "catalog_version": 1,
        "inspected_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "unavailable",
        "schema": None,
        "read_only": True,
        "visibility": "Objetos visibles para la cuenta utilizada; no acredita acceso global.",
        "expected_counts": EXPECTED_COUNTS.copy(),
        "actual_counts": {"tables": None, "procedures": None, "views": None},
        "count_checks": {"tables_match": None, "procedures_match": None},
        "tables": [],
        "views": [],
        "procedures": [],
        "warnings": [],
        "error": None,
    }


def inspect_schema(manager: MySQLConnectionManager, settings: Settings,
                   catalog: dict[str, Any]) -> None:
    schema = settings.mysql_database
    with manager.transaction(readonly=True) as connection:
        # Abrir la conexión selecciona el esquema; las consultas usan ese mismo nombre.
        tables = query_rows(manager, connection, TABLES_SQL, (schema,))
        catalog["schema"] = schema
        catalog["tables"] = [dict(t, columns=[], constraints=[]) for t in tables
                             if t["table_type"] == "BASE TABLE"]
        catalog["views"] = [dict(t, columns=[]) for t in tables
                            if t["table_type"] != "BASE TABLE"]
        catalog["actual_counts"]["tables"] = len(catalog["tables"])
        catalog["actual_counts"]["views"] = len(catalog["views"])
        by_table = {t["name"]: t for t in catalog["tables"] + catalog["views"]}
        for column in query_rows(manager, connection, COLUMNS_SQL, (schema,)):
            table_name = column.pop("table_name")
            by_table[table_name]["columns"].append(column)

        grouped_constraints: dict[tuple[str, str], dict[str, Any]] = {}
        for row in query_rows(manager, connection, CONSTRAINTS_SQL, (schema,)):
            key = (row["table_name"], row["name"])
            if key not in grouped_constraints:
                constraint = {"name": row["name"], "type": row["constraint_type"],
                              "columns": [], "on_update": row["on_update"],
                              "on_delete": row["on_delete"]}
                grouped_constraints[key] = constraint
                by_table[row["table_name"]]["constraints"].append(constraint)
            grouped_constraints[key]["columns"].append({
                field: row[field] for field in (
                    "column_name", "ordinal_position", "position_in_unique_constraint",
                    "referenced_schema", "referenced_table", "referenced_column",
                )
            })

        catalog["procedures"] = query_rows(manager, connection, PROCEDURES_SQL, (schema,))
        catalog["actual_counts"]["procedures"] = len(catalog["procedures"])
        parameters = query_rows(manager, connection, PARAMETERS_SQL, (schema,))
        for procedure in catalog["procedures"]:
            procedure["parameters"] = [
                {key: value for key, value in p.items() if key != "procedure_name"}
                for p in parameters if p["procedure_name"] == procedure["name"]
            ]
            procedure["parameter_metadata_status"] = "visible_metadata"
            procedure["definition"] = {"available": False, "sql": None,
                                       "redactions": [], "unavailable_reason": None}
            try:
                with manager.cursor(connection, dictionary=True) as cursor:
                    cursor.execute(
                        f"SHOW CREATE PROCEDURE {quote_identifier(schema)}."
                        f"{quote_identifier(procedure['name'])}"
                    )
                    result = cursor.fetchone()
                definition = result.get("Create Procedure") if result else None
                if definition:
                    sql, redactions = redact_definition(definition, settings)
                    procedure["definition"].update(
                        available=True, sql=sql, redactions=redactions,
                    )
                else:
                    procedure["definition"]["unavailable_reason"] = (
                        "SHOW CREATE PROCEDURE no devolvió la definición; "
                        "puede requerir permisos adicionales."
                    )
            except DatabaseUnavailableError as error:
                # Solo una falta de permisos permite continuar en la misma sesión.
                if error.code != "permission":
                    raise
                procedure["definition"]["unavailable_reason"] = (
                    "Permisos insuficientes para SHOW CREATE PROCEDURE."
                )
            if not procedure["definition"]["available"]:
                catalog["warnings"].append(
                    f"Definición no disponible: {procedure['name']}."
                )
                if not procedure["parameters"]:
                    procedure["parameter_metadata_status"] = "not_verifiable"
                    catalog["warnings"].append(
                        f"Sin parámetros visibles para {procedure['name']}; "
                        "no se puede confirmar que carezca de parámetros."
                    )

    for kind in EXPECTED_COUNTS:
        count = catalog["actual_counts"][kind]
        matches = count == EXPECTED_COUNTS[kind]
        catalog["count_checks"][f"{kind}_match"] = matches
        if not matches:
            catalog["warnings"].append(
                f"Conteo distinto para {kind}: esperado {EXPECTED_COUNTS[kind]}, "
                f"visible {count}. Revisar esquema y permisos; no se modificó la base."
            )
    catalog["status"] = "partial" if catalog["warnings"] else "complete"


def md_cell(value: Any) -> str:
    if value is None:
        return "—"
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def render_markdown(catalog: dict[str, Any]) -> str:
    counts = catalog["actual_counts"]
    display_count = lambda kind: "no obtenido" if counts[kind] is None else counts[kind]
    lines = [
        "# Contrato del esquema RDS", "",
        f"Estado: **{catalog['status']}**. Inspección UTC: {catalog['inspected_at_utc']}.", "",
        "Inspección de solo lectura. No se consultaron filas de negocio ni se ejecutaron "
        "procedimientos. Se omiten configuración de conexión, credenciales y cuentas DEFINER.", "",
        f"Esquema inspeccionado: {md_cell(catalog['schema'])}.", "",
        f"Tablas: **{display_count('tables')}** (esperadas: 6). "
        f"Procedimientos: **{display_count('procedures')}** (esperados: 21). "
        f"Vistas: {display_count('views')}.", "",
        catalog["visibility"], "",
    ]
    if catalog["error"]:
        lines += [f"Problema: **{catalog['error']['code']}**. "
                  f"{catalog['error']['message']}", "",
                  "Los valores no obtenidos quedan en null en el JSON; no representan cero.", ""]
    if catalog["warnings"]:
        lines += ["## Observaciones", ""] + [f"- {w}" for w in catalog["warnings"]] + [""]
    for table in catalog["tables"] + catalog["views"]:
        lines += [f"## {table['table_type']}: {md_cell(table['name'])}", "",
                  "| Orden | Columna | Tipo completo | Nulo | Predeterminado | Extra |",
                  "| --- | --- | --- | --- | --- | --- |"]
        for column in table["columns"]:
            lines.append("| " + " | ".join(md_cell(column[k]) for k in (
                "ordinal_position", "name", "full_type", "is_nullable", "default_value", "extra",
            )) + " |")
        lines += ["", "Claves y relaciones:", ""]
        for constraint in table.get("constraints", []):
            columns = ", ".join(c["column_name"] for c in constraint["columns"])
            detail = f"{constraint['type']} `{constraint['name']}` ({columns})"
            if constraint["type"] == "FOREIGN KEY":
                references = ", ".join(
                    f"{c['referenced_schema']}.{c['referenced_table']}.{c['referenced_column']}"
                    for c in constraint["columns"]
                )
                detail += (f" → {references}; ON UPDATE {constraint['on_update']}; "
                           f"ON DELETE {constraint['on_delete']}")
            lines.append(f"- {detail}")
        if not table.get("constraints"):
            lines.append("- Sin claves primarias, UNIQUE o foráneas visibles.")
        lines.append("")
    for procedure in catalog["procedures"]:
        lines += [f"## Procedimiento: {md_cell(procedure['name'])}", "",
                  "| Orden | Nombre | Modo | Tipo completo |",
                  "| --- | --- | --- | --- |"]
        for parameter in procedure.get("parameters", []):
            lines.append("| " + " | ".join(md_cell(parameter[k]) for k in (
                "ordinal_position", "name", "mode", "full_type",
            )) + " |")
        if not procedure.get("parameters"):
            lines.append("| — | Sin parámetros visibles | — | — |")
        definition = procedure.get("definition", {})
        if definition.get("available"):
            lines += ["", "Definición obtenida mediante SHOW CREATE PROCEDURE.", ""]
            lines += [f"- {r}" for r in definition["redactions"]]
            sql = definition["sql"]
            # Evita que un literal de SQL cierre prematuramente el bloque Markdown.
            longest = max((len(m) for m in re.findall(r"`+", sql)), default=0)
            fence = "`" * max(3, longest + 1)
            lines += ["", f"{fence}sql", sql, fence, ""]
        else:
            lines += ["", "**Definición no disponible:** " + (
                definition.get("unavailable_reason")
                or "La inspección terminó antes de consultarla."
            ), ""]
    lines += ["## Fuentes y alcance", "",
              "Metadatos: information_schema.TABLES, COLUMNS, TABLE_CONSTRAINTS, "
              "KEY_COLUMN_USAGE, REFERENTIAL_CONSTRAINTS, ROUTINES y PARAMETERS. "
              "Las consultas se ejecutan en una transacción READ ONLY y los procedimientos "
              "solo se describen; nunca se invocan.", "",
              "El tipo completo de los parámetros procede de DTD_IDENTIFIER, según la "
              "[documentación MySQL](https://dev.mysql.com/doc/refman/8.0/en/"
              "information-schema-parameters-table.html). La definición depende de los "
              "[permisos de SHOW CREATE PROCEDURE](https://dev.mysql.com/doc/refman/8.0/en/"
              "show-create-procedure.html).", ""]
    return "\n".join(lines)


def main() -> int:
    catalog = empty_catalog()
    try:
        settings = get_settings()
        inspect_schema(MySQLConnectionManager(settings), settings, catalog)
    except (ValidationError, OSError):
        catalog["error"] = {"code": "configuration", "message": (
            "Configuración MySQL incompleta o inválida. Revisa las cinco variables MYSQL_* en .env."
        )}
    except DatabaseUnavailableError as error:
        catalog["error"] = {"code": error.code, "message": error.message}
    except Exception:
        # La consola no debe exponer SQL, valores de configuración ni tracebacks del driver.
        catalog["error"] = {"code": "inspection", "message": (
            "La inspección no pudo completarse por un error interno; revisar el script y permisos."
        )}
    if catalog["error"]:
        catalog["status"] = "partial" if catalog["schema"] is not None else "unavailable"
    output_dir = PROJECT_ROOT / "docs"
    try:
        output_dir.mkdir(exist_ok=True)
        (output_dir / "contrato_rds.json").write_text(
            json.dumps(catalog, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        (output_dir / "contrato_rds.md").write_text(render_markdown(catalog), encoding="utf-8")
    except OSError:
        print("No fue posible guardar el catálogo en docs; revisa los permisos locales.")
        return 1
    print("Catálogo guardado en docs/contrato_rds.json y docs/contrato_rds.md.")
    print(f"Estado de la inspección: {catalog['status']}.")
    for kind, label in (("tables", "Tablas"), ("procedures", "Procedimientos")):
        count = catalog["actual_counts"][kind]
        print(f"{label}: {'no obtenido' if count is None else count}; "
              f"esperados: {EXPECTED_COUNTS[kind]}.")
    if catalog["error"]:
        print(f"Causa ({catalog['error']['code']}): {catalog['error']['message']}")
        return 1
    if catalog["warnings"]:
        print("Hay diferencias de conteo o metadatos no disponibles; consulta el catálogo.")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
