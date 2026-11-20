"""Motor de conciliación bancaria: emparejamiento automático y manual muchos-a-muchos (Fase 6)."""

from __future__ import annotations

import datetime as dt
from itertools import combinations
import logging
import re
from typing import Any, Dict, List, Optional, Tuple, Union

from app.db.database import Database
from app.services.activity import fmt_eur
from app.models.schemas import CSVMappingConfig
from app.services.bank_parsers import (
    parse_camt053,
    parse_camt053_balances,
    parse_csv_balance,
    parse_csv_or_excel,
    parse_norma43,
    parse_norma43_balances,
)

logger = logging.getLogger(__name__)


class ReconciliationError(ValueError):
    """Datos de conciliación no válidos (importe excesivo, factura ya pagada...)."""


class ReconciliationNotFound(ReconciliationError):
    """El movimiento o la factura indicados no existen."""


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


class BankReconciliationService:
    """Gestiona la importación de extractos y el emparejamiento automático y manual de movimientos y facturas."""

    def __init__(self, db: Database, auto_confirm_threshold: float = 0.85) -> None:
        self.db = db
        self.auto_confirm_threshold = auto_confirm_threshold

    def import_statement(
        self,
        content: Union[bytes, str],
        filename: str,
        formato: str = "auto",  # "auto" | "norma43" | "camt053" | "csv"
        banco: str = "Banco",
        csv_mapping: Optional[Union[CSVMappingConfig, Dict[str, Any]]] = None,
        auto_reconcile: bool = True,
    ) -> Dict[str, Any]:
        """
        Importa un fichero de extracto bancario (Norma 43, CAMT.053 o CSV/Excel),
        evita duplicados por hash y lanza opcionalmente el conciliador automático.
        """
        fmt = formato.lower().strip()
        fname_low = filename.lower()

        if fmt == "auto":
            if fname_low.endswith((".n43", ".c43", ".aeb")):
                fmt = "norma43"
            elif fname_low.endswith(".xml"):
                fmt = "camt053"
            elif fname_low.endswith((".csv", ".xlsx")):
                fmt = "csv"
            else:
                sample = content[:200].decode("utf-8", errors="ignore") if isinstance(content, bytes) else content[:200]
                if sample.lstrip().startswith("<?xml") or "<Document" in sample or "<BkToCstmrStmt" in sample:
                    fmt = "camt053"
                elif re.match(r"^(11|22|23|33|88)\d{10,}", sample.lstrip()):
                    fmt = "norma43"
                else:
                    fmt = "csv"

        effective_mapping: Optional[Union[CSVMappingConfig, Dict[str, Any]]] = None
        if fmt == "norma43":
            parsed_txs = parse_norma43(content, banco_default=banco, origen_archivo=filename)
        elif fmt == "camt053":
            parsed_txs = parse_camt053(content, banco_default=banco, origen_archivo=filename)
        else:
            saved_mappings = self.db.get_csv_mappings()
            effective_mapping = csv_mapping or saved_mappings.get(banco)
            if csv_mapping:
                mapping_dict = csv_mapping.model_dump() if isinstance(csv_mapping, CSVMappingConfig) else dict(csv_mapping)
                self.db.save_csv_mapping(banco, mapping_dict)
            parsed_txs = parse_csv_or_excel(
                content,
                filename=filename,
                mapping=effective_mapping,
                banco_default=banco,
            )

        nuevos = 0
        duplicados = 0
        inserted_ids: List[int] = []

        for tx in parsed_txs:
            tx_id, is_new = self.db.insert_bank_transaction(tx)
            if is_new and tx_id is not None:
                nuevos += 1
                inserted_ids.append(tx_id)
            else:
                duplicados += 1

        reconciliation_summary = {}
        if auto_reconcile and nuevos > 0:
            reconciliation_summary = self.run_auto_reconciliation()

        saldos = self._store_statement_balances(content, filename, fmt, banco, effective_mapping)

        mensaje = (
            f"Extracto de {banco or 'banco'} importado ({filename}): {nuevos} movimientos nuevos, "
            f"{duplicados} repetidos."
        )
        if reconciliation_summary:
            mensaje += (
                f" Cuadrados solos: {reconciliation_summary.get('conciliados_automaticos', 0)};"
                f" propuestas para revisar: {reconciliation_summary.get('propuestas_revision', 0)}."
            )
        if saldos:
            mensaje += " Saldo leído: " + ", ".join(
                f"{s['cuenta']} {fmt_eur(s['saldo'])} a {s['fecha_saldo']}" for s in saldos
            ) + "."
        self.db.log_event(
            tipo="extracto_importado",
            mensaje=mensaje,
            detalle={
                "formato": fmt,
                "fichero": filename,
                "nuevos": nuevos,
                "duplicados": duplicados,
                "conciliacion": reconciliation_summary,
                "saldos": saldos,
            },
        )

        return {
            "formato_detectado": fmt,
            "total_leidos": len(parsed_txs),
            "nuevos_insertados": nuevos,
            "duplicados_omitidos": duplicados,
            "conciliacion_automatica": reconciliation_summary,
            "saldos_importados": saldos,
        }

    def _store_statement_balances(
        self,
        content: Union[bytes, str],
        filename: str,
        fmt: str,
        banco: str,
        mapping: Optional[Union[CSVMappingConfig, Dict[str, Any]]],
    ) -> List[Dict[str, Any]]:
        """Lee el saldo final del extracto (si lo trae) y lo guarda por cuenta y fecha."""
        try:
            if fmt == "norma43":
                saldos = parse_norma43_balances(content, banco_default=banco)
            elif fmt == "camt053":
                saldos = parse_camt053_balances(content, banco_default=banco)
            else:
                saldos = parse_csv_balance(content, filename=filename, mapping=mapping, banco_default=banco)
        except Exception as exc:
            logger.warning("No se pudo leer el saldo del extracto %s: %s", filename, exc)
            return []
        for s in saldos:
            self.db.upsert_bank_balance(
                cuenta=s["cuenta"],
                fecha_saldo=s["fecha_saldo"],
                saldo=s["saldo"],
                banco=s.get("banco") or banco,
                moneda=s.get("moneda") or "EUR",
                origen_fichero=filename,
            )
        return saldos

    def run_auto_reconciliation(self) -> Dict[str, int]:
        """
        Ejecuta el conciliador automático sobre los movimientos pendientes:
        - Compara importe, cercanía de fecha al vencimiento, proveedor/NIF/número de factura en el concepto
          y reglas aprendidas ('concepto_proveedor').
        - Soporta emparejamiento 1:1 y pagos agrupados 1:N (incluyendo abonos que restan).
        - Marca automáticamente los de alta confianza (>= umbral) y deja como propuesta los dudosos.
        """
        pending_txs = self.db.list_bank_transactions(estado_conciliacion="pendiente")
        # Facturas que ya tienen una propuesta sin confirmar en otro movimiento: no se vuelven a
        # proponer para evitar que una misma factura quede asignada (y confirmada) dos veces.
        with self.db.connect() as conn:
            proposed_rows = conn.execute(
                "SELECT DISTINCT factura_id FROM vinculos_conciliacion WHERE confirmado = 0"
            ).fetchall()
        already_proposed_ids = {int(r["factura_id"]) for r in proposed_rows}
        # Todas las facturas pendientes o parciales (sin límite arbitrario); los albaranes no se pagan
        candidate_invoices = [
            inv
            for inv in self.db.list_unpaid_invoices()
            if inv["id"] not in already_proposed_ids
            and abs(float(inv.get("importe_pendiente") or 0.0)) >= 0.01
        ]
        rules = [
            r for r in self.db.list_rules(active_only=True) if r.get("tipo_regla") == "concepto_proveedor"
        ]

        auto_confirmed = 0
        proposed = 0
        used_invoice_ids: set[int] = set()

        for tx in pending_txs:
            avail_invoices = [inv for inv in candidate_invoices if inv["id"] not in used_invoice_ids]
            if not avail_invoices:
                break

            match = self._find_best_match_for_transaction(tx, avail_invoices, rules)
            if not match:
                continue

            matched_invoices, confidence, allocations = match
            if confidence >= self.auto_confirm_threshold:
                self._save_reconciliation_links(
                    movimiento_id=tx["id"],
                    allocations=allocations,
                    tipo_vinculo="automatico",
                    confianza=confidence,
                    confirmado=True,
                    diferencia_pendiente=0.0,
                )
                for inv in matched_invoices:
                    used_invoice_ids.add(inv["id"])
                auto_confirmed += 1
            elif confidence >= 0.55:
                self._save_reconciliation_links(
                    movimiento_id=tx["id"],
                    allocations=allocations,
                    tipo_vinculo="propuesto",
                    confianza=confidence,
                    confirmado=False,
                    diferencia_pendiente=0.0,
                )
                for inv in matched_invoices:
                    used_invoice_ids.add(inv["id"])
                proposed += 1

        return {
            "conciliados_automaticos": auto_confirmed,
            "propuestas_revision": proposed,
        }

    def _find_best_match_for_transaction(
        self,
        tx: Dict[str, Any],
        invoices: List[Dict[str, Any]],
        concept_rules: List[Dict[str, Any]],
    ) -> Optional[Tuple[List[Dict[str, Any]], float, List[Dict[str, Any]]]]:
        """
        Busca el mejor emparejamiento (individual o agrupado) para un movimiento bancario.
        Un pago bancario de cargo (-X €) liquida facturas cuyo saldo pendiente neto suma +X €.
        """
        tx_amount = float(tx.get("importe") or 0.0)
        if abs(tx_amount) < 0.01:
            return None

        # Importe neto de facturas esperado: si el movimiento bancario es un cargo (-120 €),
        # buscamos facturas pendientes por +120 €. Si es un abono bancario (+50 €), buscamos abonos (-50 €).
        target_invoice_sum = round(-tx_amount, 2)
        concept = (tx.get("concepto") or "").lower()
        ref = (tx.get("referencia") or "").lower()
        full_text = f"{concept} {ref}"
        tx_date = _parse_date(tx.get("fecha"))

        # Proveedor sugerido por regla aprendida de concepto bancario
        rule_supplier_name = ""
        for r in concept_rules:
            pat = (r.get("patron") or "").lower().strip()
            if pat and pat in full_text:
                rule_supplier_name = (r.get("proveedor_nombre") or "").lower().strip()
                break

        # 1. Evaluar candidatos individuales (1:1)
        best_single: Optional[Tuple[Dict[str, Any], float]] = None
        for inv in invoices:
            pending_amt = round(float(inv.get("importe_pendiente") or inv.get("total") or 0.0), 2)
            amt_diff = abs(pending_amt - target_invoice_sum)
            if amt_diff > 0.05:
                continue

            score = 0.55  # Base por coincidencia exacta de importe
            inv_num = (inv.get("numero_factura") or "").lower().strip()
            sup_name = (inv.get("proveedor_nombre") or "").lower().strip()
            sup_nif = (inv.get("nif") or "").lower().strip()

            # Coincidencia de número de factura en el concepto
            if inv_num and len(inv_num) >= 3 and inv_num in full_text:
                score += 0.30

            # Coincidencia de proveedor o NIF o regla aprendida
            if sup_nif and sup_nif in full_text:
                score += 0.25
            elif sup_name and _supplier_tokens_match(sup_name, full_text):
                score += 0.22
            elif rule_supplier_name and rule_supplier_name in sup_name:
                score += 0.22

            # Cercanía de fecha al vencimiento o emisión
            ref_date = _parse_date(inv.get("fecha_vencimiento") or inv.get("fecha_emision"))
            if tx_date and ref_date:
                days_diff = abs((tx_date - ref_date).days)
                if days_diff <= 5:
                    score += 0.15
                elif days_diff <= 15:
                    score += 0.10
                elif days_diff <= 35:
                    score += 0.05
                elif days_diff > 90:
                    score -= 0.15

            score = round(max(0.0, min(0.99, score)), 2)
            if best_single is None or score > best_single[1]:
                best_single = (inv, score)

        if best_single and best_single[1] >= 0.75:
            inv, sc = best_single
            pending_amt = round(float(inv.get("importe_pendiente") or inv.get("total") or 0.0), 2)
            return ([inv], sc, [{"factura_id": inv["id"], "importe_asignado": pending_amt}])

        # 2. Evaluar pagos agrupados (1 movimiento cubre 2..4 facturas/abonos del mismo proveedor)
        by_supplier: Dict[str, List[Dict[str, Any]]] = {}
        for inv in invoices:
            sup_key = (inv.get("proveedor_nombre") or "").strip().lower()
            if sup_key:
                by_supplier.setdefault(sup_key, []).append(inv)

        for sup_key, sup_invoices in by_supplier.items():
            if len(sup_invoices) < 2:
                continue
            supplier_mentioned = _supplier_tokens_match(sup_key, full_text) or (
                rule_supplier_name and rule_supplier_name in sup_key
            )
            pool = sup_invoices[:10]
            for r_size in range(2, min(5, len(pool) + 1)):
                for combo in combinations(pool, r_size):
                    combo_sum = round(
                        sum(float(i.get("importe_pendiente") or i.get("total") or 0.0) for i in combo),
                        2,
                    )
                    if abs(combo_sum - target_invoice_sum) <= 0.05:
                        conf = 0.88 if supplier_mentioned else 0.72
                        allocs = [
                            {
                                "factura_id": i["id"],
                                "importe_asignado": round(
                                    float(i.get("importe_pendiente") or i.get("total") or 0.0), 2
                                ),
                            }
                            for i in combo
                        ]
                        return (list(combo), conf, allocs)

        if best_single:
            inv, sc = best_single
            pending_amt = round(float(inv.get("importe_pendiente") or inv.get("total") or 0.0), 2)
            return ([inv], sc, [{"factura_id": inv["id"], "importe_asignado": pending_amt}])

        return None

    # -----------------------------------------------------------------
    # Comprobaciones comunes (dentro de la misma transacción)
    # -----------------------------------------------------------------

    @staticmethod
    def _remaining_for_invoice(conn: Any, invoice_id: int, exclude_movement_id: int) -> Tuple[Dict[str, Any], float]:
        """Factura y lo que le queda por pagar sin contar los vínculos del propio movimiento."""
        inv = conn.execute(
            "SELECT id, numero_factura, proveedor_nombre, total, tipo_documento FROM facturas WHERE id = ?",
            (invoice_id,),
        ).fetchone()
        if not inv:
            raise ReconciliationNotFound(f"La factura {invoice_id} no existe.")
        paid = conn.execute(
            """
            SELECT COALESCE(SUM(importe_asignado), 0.0) AS pagado FROM vinculos_conciliacion
            WHERE factura_id = ? AND confirmado = 1 AND movimiento_id != ?
            """,
            (invoice_id, exclude_movement_id),
        ).fetchone()
        return dict(inv), round(float(inv["total"] or 0.0) - float(paid["pagado"] or 0.0), 2)

    @staticmethod
    def _check_amount(inv: Dict[str, Any], remaining: float, amount: float) -> None:
        etiqueta = f"{inv.get('numero_factura') or '#' + str(inv['id'])} de {inv.get('proveedor_nombre') or 'proveedor'}"
        if abs(amount) < 0.005:
            raise ReconciliationError(f"El importe asignado a la factura {etiqueta} no puede ser 0.")
        if abs(remaining) < 0.01:
            raise ReconciliationError(f"La factura {etiqueta} ya está pagada del todo por otro movimiento del banco.")
        if (amount > 0) != (remaining > 0) or abs(amount) > abs(remaining) + 0.01:
            raise ReconciliationError(
                f"El importe asignado a la factura {etiqueta} ({amount:.2f} €) es mayor que lo que le queda "
                f"por pagar ({remaining:.2f} €)."
            )

    @staticmethod
    def _drop_conflicting_proposals(conn: Any, invoice_ids: List[int], movimiento_id: int) -> List[int]:
        """Quita las propuestas sin confirmar de esas facturas en otros movimientos (que quedan pendientes)."""
        touched: set[int] = set()
        for inv_id in invoice_ids:
            for r in conn.execute(
                "SELECT DISTINCT movimiento_id FROM vinculos_conciliacion WHERE factura_id = ? AND confirmado = 0 AND movimiento_id != ?",
                (inv_id, movimiento_id),
            ).fetchall():
                touched.add(int(r["movimiento_id"]))
        for mov_id in touched:
            # La propuesta de ese movimiento era un conjunto: se descarta entera y vuelve a 'pendiente'
            conn.execute("DELETE FROM vinculos_conciliacion WHERE movimiento_id = ? AND confirmado = 0", (mov_id,))
            if conn.execute(
                "SELECT 1 FROM vinculos_conciliacion WHERE movimiento_id = ? LIMIT 1", (mov_id,)
            ).fetchone() is None:
                conn.execute(
                    "UPDATE movimientos_bancarios SET estado_conciliacion = 'pendiente', diferencia_pendiente = 0.0 WHERE id = ?",
                    (mov_id,),
                )
        return sorted(touched)

    def confirm_proposal(self, movimiento_id: int) -> Dict[str, Any]:
        """
        Confirma los vínculos propuestos de un movimiento, comprobando de nuevo que cada factura
        sigue teniendo ese importe pendiente (una propuesta antigua no puede pagar dos veces).
        """
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            mov = conn.execute("SELECT id FROM movimientos_bancarios WHERE id = ?", (movimiento_id,)).fetchone()
            if not mov:
                raise ReconciliationNotFound(f"El movimiento bancario {movimiento_id} no existe.")
            rows = conn.execute(
                "SELECT factura_id, importe_asignado FROM vinculos_conciliacion WHERE movimiento_id = ? AND confirmado = 0",
                (movimiento_id,),
            ).fetchall()
            if not rows:
                raise ReconciliationError("Este movimiento no tiene ninguna propuesta pendiente de confirmar.")
            affected_invoices = [int(r["factura_id"]) for r in rows]
            for r in rows:
                inv, remaining = self._remaining_for_invoice(conn, int(r["factura_id"]), movimiento_id)
                self._check_amount(inv, remaining, float(r["importe_asignado"]))
            conn.execute(
                "UPDATE vinculos_conciliacion SET confirmado = 1 WHERE movimiento_id = ?",
                (movimiento_id,),
            )
            conn.execute(
                "UPDATE movimientos_bancarios SET estado_conciliacion = 'conciliado' WHERE id = ?",
                (movimiento_id,),
            )
            released = self._drop_conflicting_proposals(conn, affected_invoices, movimiento_id)

        for inv_id in affected_invoices:
            self.db.recalculate_invoice_payment_status(inv_id)

        return {
            "movimiento_id": movimiento_id,
            "estado": "conciliado",
            "facturas_actualizadas": affected_invoices,
            "propuestas_descartadas_en_movimientos": released,
        }

    def reject_proposal(self, movimiento_id: int) -> Dict[str, Any]:
        """Descarta una propuesta o desvincula un movimiento bancario dejándolo pendiente."""
        affected_invoices: List[int] = []
        with self.db.connect() as conn:
            mov = conn.execute("SELECT id FROM movimientos_bancarios WHERE id = ?", (movimiento_id,)).fetchone()
            if not mov:
                raise ReconciliationNotFound(f"El movimiento bancario {movimiento_id} no existe.")
            rows = conn.execute(
                "SELECT factura_id FROM vinculos_conciliacion WHERE movimiento_id = ?",
                (movimiento_id,),
            ).fetchall()
            affected_invoices = [int(r["factura_id"]) for r in rows]
            conn.execute(
                "DELETE FROM vinculos_conciliacion WHERE movimiento_id = ?",
                (movimiento_id,),
            )
            conn.execute(
                """
                UPDATE movimientos_bancarios
                SET estado_conciliacion = 'pendiente', diferencia_pendiente = 0.0
                WHERE id = ?
                """,
                (movimiento_id,),
            )

        for inv_id in affected_invoices:
            self.db.recalculate_invoice_payment_status(inv_id)

        return {"movimiento_id": movimiento_id, "estado": "pendiente", "facturas_actualizadas": affected_invoices}

    def reconcile_manual(
        self,
        movimiento_id: int,
        asignaciones: List[Dict[str, Any]],
        notas: str = "",
        aprender_regla: bool = True,
    ) -> Dict[str, Any]:
        """
        Emparejamiento manual muchos-a-muchos (Sección 8), en una sola transacción:
        - Comprueba que el movimiento y las facturas existen (si no, ReconciliationNotFound).
        - Cada importe no puede superar lo que le queda por pagar a la factura (ReconciliationError).
        - Si la suma no cuadra con el movimiento (descuento, abono...), se guarda igualmente y se anota
          la diferencia como pendiente (`diferencia_pendiente`).
        - Las propuestas sin confirmar de esas facturas en otros movimientos se descartan.
        """
        if not asignaciones:
            raise ReconciliationError("Marca al menos una factura para unir con el movimiento.")
        clean: Dict[int, float] = {}
        for a in asignaciones:
            try:
                inv_id = int(a["factura_id"])
                amount = round(float(a["importe_asignado"]), 2)
            except (KeyError, TypeError, ValueError):
                raise ReconciliationError("Cada factura debe llevar su número y un importe válido.")
            if amount != amount or amount in (float("inf"), float("-inf")):
                raise ReconciliationError("Importe no válido.")
            clean[inv_id] = round(clean.get(inv_id, 0.0) + amount, 2)

        now = _now_iso()
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            tx_row = conn.execute(
                "SELECT * FROM movimientos_bancarios WHERE id = ?", (movimiento_id,)
            ).fetchone()
            if not tx_row:
                raise ReconciliationNotFound(f"Movimiento bancario {movimiento_id} no encontrado.")
            tx = dict(tx_row)
            first_inv: Optional[Dict[str, Any]] = None
            for inv_id, amount in clean.items():
                inv, remaining = self._remaining_for_invoice(conn, inv_id, movimiento_id)
                self._check_amount(inv, remaining, amount)
                first_inv = first_inv or inv

            previous_invoice_ids = [
                int(r["factura_id"])
                for r in conn.execute(
                    "SELECT factura_id FROM vinculos_conciliacion WHERE movimiento_id = ?", (movimiento_id,)
                ).fetchall()
            ]
            tx_amount = float(tx["importe"])
            assigned_sum = round(sum(clean.values()), 2)
            diferencia = round(-tx_amount - assigned_sum, 2)

            conn.execute("DELETE FROM vinculos_conciliacion WHERE movimiento_id = ?", (movimiento_id,))
            for inv_id, amount in clean.items():
                conn.execute(
                    """
                    INSERT INTO vinculos_conciliacion
                    (movimiento_id, factura_id, importe_asignado, tipo_vinculo, confianza, confirmado, created_at)
                    VALUES (?, ?, ?, 'manual', 1.0, 1, ?)
                    """,
                    (movimiento_id, inv_id, amount, now),
                )
            conn.execute(
                """
                UPDATE movimientos_bancarios
                SET estado_conciliacion = 'conciliado', diferencia_pendiente = ?,
                    notas = CASE WHEN ? != '' THEN ? ELSE notas END
                WHERE id = ?
                """,
                (diferencia, notas, notas, movimiento_id),
            )
            self._drop_conflicting_proposals(conn, list(clean.keys()), movimiento_id)

        for inv_id in sorted(set(previous_invoice_ids) | set(clean.keys())):
            self.db.recalculate_invoice_payment_status(inv_id)

        # Aprender regla concepto -> proveedor
        if aprender_regla and first_inv and first_inv.get("proveedor_nombre"):
            concept_key = _extract_reusable_concept_pattern(tx.get("concepto") or "")
            full_inv = self.db.get_invoice_by_id(int(first_inv["id"])) or {}
            if concept_key:
                self.db.create_rule(
                    tipo_regla="concepto_proveedor",
                    patron=concept_key,
                    proveedor_id=full_inv.get("proveedor_id"),
                    proveedor_nombre=first_inv["proveedor_nombre"],
                    categoria=full_inv.get("categoria") or "",
                    aprendida=True,
                )

        return {
            "movimiento_id": movimiento_id,
            "importe_movimiento": tx_amount,
            "suma_asignada": assigned_sum,
            "cuadra_exacto": abs(diferencia) <= 0.05,
            "diferencia_pendiente": diferencia,
            "estado_conciliacion": "conciliado",
        }

    def _save_reconciliation_links(
        self,
        movimiento_id: int,
        allocations: List[Dict[str, Any]],
        tipo_vinculo: str,
        confianza: float,
        confirmado: bool,
        diferencia_pendiente: float,
        notas: str = "",
    ) -> None:
        now = _now_iso()
        estado_mov = "conciliado" if confirmado else "propuesto"
        invoice_ids: List[int] = []

        previous_invoice_ids: List[int] = []

        with self.db.connect() as conn:
            prev_rows = conn.execute(
                "SELECT factura_id FROM vinculos_conciliacion WHERE movimiento_id = ?",
                (movimiento_id,),
            ).fetchall()
            previous_invoice_ids = [int(r["factura_id"]) for r in prev_rows]
            conn.execute(
                "DELETE FROM vinculos_conciliacion WHERE movimiento_id = ?",
                (movimiento_id,),
            )
            for alloc in allocations:
                inv_id = int(alloc["factura_id"])
                imp = round(float(alloc["importe_asignado"]), 2)
                invoice_ids.append(inv_id)
                conn.execute(
                    """
                    INSERT INTO vinculos_conciliacion
                    (movimiento_id, factura_id, importe_asignado, tipo_vinculo, confianza, confirmado, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (movimiento_id, inv_id, imp, tipo_vinculo, round(confianza, 2), 1 if confirmado else 0, now),
                )

            conn.execute(
                """
                UPDATE movimientos_bancarios
                SET estado_conciliacion = ?,
                    diferencia_pendiente = ?,
                    notas = CASE WHEN ? != '' THEN ? ELSE notas END
                WHERE id = ?
                """,
                (estado_mov, round(diferencia_pendiente, 2), notas, notas, movimiento_id),
            )

        # Recalcular también las facturas que estaban vinculadas antes y han dejado de estarlo
        to_recalc = set(previous_invoice_ids)
        if confirmado:
            to_recalc.update(invoice_ids)
        for inv_id in sorted(to_recalc):
            self.db.recalculate_invoice_payment_status(inv_id)


def _parse_date(date_str: Optional[str]) -> Optional[dt.date]:
    if not date_str:
        return None
    try:
        return dt.date.fromisoformat(date_str[:10])
    except ValueError:
        return None


def _supplier_tokens_match(supplier_name: str, bank_text: str) -> bool:
    stop_words = {"s.l.", "s.a.", "s.l.u.", "sl", "sa", "slu", "s.coop", "coop", "farmaceutica", "distribucion", "de", "la", "el", "los", "las", "y"}
    tokens = [
        t
        for t in re.split(r"[\s,.;:\-_]+", supplier_name.lower())
        if len(t) >= 3 and t not in stop_words
    ]
    if not tokens:
        return False
    return any(tok in bank_text for tok in tokens)


def _extract_reusable_concept_pattern(concept: str) -> str:
    """Extrae la parte estable de un concepto bancario (sin fechas ni números de recibo variables)."""
    cleaned = re.sub(r"\b\d{2}[/-]\d{2}[/-]\d{2,4}\b", "", concept)
    cleaned = re.sub(r"\b(?:RECIBO|ADEUDO|SEPA|N[º°o]?|FACTURA|FRA|REF|DOC)\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b[A-Z0-9/\-_]*\d{3,}[A-Z0-9/\-_]*\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -:/")
    return cleaned[:45] if len(cleaned) >= 4 else ""
