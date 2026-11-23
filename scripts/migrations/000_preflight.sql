-- Solo lectura. Guardar esta salida antes de ejecutar cualquier DDL.
SELECT DATABASE() AS esquema, @@version AS mysql, @@session.time_zone AS zona,
       UTC_TIMESTAMP() AS inspeccion_utc;

SELECT TABLE_NAME, ENGINE, TABLE_COLLATION
FROM information_schema.TABLES
WHERE TABLE_SCHEMA=DATABASE()
  AND TABLE_NAME IN ('vehiculos','comandos','estado_vehiculo','historial_comandos','demos','demo_movimientos')
ORDER BY TABLE_NAME;

SELECT TABLE_NAME,COLUMN_NAME,COLUMN_TYPE,IS_NULLABLE,COLUMN_DEFAULT,EXTRA
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA=DATABASE()
  AND TABLE_NAME IN ('vehiculos','comandos','estado_vehiculo','historial_comandos','demos','demo_movimientos')
ORDER BY TABLE_NAME,ORDINAL_POSITION;

SELECT k.TABLE_NAME,k.CONSTRAINT_NAME,k.COLUMN_NAME,k.REFERENCED_TABLE_NAME,k.REFERENCED_COLUMN_NAME,
       r.UPDATE_RULE,r.DELETE_RULE
FROM information_schema.KEY_COLUMN_USAGE k
LEFT JOIN information_schema.REFERENTIAL_CONSTRAINTS r ON r.CONSTRAINT_SCHEMA=k.CONSTRAINT_SCHEMA
     AND r.TABLE_NAME=k.TABLE_NAME AND r.CONSTRAINT_NAME=k.CONSTRAINT_NAME
WHERE k.TABLE_SCHEMA=DATABASE()
  AND k.TABLE_NAME IN ('vehiculos','comandos','estado_vehiculo','historial_comandos','demos','demo_movimientos')
ORDER BY k.TABLE_NAME,k.CONSTRAINT_NAME,k.ORDINAL_POSITION;

SELECT TABLE_NAME,INDEX_NAME,NON_UNIQUE,SEQ_IN_INDEX,COLUMN_NAME
FROM information_schema.STATISTICS
WHERE TABLE_SCHEMA=DATABASE()
  AND TABLE_NAME IN ('vehiculos','comandos','estado_vehiculo','historial_comandos','demos','demo_movimientos')
ORDER BY TABLE_NAME,INDEX_NAME,SEQ_IN_INDEX;

SELECT ROUTINE_NAME,SQL_DATA_ACCESS
FROM information_schema.ROUTINES
WHERE ROUTINE_SCHEMA=DATABASE() AND ROUTINE_TYPE='PROCEDURE'
ORDER BY ROUTINE_NAME;

SELECT 'vehiculos' tabla,COUNT(*) filas FROM vehiculos
UNION ALL SELECT 'comandos',COUNT(*) FROM comandos
UNION ALL SELECT 'estado_vehiculo',COUNT(*) FROM estado_vehiculo
UNION ALL SELECT 'historial_comandos',COUNT(*) FROM historial_comandos
UNION ALL SELECT 'demos',COUNT(*) FROM demos
UNION ALL SELECT 'demo_movimientos',COUNT(*) FROM demo_movimientos;

SELECT 'comandos sin vehículo' AS comprobacion,COUNT(*) AS incidentes
FROM comandos c LEFT JOIN vehiculos v USING(id_vehiculo) WHERE v.id_vehiculo IS NULL
UNION ALL SELECT 'demos sin vehículo',COUNT(*)
FROM demos d LEFT JOIN vehiculos v USING(id_vehiculo) WHERE v.id_vehiculo IS NULL
UNION ALL SELECT 'pasos sin DEMO o con comando ajeno',COUNT(*)
FROM demo_movimientos p LEFT JOIN demos d USING(id_demo) LEFT JOIN comandos c USING(id_comando)
WHERE d.id_demo IS NULL OR c.id_comando IS NULL OR c.id_vehiculo<>d.id_vehiculo
UNION ALL SELECT 'estado sin vehículo o con comando ajeno',COUNT(*)
FROM estado_vehiculo e LEFT JOIN vehiculos v USING(id_vehiculo) LEFT JOIN comandos c USING(id_comando)
WHERE v.id_vehiculo IS NULL OR (e.id_comando IS NOT NULL AND (c.id_comando IS NULL OR c.id_vehiculo<>e.id_vehiculo))
UNION ALL SELECT 'historial sin vehículo o con comando ajeno',COUNT(*)
FROM historial_comandos h LEFT JOIN vehiculos v USING(id_vehiculo) LEFT JOIN comandos c USING(id_comando)
WHERE v.id_vehiculo IS NULL OR (h.id_comando IS NOT NULL AND (c.id_comando IS NULL OR c.id_vehiculo<>h.id_vehiculo));
