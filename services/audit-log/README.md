# audit-log

Bitácora de auditoría de TINT (Prototipo 3, escenario 5: táctica *Audit*, patrón *Security Logger and Auditor*). Consume `audit:events` de Redis y guarda cada acción sensible en `audit_entries` (PostgreSQL), una tabla donde solo se agregan filas y que lleva una cadena de hashes.

## Cómo funciona
1. Un productor (identity-api) hace `XADD audit:events` sin esperar respuesta: si Redis falla, la acción sigue.
2. `consumer.py` lee con `XREADGROUP` (grupo `audit-workers`), valida el contrato (`event.py`, lista blanca de campos) y guarda el lote en una transacción; luego `XACK`. Si PostgreSQL cae, el lote queda pendiente y se reintenta.
3. `db.py` toma un advisory lock, lee el último hash y encadena cada fila nueva: `hash = sha256(prev_hash + "\n" + fila canónica)` (`chain.py`). Un `event_id` repetido se salta.
4. Un trigger rechaza `UPDATE`, `DELETE` y `TRUNCATE` (`migrations/001_init.sql`). Si alguien lo desactiva y edita, `verify` encuentra la fila.

## Comandos (dentro del contenedor)
```bash
python -m audit_log.verify                 # 0 = cadena entera, 1 = rota (dice en qué seq)
python -m audit_log.list --room <id>       # una fila JSON por línea; también --actor y --limit
```

## Pruebas locales
```bash
cd services/audit-log
python3.11 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/pytest --cov=src --cov-fail-under=85
```

Las pruebas de `tests/test_db_integration.py` (trigger y cadena sobre PostgreSQL real) se saltan si no hay base. Para correrlas:
```bash
docker run -d --rm --name audit-log-test-pg -e POSTGRES_PASSWORD=test -p 55432:5432 postgres:16-alpine
AUDIT_LOG_TEST_DATABASE_URL=postgresql://postgres:test@localhost:55432/postgres .venv/bin/pytest -v
docker stop audit-log-test-pg
```
Cada prueba usa un esquema temporal que se borra al terminar.
