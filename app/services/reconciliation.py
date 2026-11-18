"""Motor de conciliación bancaria: emparejamiento automático y manual muchos-a-muchos (Fase 6)."""

from __future__ import annotations

import datetime as dt
from itertools import combinations
import logging
import re
from typing import Any, Dict, List, Optional, Tuple, Union

from app.db.database import Database
from app.models.schemas import CSVMappingConfig
from app.services.bank_parsers import parse_camt053, parse_csv_or_excel, parse_norma43

logger = logging.getLogger(__name__)


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

        return {
            "formato_detectado": fmt,
            "total_leidos": len(parsed_txs),
            "nuevos_insertados": nuevos,
            "duplicados_omitidos": duplicados,
            "conciliacion_automatica": reconciliation_summary,
        }

    def run_auto_reconciliation(self) -> Dict[str, int]:
        """
        Ejecuta el conciliador automático sobre los movimientos pendientes:
        - Compara importe, cercanía de fecha al vencimiento, proveedor/NIF/número de factura en el concepto
          y reglas aprendidas ('concepto_proveedor').
        - Soporta emparejamiento 1:1 y pagos agrupados 1:N (incluyendo abonos que restan).
        - Marca automáticamente los de alta confianza (>= umbral) y deja como propuesta los dudosos.
        """
        pending_txs = self.db.list_bank_transactions(estado_conciliacion="pendiente")
        candidate_invoices = [
            inv for inv in self.db.list_invoices(limit=1000) if inv.get("estado_pago") != "pagada"
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

    def confirm_proposal(self, movimiento_id: int) -> Dict[str, Any]:
        """Confirma los vínculos propuestos para un movimiento bancario."""
        affected_invoices: List[int] = []
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT factura_id FROM vinculos_conciliacion WHERE movimiento_id = ?",
                (movimiento_id,),
            ).fetchall()
            affected_invoices = [int(r["factura_id"]) for r in rows]
            conn.execute(
                "UPDATE vinculos_conciliacion SET confirmado = 1 WHERE movimiento_id = ?",
                (movimiento_id,),
            )
            conn.execute(
                "UPDATE movimientos_bancarios SET estado_conciliacion = 'conciliado' WHERE id = ?",
                (movimiento_id,),
            )

        for inv_id in affected_invoices:
            self.db.recalculate_invoice_payment_status(inv_id)

        return {"movimiento_id": movimiento_id, "estado": "conciliado", "facturas_actualizadas": affected_invoices}

    def reject_proposal(self, movimiento_id: int) -> Dict[str, Any]:
        """Descarta una propuesta o desvincula un movimiento bancario dejándolo pendiente."""
        affected_invoices: List[int] = []
        with self.db.connect() as conn:
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
        Emparejamiento manual muchos-a-muchos (Sección 8):
        - El usuario selecciona un movimiento y marca las facturas que lo componen (o un pago parcial).
        - La app comprueba si la suma cuadra con el movimiento.
        - Si hay diferencia (por ejemplo por un descuento o abono), permite guardar igualmente
          y anota la diferencia como pendiente (`diferencia_pendiente`).
        """
        with self.db.connect() as conn:
            tx_row = conn.execute(
                "SELECT * FROM movimientos_bancarios WHERE id = ?", (movimiento_id,)
            ).fetchone()
            if not tx_row:
                raise ValueError(f"Movimiento bancario {movimiento_id} no encontrado.")
            tx = dict(tx_row)

        tx_amount = float(tx["importe"])
        expected_net_invoices = round(-tx_amount, 2)
        assigned_sum = round(sum(float(a["importe_asignado"]) for a in asignaciones), 2)
        diferencia = round(expected_net_invoices - assigned_sum, 2)

        self._save_reconciliation_links(
            movimiento_id=movimiento_id,
            allocations=asignaciones,
            tipo_vinculo="manual",
            confianza=1.0,
            confirmado=True,
            diferencia_pendiente=diferencia,
            notas=notas,
        )

        # Aprender regla concepto -> proveedor si todas las facturas pertenecen al mismo proveedor
        if aprender_regla and asignaciones:
            first_inv = self.db.get_invoice_by_id(int(asignaciones[0]["factura_id"]))
            concept_key = _extract_reusable_concept_pattern(tx.get("concepto") or "")
            if first_inv and first_inv.get("proveedor_nombre") and concept_key:
                self.db.create_rule(
                    tipo_regla="concepto_proveedor",
                    patron=concept_key,
                    proveedor_id=first_inv.get("proveedor_id"),
                    proveedor_nombre=first_inv["proveedor_nombre"],
                    categoria=first_inv.get("categoria") or "",
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

        with self.db.connect() as conn:
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

        if confirmado:
            for inv_id in invoice_ids:
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
