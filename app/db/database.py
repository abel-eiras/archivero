"""Gestión de base de datos SQLite local, migraciones con copia de seguridad y consultas."""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import shutil
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from app.config import DEFAULT_BACKUP_DIR, DEFAULT_DB_PATH, DEFAULT_SETTINGS

logger = logging.getLogger(__name__)

MIGRATIONS: List[Tuple[int, str, str]] = [
    (
        1,
        "Esquema inicial completo: facturas, proveedores, correo, conciliación y reglas",
        """
        CREATE TABLE IF NOT EXISTS schema_versions (
            version INTEGER PRIMARY KEY,
            description TEXT NOT NULL,
            applied_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS configuracion (
            clave TEXT PRIMARY KEY,
            valor_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS proveedores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT NOT NULL,
            nif TEXT DEFAULT '',
            categoria_habitual TEXT DEFAULT 'compras_mercancia',
            tipo_documento_habitual TEXT DEFAULT 'factura_mercancia',
            reglas_json TEXT DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_proveedores_nif ON proveedores(nif);
        CREATE INDEX IF NOT EXISTS idx_proveedores_nombre ON proveedores(nombre);

        CREATE TABLE IF NOT EXISTS cuentas_correo (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE,
            tipo_conexion TEXT NOT NULL DEFAULT 'imap',
            servidor_imap TEXT DEFAULT '',
            puerto_imap INTEGER DEFAULT 993,
            usar_ssl INTEGER DEFAULT 1,
            remitentes_permitidos_json TEXT DEFAULT '[]',
            intervalo_minutos INTEGER DEFAULT 15,
            activa INTEGER DEFAULT 1,
            ultima_sincronizacion TEXT,
            ultimo_error TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS mensajes_procesados (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cuenta_id INTEGER NOT NULL,
            message_id TEXT NOT NULL,
            uid_imap TEXT DEFAULT '',
            remitente TEXT DEFAULT '',
            asunto TEXT DEFAULT '',
            fecha_mensaje TEXT,
            estado TEXT NOT NULL DEFAULT 'procesado',
            detalle TEXT DEFAULT '',
            procesado_en TEXT NOT NULL,
            FOREIGN KEY (cuenta_id) REFERENCES cuentas_correo(id) ON DELETE CASCADE,
            UNIQUE(cuenta_id, message_id)
        );
        CREATE INDEX IF NOT EXISTS idx_mensajes_message_id ON mensajes_procesados(message_id);

        CREATE TABLE IF NOT EXISTS facturas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            proveedor_id INTEGER,
            proveedor_nombre TEXT NOT NULL DEFAULT '',
            nif TEXT NOT NULL DEFAULT '',
            numero_factura TEXT NOT NULL DEFAULT '',
            fecha_emision TEXT,
            fecha_vencimiento TEXT,
            base_imponible REAL NOT NULL DEFAULT 0.0,
            iva REAL NOT NULL DEFAULT 0.0,
            total REAL NOT NULL DEFAULT 0.0,
            moneda TEXT NOT NULL DEFAULT 'EUR',
            tipo_documento TEXT NOT NULL DEFAULT 'factura_mercancia',
            categoria TEXT NOT NULL DEFAULT 'compras_mercancia',
            estado_revision TEXT NOT NULL DEFAULT 'pendiente_revision',
            estado_pago TEXT NOT NULL DEFAULT 'pendiente',
            nivel_confianza REAL NOT NULL DEFAULT 0.0,
            hash_pdf TEXT NOT NULL UNIQUE,
            ruta_archivo TEXT DEFAULT '',
            drive_file_id TEXT DEFAULT '',
            mensaje_origen_id TEXT DEFAULT '',
            datos_extra_json TEXT NOT NULL DEFAULT '{}',
            errores_validacion_json TEXT NOT NULL DEFAULT '[]',
            notas TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (proveedor_id) REFERENCES proveedores(id) ON DELETE SET NULL
        );
        CREATE INDEX IF NOT EXISTS idx_facturas_hash ON facturas(hash_pdf);
        CREATE INDEX IF NOT EXISTS idx_facturas_estado_rev ON facturas(estado_revision);
        CREATE INDEX IF NOT EXISTS idx_facturas_estado_pago ON facturas(estado_pago);
        CREATE INDEX IF NOT EXISTS idx_facturas_fecha_emision ON facturas(fecha_emision);
        CREATE INDEX IF NOT EXISTS idx_facturas_fecha_vencimiento ON facturas(fecha_vencimiento);
        CREATE INDEX IF NOT EXISTS idx_facturas_categoria ON facturas(categoria);

        CREATE TABLE IF NOT EXISTS movimientos_bancarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            banco TEXT NOT NULL DEFAULT 'Banco',
            cuenta_iban TEXT DEFAULT '',
            fecha TEXT NOT NULL,
            fecha_valor TEXT,
            importe REAL NOT NULL,
            moneda TEXT NOT NULL DEFAULT 'EUR',
            concepto TEXT NOT NULL DEFAULT '',
            referencia TEXT DEFAULT '',
            hash_movimiento TEXT NOT NULL UNIQUE,
            estado_conciliacion TEXT NOT NULL DEFAULT 'pendiente',
            diferencia_pendiente REAL NOT NULL DEFAULT 0.0,
            notas TEXT DEFAULT '',
            origen_archivo TEXT DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_movimientos_hash ON movimientos_bancarios(hash_movimiento);
        CREATE INDEX IF NOT EXISTS idx_movimientos_fecha ON movimientos_bancarios(fecha);
        CREATE INDEX IF NOT EXISTS idx_movimientos_estado ON movimientos_bancarios(estado_conciliacion);

        CREATE TABLE IF NOT EXISTS vinculos_conciliacion (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            movimiento_id INTEGER NOT NULL,
            factura_id INTEGER NOT NULL,
            importe_asignado REAL NOT NULL,
            tipo_vinculo TEXT NOT NULL DEFAULT 'propuesto',
            confianza REAL NOT NULL DEFAULT 1.0,
            confirmado INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY (movimiento_id) REFERENCES movimientos_bancarios(id) ON DELETE CASCADE,
            FOREIGN KEY (factura_id) REFERENCES facturas(id) ON DELETE CASCADE,
            UNIQUE(movimiento_id, factura_id)
        );
        CREATE INDEX IF NOT EXISTS idx_vinculos_movimiento ON vinculos_conciliacion(movimiento_id);
        CREATE INDEX IF NOT EXISTS idx_vinculos_factura ON vinculos_conciliacion(factura_id);

        CREATE TABLE IF NOT EXISTS reglas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tipo_regla TEXT NOT NULL,
            patron TEXT NOT NULL,
            proveedor_id INTEGER,
            proveedor_nombre TEXT DEFAULT '',
            categoria TEXT DEFAULT '',
            tipo_documento TEXT DEFAULT '',
            prioridad INTEGER NOT NULL DEFAULT 10,
            aprendida INTEGER NOT NULL DEFAULT 0,
            activa INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            FOREIGN KEY (proveedor_id) REFERENCES proveedores(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS mapeos_csv (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            banco TEXT NOT NULL UNIQUE,
            config_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """,
    ),
    (
        2,
        "Facturas sustituidas: una factura nueva con el mismo proveedor y número reemplaza a la anterior",
        """
        ALTER TABLE facturas ADD COLUMN sustituida_por INTEGER REFERENCES facturas(id) ON DELETE SET NULL;
        ALTER TABLE facturas ADD COLUMN sustituye_a INTEGER REFERENCES facturas(id) ON DELETE SET NULL;
        CREATE INDEX IF NOT EXISTS idx_facturas_sustituida_por ON facturas(sustituida_por);
        """,
    ),
]


def sender_matches_pattern(pattern: Optional[str], sender_email: Optional[str]) -> bool:
    """
    Coincidencia EXACTA de remitente (sin subcadenas):
    - Con '@' (p. ej. 'facturas@cofares.es'): la dirección completa, sin distinguir mayúsculas.
    - Sin '@' o con '@' inicial ('cofares.es' / '@cofares.es'): el dominio del remitente
      es ese dominio o un subdominio suyo ('pedidos.cofares.es'); 'cofares.es.evil.com' o
      'notcofares.es' no coinciden.
    """
    pat = (pattern or "").strip().lower()
    sender = (sender_email or "").strip().lower()
    if not pat or not sender or "@" not in sender:
        return False
    if pat.startswith("@"):
        pat = pat[1:]
    elif "@" in pat:
        return sender == pat
    pat = pat.strip(".")
    if not pat:
        return False
    domain = sender.rsplit("@", 1)[1].strip(".")
    return domain == pat or domain.endswith("." + pat)


def normalize_invoice_number(numero: Optional[str]) -> str:
    """'FRA 2026-001' y 'fra2026/001' se consideran el mismo número."""
    return re.sub(r"[\s\-_./\\]+", "", (numero or "")).upper()


def normalize_supplier_name(nombre: Optional[str]) -> str:
    cleaned = re.sub(r"[^\w]+", " ", (nombre or "").lower(), flags=re.UNICODE)
    return re.sub(r"\s+", " ", cleaned).strip()


# Campos de factura que el usuario puede vaciar explícitamente (None) al editar
NULLABLE_INVOICE_FIELDS = frozenset({"fecha_emision", "fecha_vencimiento", "notas", "nif", "numero_factura"})


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


class Database:
    """Gestor de base de datos SQLite local con migraciones seguras y backups automáticos."""

    def __init__(
        self,
        db_path: Optional[Path] = None,
        backup_dir: Optional[Path] = None,
    ) -> None:
        self.db_path = Path(db_path or DEFAULT_DB_PATH)
        self.backup_dir = Path(backup_dir or DEFAULT_BACKUP_DIR)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self.initialize_and_migrate()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA journal_mode = WAL;")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_schema_version(self) -> int:
        if not self.db_path.exists():
            return 0
        with self.connect() as conn:
            cur = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_versions';"
            )
            if not cur.fetchone():
                return 0
            row = conn.execute("SELECT MAX(version) AS v FROM schema_versions;").fetchone()
            return int(row["v"] or 0)

    def create_backup(self, reason: str = "manual") -> Optional[Path]:
        """Crea una copia de seguridad del archivo SQLite antes de migrar o bajo demanda."""
        if not self.db_path.exists() or self.db_path.stat().st_size == 0:
            return None
        timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        backup_file = self.backup_dir / f"facturas_backup_{reason}_{timestamp}.sqlite"
        with sqlite3.connect(str(self.db_path)) as src_conn:
            with sqlite3.connect(str(backup_file)) as dst_conn:
                src_conn.backup(dst_conn)
        logger.info("Copia de seguridad de base de datos creada en %s", backup_file)
        return backup_file

    def initialize_and_migrate(self) -> None:
        """Aplica migraciones pendientes realizando copia de seguridad previa si la base ya existe."""
        current_version = self.get_schema_version()
        pending = [m for m in MIGRATIONS if m[0] > current_version]

        if pending and current_version > 0:
            self.create_backup(reason=f"pre_migration_v{current_version}_to_v{pending[-1][0]}")

        with self.connect() as conn:
            for version, description, sql_script in pending:
                conn.executescript(sql_script)
                conn.execute(
                    "INSERT INTO schema_versions (version, description, applied_at) VALUES (?, ?, ?)",
                    (version, description, _now_iso()),
                )
                logger.info("Migración v%d aplicada: %s", version, description)

            # Inicializar valores de configuración por defecto sin sobrescribir los existentes
            now = _now_iso()
            for key, default_val in DEFAULT_SETTINGS.items():
                conn.execute(
                    """
                    INSERT OR IGNORE INTO configuracion (clave, valor_json, updated_at)
                    VALUES (?, ?, ?)
                    """,
                    (key, json.dumps(default_val, ensure_ascii=False), now),
                )

    # =====================================================================
    # Configuración
    # =====================================================================

    def get_all_settings(self) -> Dict[str, Any]:
        settings = dict(DEFAULT_SETTINGS)
        with self.connect() as conn:
            rows = conn.execute("SELECT clave, valor_json FROM configuracion").fetchall()
            for r in rows:
                try:
                    settings[r["clave"]] = json.loads(r["valor_json"])
                except Exception:
                    settings[r["clave"]] = r["valor_json"]
        return settings

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT valor_json FROM configuracion WHERE clave = ?", (key,)
            ).fetchone()
            if not row:
                return DEFAULT_SETTINGS.get(key, default)
            try:
                return json.loads(row["valor_json"])
            except Exception:
                return row["valor_json"]

    def set_settings(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        now = _now_iso()
        with self.connect() as conn:
            for key, value in updates.items():
                conn.execute(
                    """
                    INSERT INTO configuracion (clave, valor_json, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(clave) DO UPDATE SET
                        valor_json = excluded.valor_json,
                        updated_at = excluded.updated_at
                    """,
                    (key, json.dumps(value, ensure_ascii=False), now),
                )
        return self.get_all_settings()

    # =====================================================================
    # Proveedores y Reglas
    # =====================================================================

    def upsert_supplier(
        self,
        nombre: str,
        nif: str = "",
        categoria_habitual: str = "compras_mercancia",
        tipo_documento_habitual: str = "factura_mercancia",
    ) -> int:
        nombre_clean = nombre.strip()
        nif_clean = nif.strip().upper()
        if not nombre_clean:
            nombre_clean = "Proveedor Desconocido"
        now = _now_iso()

        with self.connect() as conn:
            existing = None
            if nif_clean:
                existing = conn.execute(
                    "SELECT id FROM proveedores WHERE UPPER(nif) = ? LIMIT 1",
                    (nif_clean,),
                ).fetchone()
            if not existing:
                existing = conn.execute(
                    "SELECT id FROM proveedores WHERE LOWER(nombre) = LOWER(?) LIMIT 1",
                    (nombre_clean,),
                ).fetchone()

            if existing:
                supplier_id = int(existing["id"])
                conn.execute(
                    """
                    UPDATE proveedores
                    SET nombre = ?,
                        nif = CASE WHEN ? != '' THEN ? ELSE nif END,
                        categoria_habitual = COALESCE(NULLIF(?, ''), categoria_habitual),
                        tipo_documento_habitual = COALESCE(NULLIF(?, ''), tipo_documento_habitual),
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (nombre_clean, nif_clean, nif_clean, categoria_habitual, tipo_documento_habitual, now, supplier_id),
                )
                return supplier_id

            cur = conn.execute(
                """
                INSERT INTO proveedores (nombre, nif, categoria_habitual, tipo_documento_habitual, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (nombre_clean, nif_clean, categoria_habitual, tipo_documento_habitual, now, now),
            )
            return int(cur.lastrowid)

    def list_suppliers(self) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT p.*,
                       COUNT(f.id) AS total_facturas,
                       COALESCE(SUM(f.total), 0.0) AS gasto_acumulado
                FROM proveedores p
                LEFT JOIN facturas f ON f.proveedor_id = p.id
                GROUP BY p.id
                ORDER BY p.nombre ASC
                """
            ).fetchall()
            return [dict(r) for r in rows]

    def create_rule(
        self,
        tipo_regla: str,
        patron: str,
        proveedor_id: Optional[int] = None,
        proveedor_nombre: str = "",
        categoria: str = "",
        tipo_documento: str = "",
        prioridad: int = 10,
        aprendida: bool = False,
        activa: bool = True,
    ) -> int:
        now = _now_iso()
        with self.connect() as conn:
            # Si ya existe una regla con el mismo tipo y patrón, actualizarla
            existing = conn.execute(
                "SELECT id FROM reglas WHERE tipo_regla = ? AND LOWER(patron) = LOWER(?)",
                (tipo_regla, patron.strip()),
            ).fetchone()
            if existing:
                rule_id = int(existing["id"])
                conn.execute(
                    """
                    UPDATE reglas
                    SET proveedor_id = ?, proveedor_nombre = ?, categoria = ?, tipo_documento = ?,
                        prioridad = ?, aprendida = ?, activa = ?
                    WHERE id = ?
                    """,
                    (
                        proveedor_id,
                        proveedor_nombre,
                        categoria,
                        tipo_documento,
                        prioridad,
                        1 if aprendida else 0,
                        1 if activa else 0,
                        rule_id,
                    ),
                )
                return rule_id

            cur = conn.execute(
                """
                INSERT INTO reglas
                (tipo_regla, patron, proveedor_id, proveedor_nombre, categoria, tipo_documento, prioridad, aprendida, activa, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tipo_regla,
                    patron.strip(),
                    proveedor_id,
                    proveedor_nombre.strip(),
                    categoria.strip(),
                    tipo_documento.strip(),
                    prioridad,
                    1 if aprendida else 0,
                    1 if activa else 0,
                    now,
                ),
            )
            return int(cur.lastrowid)

    def list_rules(self, active_only: bool = False) -> List[Dict[str, Any]]:
        query = "SELECT * FROM reglas"
        if active_only:
            query += " WHERE activa = 1"
        query += " ORDER BY prioridad DESC, id DESC"
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(query).fetchall()]

    def delete_rule(self, rule_id: int) -> bool:
        with self.connect() as conn:
            cur = conn.execute("DELETE FROM reglas WHERE id = ?", (rule_id,))
            return cur.rowcount > 0

    def match_rules_for_invoice(
        self,
        sender_email: str = "",
        supplier_name: str = "",
        nif: str = "",
    ) -> Dict[str, Any]:
        """Devuelve overrides de proveedor, categoría o tipo de documento según las reglas activas."""
        overrides: Dict[str, Any] = {}
        rules = self.list_rules(active_only=True)
        sender_low = (sender_email or "").lower()
        supplier_low = (supplier_name or "").lower()
        nif_up = (nif or "").upper()

        for r in rules:
            pat = (r["patron"] or "").lower()
            if not pat:
                continue
            matched = False
            if r["tipo_regla"] in ("remitente_proveedor", "remitente_categoria") and sender_matches_pattern(
                pat, sender_low
            ):
                matched = True
            elif r["tipo_regla"] == "nif_categoria" and (pat.upper() == nif_up or pat in supplier_low):
                matched = True

            if matched:
                if r.get("proveedor_nombre") and not overrides.get("proveedor"):
                    overrides["proveedor"] = r["proveedor_nombre"]
                    overrides["forzar_proveedor"] = True
                if r.get("categoria") and not overrides.get("categoria"):
                    overrides["categoria"] = r["categoria"]
                if r.get("tipo_documento") and not overrides.get("tipo_documento"):
                    overrides["tipo_documento"] = r["tipo_documento"]

        return overrides

    # =====================================================================
    # Facturas
    # =====================================================================

    def get_invoice_by_hash(self, hash_pdf: str) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM facturas WHERE hash_pdf = ? LIMIT 1", (hash_pdf,)
            ).fetchone()
            return self._deserialize_invoice_row(row) if row else None

    def get_invoice_by_id(self, invoice_id: int) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT f.*,
                       COALESCE((
                           SELECT SUM(vc.importe_asignado)
                           FROM vinculos_conciliacion vc
                           WHERE vc.factura_id = f.id AND vc.confirmado = 1
                       ), 0.0) AS importe_conciliado
                FROM facturas f
                WHERE f.id = ? LIMIT 1
                """,
                (invoice_id,),
            ).fetchone()
            return self._deserialize_invoice_row(row) if row else None

    def insert_invoice(self, data: Dict[str, Any]) -> int:
        now = _now_iso()
        proveedor_nombre = (data.get("proveedor_nombre") or data.get("proveedor") or "").strip()
        nif = (data.get("nif") or "").strip().upper()
        categoria = data.get("categoria") or "compras_mercancia"
        tipo_documento = data.get("tipo_documento") or "factura_mercancia"

        proveedor_id = data.get("proveedor_id")
        if not proveedor_id and proveedor_nombre:
            proveedor_id = self.upsert_supplier(
                nombre=proveedor_nombre,
                nif=nif,
                categoria_habitual=categoria,
                tipo_documento_habitual=tipo_documento,
            )

        datos_extra_json = json.dumps(data.get("datos_extra") or {}, ensure_ascii=False)
        errores_json = json.dumps(data.get("errores_validacion") or [], ensure_ascii=False)

        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO facturas (
                    proveedor_id, proveedor_nombre, nif, numero_factura,
                    fecha_emision, fecha_vencimiento, base_imponible, iva, total,
                    moneda, tipo_documento, categoria, estado_revision, estado_pago,
                    nivel_confianza, hash_pdf, ruta_archivo, drive_file_id,
                    mensaje_origen_id, datos_extra_json, errores_validacion_json,
                    notas, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    proveedor_id,
                    proveedor_nombre,
                    nif,
                    (data.get("numero_factura") or "").strip(),
                    data.get("fecha_emision"),
                    data.get("fecha_vencimiento"),
                    round(float(data.get("base_imponible") or 0.0), 2),
                    round(float(data.get("iva") or 0.0), 2),
                    round(float(data.get("total") or 0.0), 2),
                    data.get("moneda") or "EUR",
                    tipo_documento,
                    categoria,
                    data.get("estado_revision") or "pendiente_revision",
                    data.get("estado_pago") or "pendiente",
                    round(float(data.get("nivel_confianza") or 0.0), 2),
                    data["hash_pdf"],
                    data.get("ruta_archivo") or "",
                    data.get("drive_file_id") or "",
                    data.get("mensaje_origen_id") or "",
                    datos_extra_json,
                    errores_json,
                    data.get("notas") or "",
                    now,
                    now,
                ),
            )
            return int(cur.lastrowid)

    def update_invoice(self, invoice_id: int, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        current = self.get_invoice_by_id(invoice_id)
        if not current:
            return None

        merged = dict(current)
        for k, v in updates.items():
            # Los campos anulables pueden vaciarse explícitamente con None;
            # en el resto, None significa "no cambiar".
            if v is not None or k in NULLABLE_INVOICE_FIELDS:
                merged[k] = v

        proveedor_id = merged.get("proveedor_id")
        if merged.get("proveedor_nombre"):
            proveedor_id = self.upsert_supplier(
                nombre=merged["proveedor_nombre"],
                nif=merged.get("nif") or "",
                categoria_habitual=merged.get("categoria") or "compras_mercancia",
                tipo_documento_habitual=merged.get("tipo_documento") or "factura_mercancia",
            )

        now = _now_iso()
        datos_extra_json = json.dumps(merged.get("datos_extra") or {}, ensure_ascii=False)
        errores_json = json.dumps(merged.get("errores_validacion") or [], ensure_ascii=False)

        with self.connect() as conn:
            conn.execute(
                """
                UPDATE facturas
                SET proveedor_id = ?,
                    proveedor_nombre = ?,
                    nif = ?,
                    numero_factura = ?,
                    fecha_emision = ?,
                    fecha_vencimiento = ?,
                    base_imponible = ?,
                    iva = ?,
                    total = ?,
                    moneda = ?,
                    tipo_documento = ?,
                    categoria = ?,
                    estado_revision = ?,
                    estado_pago = ?,
                    nivel_confianza = ?,
                    ruta_archivo = ?,
                    drive_file_id = ?,
                    datos_extra_json = ?,
                    errores_validacion_json = ?,
                    notas = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    proveedor_id,
                    merged.get("proveedor_nombre") or "",
                    (merged.get("nif") or "").upper(),
                    merged.get("numero_factura") or "",
                    merged.get("fecha_emision"),
                    merged.get("fecha_vencimiento"),
                    round(float(merged.get("base_imponible") or 0.0), 2),
                    round(float(merged.get("iva") or 0.0), 2),
                    round(float(merged.get("total") or 0.0), 2),
                    merged.get("moneda") or "EUR",
                    merged.get("tipo_documento") or "factura_mercancia",
                    merged.get("categoria") or "compras_mercancia",
                    merged.get("estado_revision") or "pendiente_revision",
                    merged.get("estado_pago") or "pendiente",
                    round(float(merged.get("nivel_confianza") or 0.0), 2),
                    merged.get("ruta_archivo") or "",
                    merged.get("drive_file_id") or "",
                    datos_extra_json,
                    errores_json,
                    merged.get("notas") or "",
                    now,
                    invoice_id,
                ),
            )
        return self.get_invoice_by_id(invoice_id)

    def find_replaceable_invoice(
        self, numero_factura: str, nif: str = "", proveedor_nombre: str = "", exclude_id: Optional[int] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Busca una factura ACTIVA (no sustituida) del mismo proveedor con el mismo número.
        Proveedor: por NIF si ambas lo tienen; si no, por nombre normalizado.
        Número: sin distinguir mayúsculas, espacios, guiones, puntos ni barras.
        """
        num_key = normalize_invoice_number(numero_factura)
        if not num_key:
            return None
        nif_key = (nif or "").strip().upper()
        name_key = normalize_supplier_name(proveedor_nombre)
        if not nif_key and not name_key:
            return None
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id, nif, proveedor_nombre, numero_factura FROM facturas "
                "WHERE sustituida_por IS NULL ORDER BY id DESC"
            ).fetchall()
        for r in rows:
            if exclude_id is not None and int(r["id"]) == exclude_id:
                continue
            if normalize_invoice_number(r["numero_factura"]) != num_key:
                continue
            other_nif = (r["nif"] or "").strip().upper()
            if nif_key and other_nif:
                same_supplier = nif_key == other_nif
            else:
                same_supplier = bool(name_key) and normalize_supplier_name(r["proveedor_nombre"]) == name_key
            if same_supplier:
                return self.get_invoice_by_id(int(r["id"]))
        return None

    def mark_invoice_replaced(self, old_id: int, new_id: int) -> None:
        """Marca la factura antigua como sustituida (se conserva en el historial con su archivo)."""
        now = _now_iso()
        with self.connect() as conn:
            conn.execute(
                "UPDATE facturas SET sustituida_por = ?, updated_at = ? WHERE id = ?", (new_id, now, old_id)
            )
            conn.execute(
                "UPDATE facturas SET sustituye_a = ?, updated_at = ? WHERE id = ?", (old_id, now, new_id)
            )

    def invoice_has_reconciliation_links(self, invoice_id: int) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM vinculos_conciliacion WHERE factura_id = ? LIMIT 1", (invoice_id,)
            ).fetchone()
            return row is not None

    def delete_invoice(self, invoice_id: int) -> bool:
        with self.connect() as conn:
            mov_rows = conn.execute(
                "SELECT DISTINCT movimiento_id FROM vinculos_conciliacion WHERE factura_id = ?",
                (invoice_id,),
            ).fetchall()
            affected_movs = [int(r["movimiento_id"]) for r in mov_rows]
            # Borrado explícito de vínculos (por si la conexión no aplica ON DELETE CASCADE)
            conn.execute("DELETE FROM vinculos_conciliacion WHERE factura_id = ?", (invoice_id,))
            cur = conn.execute("DELETE FROM facturas WHERE id = ?", (invoice_id,))
            # Los movimientos que se quedan sin ningún vínculo vuelven a 'pendiente'
            for mov_id in affected_movs:
                remaining = conn.execute(
                    "SELECT 1 FROM vinculos_conciliacion WHERE movimiento_id = ? LIMIT 1",
                    (mov_id,),
                ).fetchone()
                if remaining is None:
                    conn.execute(
                        """
                        UPDATE movimientos_bancarios
                        SET estado_conciliacion = 'pendiente', diferencia_pendiente = 0.0
                        WHERE id = ?
                        """,
                        (mov_id,),
                    )
            return cur.rowcount > 0

    def list_invoices(
        self,
        estado_revision: Optional[str] = None,
        estado_pago: Optional[str] = None,
        categoria: Optional[str] = None,
        proveedor: Optional[str] = None,
        mes: Optional[str] = None,  # YYYY-MM
        fecha_desde: Optional[str] = None,
        fecha_hasta: Optional[str] = None,
        busqueda: Optional[str] = None,
        limit: int = 500,
        incluir_sustituidas: bool = False,
    ) -> List[Dict[str, Any]]:
        clauses: List[str] = ["1=1"]
        params: List[Any] = []

        # Las facturas sustituidas por otra (mismo proveedor y número) se ocultan por defecto
        if not incluir_sustituidas:
            clauses.append("f.sustituida_por IS NULL")

        if estado_revision:
            clauses.append("f.estado_revision = ?")
            params.append(estado_revision)
        if estado_pago:
            clauses.append("f.estado_pago = ?")
            params.append(estado_pago)
        if categoria:
            clauses.append("f.categoria = ?")
            params.append(categoria)
        if proveedor:
            clauses.append("(LOWER(f.proveedor_nombre) LIKE ? OR UPPER(f.nif) = ?)")
            params.extend([f"%{proveedor.lower()}%", proveedor.upper()])
        if mes:
            clauses.append("strftime('%Y-%m', f.fecha_emision) = ?")
            params.append(mes)
        if fecha_desde:
            clauses.append("f.fecha_emision >= ?")
            params.append(fecha_desde)
        if fecha_hasta:
            clauses.append("f.fecha_emision <= ?")
            params.append(fecha_hasta)
        if busqueda:
            q = f"%{busqueda.lower()}%"
            clauses.append(
                "(LOWER(f.proveedor_nombre) LIKE ? OR LOWER(f.numero_factura) LIKE ? "
                "OR LOWER(f.nif) LIKE ? OR LOWER(f.notas) LIKE ?)"
            )
            params.extend([q, q, q, q])

        params.append(limit)
        sql = f"""
            SELECT f.*,
                   COALESCE((
                       SELECT SUM(vc.importe_asignado)
                       FROM vinculos_conciliacion vc
                       WHERE vc.factura_id = f.id AND vc.confirmado = 1
                   ), 0.0) AS importe_conciliado
            FROM facturas f
            WHERE {' AND '.join(clauses)}
            ORDER BY COALESCE(f.fecha_emision, f.created_at) DESC, f.id DESC
            LIMIT ?
        """
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [self._deserialize_invoice_row(r) for r in rows]

    def _deserialize_invoice_row(self, row: sqlite3.Row) -> Dict[str, Any]:
        d = dict(row)
        try:
            d["datos_extra"] = json.loads(d.get("datos_extra_json") or "{}")
        except Exception:
            d["datos_extra"] = {}
        try:
            d["errores_validacion"] = json.loads(d.get("errores_validacion_json") or "[]")
        except Exception:
            d["errores_validacion"] = []

        ruta = d.get("ruta_archivo") or ""
        d["archivo_disponible"] = bool(ruta and Path(ruta).is_file())
        importe_conciliado = float(d.get("importe_conciliado") or 0.0)
        d["importe_conciliado"] = round(importe_conciliado, 2)
        d["importe_pendiente"] = round(float(d.get("total") or 0.0) - importe_conciliado, 2)
        return d

    # =====================================================================
    # Cuentas de correo y mensajes procesados
    # =====================================================================

    def upsert_mail_account(self, data: Dict[str, Any]) -> int:
        now = _now_iso()
        email = data["email"].strip().lower()
        remitentes_json = json.dumps(data.get("remitentes_permitidos") or [], ensure_ascii=False)

        with self.connect() as conn:
            existing = conn.execute(
                "SELECT id FROM cuentas_correo WHERE LOWER(email) = ?", (email,)
            ).fetchone()
            if existing:
                acc_id = int(existing["id"])
                conn.execute(
                    """
                    UPDATE cuentas_correo
                    SET nombre = ?, tipo_conexion = ?, servidor_imap = ?, puerto_imap = ?,
                        usar_ssl = ?, remitentes_permitidos_json = ?, intervalo_minutos = ?, activa = ?
                    WHERE id = ?
                    """,
                    (
                        data["nombre"].strip(),
                        data.get("tipo_conexion") or "imap",
                        data.get("servidor_imap") or "",
                        int(data.get("puerto_imap") or 993),
                        1 if data.get("usar_ssl", True) else 0,
                        remitentes_json,
                        int(data.get("intervalo_minutos") or 15),
                        1 if data.get("activa", True) else 0,
                        acc_id,
                    ),
                )
                return acc_id

            cur = conn.execute(
                """
                INSERT INTO cuentas_correo (
                    nombre, email, tipo_conexion, servidor_imap, puerto_imap,
                    usar_ssl, remitentes_permitidos_json, intervalo_minutos, activa, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    data["nombre"].strip(),
                    email,
                    data.get("tipo_conexion") or "imap",
                    data.get("servidor_imap") or "",
                    int(data.get("puerto_imap") or 993),
                    1 if data.get("usar_ssl", True) else 0,
                    remitentes_json,
                    int(data.get("intervalo_minutos") or 15),
                    1 if data.get("activa", True) else 0,
                    now,
                ),
            )
            return int(cur.lastrowid)

    def list_mail_accounts(self, active_only: bool = False) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM cuentas_correo"
        if active_only:
            sql += " WHERE activa = 1"
        sql += " ORDER BY id ASC"
        with self.connect() as conn:
            rows = conn.execute(sql).fetchall()
            result = []
            for r in rows:
                item = dict(r)
                try:
                    item["remitentes_permitidos"] = json.loads(
                        item.get("remitentes_permitidos_json") or "[]"
                    )
                except Exception:
                    item["remitentes_permitidos"] = []
                item["usar_ssl"] = bool(item.get("usar_ssl"))
                item["activa"] = bool(item.get("activa"))
                result.append(item)
            return result

    def delete_mail_account(self, account_id: int) -> bool:
        with self.connect() as conn:
            cur = conn.execute("DELETE FROM cuentas_correo WHERE id = ?", (account_id,))
            return cur.rowcount > 0

    def update_mail_account_sync_status(
        self, account_id: int, error: Optional[str] = None
    ) -> None:
        now = _now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE cuentas_correo
                SET ultima_sincronizacion = ?, ultimo_error = ?
                WHERE id = ?
                """,
                (now, error, account_id),
            )

    def is_message_processed(self, cuenta_id: int, message_id: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id FROM mensajes_procesados WHERE cuenta_id = ? AND message_id = ? LIMIT 1",
                (cuenta_id, message_id),
            ).fetchone()
            return row is not None

    def mark_message_processed(
        self,
        cuenta_id: int,
        message_id: str,
        uid_imap: str = "",
        remitente: str = "",
        asunto: str = "",
        fecha_mensaje: Optional[str] = None,
        estado: str = "procesado",
        detalle: str = "",
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO mensajes_procesados (
                    cuenta_id, message_id, uid_imap, remitente, asunto,
                    fecha_mensaje, estado, detalle, procesado_en
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cuenta_id,
                    message_id,
                    uid_imap,
                    remitente,
                    asunto,
                    fecha_mensaje,
                    estado,
                    detalle,
                    _now_iso(),
                ),
            )

    # =====================================================================
    # Movimientos bancarios y Conciliación
    # =====================================================================

    def insert_bank_transaction(self, tx: Dict[str, Any]) -> Tuple[Optional[int], bool]:
        """
        Inserta un movimiento bancario si su hash no existe.
        Devuelve (id_movimiento, es_nuevo).
        """
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT id FROM movimientos_bancarios WHERE hash_movimiento = ? LIMIT 1",
                (tx["hash_movimiento"],),
            ).fetchone()
            if existing:
                return int(existing["id"]), False

            cur = conn.execute(
                """
                INSERT INTO movimientos_bancarios (
                    banco, cuenta_iban, fecha, fecha_valor, importe, moneda,
                    concepto, referencia, hash_movimiento, estado_conciliacion,
                    diferencia_pendiente, notas, origen_archivo, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tx.get("banco") or "Banco",
                    tx.get("cuenta_iban") or "",
                    tx["fecha"],
                    tx.get("fecha_valor") or tx["fecha"],
                    round(float(tx["importe"]), 2),
                    tx.get("moneda") or "EUR",
                    tx.get("concepto") or "",
                    tx.get("referencia") or "",
                    tx["hash_movimiento"],
                    tx.get("estado_conciliacion") or "pendiente",
                    round(float(tx.get("diferencia_pendiente") or 0.0), 2),
                    tx.get("notas") or "",
                    tx.get("origen_archivo") or "",
                    _now_iso(),
                ),
            )
            return int(cur.lastrowid), True

    def list_bank_transactions(
        self,
        estado_conciliacion: Optional[str] = None,
        banco: Optional[str] = None,
        limit: int = 500,
    ) -> List[Dict[str, Any]]:
        clauses = ["1=1"]
        params: List[Any] = []
        if estado_conciliacion:
            clauses.append("m.estado_conciliacion = ?")
            params.append(estado_conciliacion)
        if banco:
            clauses.append("LOWER(m.banco) = LOWER(?)")
            params.append(banco)
        params.append(limit)

        sql = f"""
            SELECT m.*
            FROM movimientos_bancarios m
            WHERE {' AND '.join(clauses)}
            ORDER BY m.fecha DESC, m.id DESC
            LIMIT ?
        """
        with self.connect() as conn:
            txs = [dict(r) for r in conn.execute(sql, params).fetchall()]
            for tx in txs:
                links = conn.execute(
                    """
                    SELECT vc.*, f.numero_factura, f.proveedor_nombre, f.total AS factura_total,
                           f.fecha_emision, f.fecha_vencimiento, f.tipo_documento
                    FROM vinculos_conciliacion vc
                    JOIN facturas f ON f.id = vc.factura_id
                    WHERE vc.movimiento_id = ?
                    ORDER BY vc.id ASC
                    """,
                    (tx["id"],),
                ).fetchall()
                tx["vinculos"] = [dict(l) for l in links]
            return txs

    def save_csv_mapping(self, banco: str, config: Dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO mapeos_csv (banco, config_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(banco) DO UPDATE SET
                    config_json = excluded.config_json,
                    updated_at = excluded.updated_at
                """,
                (banco.strip(), json.dumps(config, ensure_ascii=False), _now_iso()),
            )

    def get_csv_mappings(self) -> Dict[str, Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT banco, config_json FROM mapeos_csv").fetchall()
            out: Dict[str, Dict[str, Any]] = {}
            for r in rows:
                try:
                    out[r["banco"]] = json.loads(r["config_json"])
                except Exception:
                    continue
            return out

    def recalculate_invoice_payment_status(self, invoice_id: int) -> str:
        """Actualiza el estado de pago ('pendiente', 'parcial', 'pagada') según los vínculos confirmados."""
        with self.connect() as conn:
            inv = conn.execute(
                "SELECT id, total, tipo_documento FROM facturas WHERE id = ?", (invoice_id,)
            ).fetchone()
            if not inv:
                return "pendiente"
            row = conn.execute(
                """
                SELECT COALESCE(SUM(importe_asignado), 0.0) AS pagado
                FROM vinculos_conciliacion
                WHERE factura_id = ? AND confirmado = 1
                """,
                (invoice_id,),
            ).fetchone()
            pagado = round(float(row["pagado"] or 0.0), 2)
            total = round(float(inv["total"] or 0.0), 2)

            if abs(pagado) < 0.01:
                nuevo_estado = "pendiente"
            elif abs(abs(pagado) - abs(total)) <= 0.05 or abs(pagado) >= abs(total):
                nuevo_estado = "pagada"
            else:
                nuevo_estado = "parcial"

            conn.execute(
                "UPDATE facturas SET estado_pago = ?, updated_at = ? WHERE id = ?",
                (nuevo_estado, _now_iso(), invoice_id),
            )
            return nuevo_estado

    # =====================================================================
    # Estadísticas y Métricas del Dashboard (Sección 9)
    # =====================================================================

    def get_dashboard_metrics(self) -> Dict[str, Any]:
        """Calcula las métricas del panel principal leyendo exclusivamente de SQLite."""
        today = dt.date.today().isoformat()
        in_15_days = (dt.date.today() + dt.timedelta(days=15)).isoformat()

        with self.connect() as conn:
            # Totales globales
            resumen = conn.execute(
                """
                SELECT
                    COUNT(*) AS total_facturas,
                    COALESCE(SUM(total), 0.0) AS gasto_total,
                    COALESCE(SUM(base_imponible), 0.0) AS base_total,
                    COALESCE(SUM(iva), 0.0) AS iva_total,
                    SUM(CASE WHEN estado_revision = 'pendiente_revision' THEN 1 ELSE 0 END) AS pendientes_revision,
                    SUM(CASE WHEN estado_pago != 'pagada' THEN 1 ELSE 0 END) AS facturas_impagadas,
                    COALESCE(SUM(CASE WHEN estado_pago != 'pagada' THEN total ELSE 0.0 END), 0.0) AS importe_pendiente_pago
                FROM facturas
                WHERE sustituida_por IS NULL
                """
            ).fetchone()

            # Gasto por mes
            por_mes = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT COALESCE(strftime('%Y-%m', fecha_emision), 'Sin fecha') AS mes,
                           COUNT(*) AS cantidad,
                           ROUND(SUM(base_imponible), 2) AS base_imponible,
                           ROUND(SUM(iva), 2) AS iva,
                           ROUND(SUM(total), 2) AS total
                    FROM facturas
                    WHERE sustituida_por IS NULL
                    GROUP BY mes
                    ORDER BY mes DESC
                    LIMIT 12
                    """
                ).fetchall()
            ]

            # Gasto por categoría
            por_categoria = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT categoria,
                           COUNT(*) AS cantidad,
                           ROUND(SUM(total), 2) AS total
                    FROM facturas
                    WHERE sustituida_por IS NULL
                    GROUP BY categoria
                    ORDER BY total DESC
                    """
                ).fetchall()
            ]

            # Gasto por proveedor (Top 10)
            por_proveedor = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT COALESCE(NULLIF(proveedor_nombre, ''), 'Sin proveedor') AS proveedor,
                           nif,
                           COUNT(*) AS cantidad,
                           ROUND(SUM(total), 2) AS total
                    FROM facturas
                    WHERE sustituida_por IS NULL
                    GROUP BY proveedor_nombre
                    ORDER BY total DESC
                    LIMIT 10
                    """
                ).fetchall()
            ]

            # Vencimientos próximos (próximos 15 días o vencidas aún no pagadas)
            vencimientos_proximos = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT id, proveedor_nombre, numero_factura, fecha_emision,
                           fecha_vencimiento, total, estado_pago, categoria
                    FROM facturas
                    WHERE sustituida_por IS NULL AND estado_pago != 'pagada'
                      AND fecha_vencimiento IS NOT NULL
                      AND fecha_vencimiento != ''
                      AND fecha_vencimiento <= ?
                    ORDER BY fecha_vencimiento ASC
                    LIMIT 15
                    """,
                    (in_15_days,),
                ).fetchall()
            ]

            # Facturas pendientes de pago
            pendientes_pago = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT id, proveedor_nombre, numero_factura, fecha_emision,
                           fecha_vencimiento, total, estado_pago, categoria
                    FROM facturas
                    WHERE sustituida_por IS NULL AND estado_pago != 'pagada'
                    ORDER BY COALESCE(fecha_vencimiento, fecha_emision) ASC
                    LIMIT 20
                    """
                ).fetchall()
            ]

            # Resumen de conciliación bancaria
            conciliacion_stats = conn.execute(
                """
                SELECT
                    COUNT(*) AS total_movimientos,
                    SUM(CASE WHEN estado_conciliacion = 'conciliado' THEN 1 ELSE 0 END) AS conciliados,
                    SUM(CASE WHEN estado_conciliacion = 'propuesto' THEN 1 ELSE 0 END) AS propuestos,
                    SUM(CASE WHEN estado_conciliacion = 'pendiente' THEN 1 ELSE 0 END) AS pendientes
                FROM movimientos_bancarios
                """
            ).fetchone()

        return {
            "resumen": {
                "total_facturas": int(resumen["total_facturas"] or 0),
                "gasto_total": round(float(resumen["gasto_total"] or 0.0), 2),
                "base_total": round(float(resumen["base_total"] or 0.0), 2),
                "iva_total": round(float(resumen["iva_total"] or 0.0), 2),
                "pendientes_revision": int(resumen["pendientes_revision"] or 0),
                "facturas_impagadas": int(resumen["facturas_impagadas"] or 0),
                "importe_pendiente_pago": round(float(resumen["importe_pendiente_pago"] or 0.0), 2),
                "fecha_referencia": today,
            },
            "gasto_por_mes": list(reversed(por_mes)),
            "gasto_por_categoria": por_categoria,
            "gasto_por_proveedor": por_proveedor,
            "vencimientos_proximos": vencimientos_proximos,
            "pendientes_pago": pendientes_pago,
            "conciliacion": {
                "total_movimientos": int(conciliacion_stats["total_movimientos"] or 0),
                "conciliados": int(conciliacion_stats["conciliados"] or 0),
                "propuestos": int(conciliacion_stats["propuestos"] or 0),
                "pendientes": int(conciliacion_stats["pendientes"] or 0),
            },
        }
