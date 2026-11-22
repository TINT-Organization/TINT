-- 001_init: bitácora de acciones sensibles. Contrato: services/audit-log/AGENTS.md.
-- Sin correo, IP, nick ni tokens (Ley 1581 de 2012).

CREATE TABLE IF NOT EXISTS audit_entries (
    seq BIGSERIAL PRIMARY KEY,           -- orden de la cadena (puede tener huecos)
    event_id UUID NOT NULL UNIQUE,       -- idempotencia: un reintento no duplica la fila
    action TEXT NOT NULL,                -- p. ej. room.delete, ban.create, access.denied
    outcome TEXT NOT NULL CHECK (outcome IN ('ok', 'denied')),
    actor_id TEXT NULL,                  -- sub del token; NULL si no hubo token
    actor_role TEXT NULL,
    room_id TEXT NULL,
    source TEXT NOT NULL,                -- servicio productor
    occurred_at TIMESTAMPTZ NOT NULL,    -- cuándo pasó (lo dice el productor)
    recorded_at TIMESTAMPTZ NOT NULL,    -- cuándo se guardó (entra al hash)
    prev_hash CHAR(64) NOT NULL,         -- hash de la fila anterior; ceros en la primera
    hash CHAR(64) NOT NULL               -- sha256(prev_hash + "\n" + fila canónica)
);

CREATE INDEX IF NOT EXISTS audit_entries_room_idx ON audit_entries (room_id, seq);
CREATE INDEX IF NOT EXISTS audit_entries_actor_idx ON audit_entries (actor_id, seq);

-- Solo se agregan filas: UPDATE, DELETE y TRUNCATE fallan, también para el dueño de la tabla.
-- Quien pueda desactivar el trigger (superusuario) aún podría editar, pero la cadena de
-- hashes lo deja a la vista (`python -m audit_log.verify`).
CREATE OR REPLACE FUNCTION audit_entries_reject_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit_entries solo admite INSERT (% rechazado)', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END;
$$;

DROP TRIGGER IF EXISTS audit_entries_no_update_delete ON audit_entries;
CREATE TRIGGER audit_entries_no_update_delete
    BEFORE UPDATE OR DELETE ON audit_entries
    FOR EACH ROW EXECUTE FUNCTION audit_entries_reject_change();

DROP TRIGGER IF EXISTS audit_entries_no_truncate ON audit_entries;
CREATE TRIGGER audit_entries_no_truncate
    BEFORE TRUNCATE ON audit_entries
    FOR EACH STATEMENT EXECUTE FUNCTION audit_entries_reject_change();
