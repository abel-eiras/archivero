"""Tests de Fases 5 y 6: Conciliación bancaria (Norma 43, CAMT.053, CSV), Exportación Excel y Actualizador."""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

import openpyxl

from app.db.database import Database
from app.services.excel_exporter import AccountingExcelExporter
from app.services.pipeline import InvoiceIngestionPipeline
from app.services.reconciliation import BankReconciliationService
from app.services.updater_service import (
    extract_sha256_from_release_body,
    is_newer_version,
    verify_sha256_checksum,
)
from scripts.generate_sample_data import (
    build_invoice_pdf_bytes,
    build_sample_camt053_xml,
    build_sample_csv_statement,
    build_sample_norma43_statement,
    get_sample_invoices_specs,
)
import updater


def test_bank_reconciliation_norma43_camt053_csv_and_grouped_payments(tmp_path: Path):
    db = Database(db_path=tmp_path / "bank.sqlite", backup_dir=tmp_path / "backups")
    db.set_settings({"carpeta_salida": str(tmp_path / "archivo")})
    pipeline = InvoiceIngestionPipeline(db=db)

    # Ingerir todas las facturas de muestra (Cofares, Bidafarma factura + abono, Iberdrola, Alquiler, Asesoría)
    for spec in get_sample_invoices_specs():
        pipeline.ingest_pdf(
            pdf_source=build_invoice_pdf_bytes(spec["lines"]),
            filename=spec["filename"],
            sender_email=spec["sender"],
        )

    reconciler = BankReconciliationService(db=db)

    # 1. Importar Norma 43 (3 movimientos: Cofares -1254.00, Bidafarma agrupado -783.75, Iberdrola -254.10)
    res_n43 = reconciler.import_statement(
        content=build_sample_norma43_statement(),
        filename="extracto.n43",
        formato="norma43",
        banco="CaixaBank",
        auto_reconcile=True,
    )
    assert res_n43["nuevos_insertados"] == 3
    assert res_n43["duplicados_omitidos"] == 0
    assert res_n43["conciliacion_automatica"]["conciliados_automaticos"] == 3

    # Reimportar el mismo Norma 43 no duplica nada
    res_n43_dup = reconciler.import_statement(
        content=build_sample_norma43_statement(),
        filename="extracto.n43",
        formato="norma43",
        banco="CaixaBank",
        auto_reconcile=False,
    )
    assert res_n43_dup["nuevos_insertados"] == 0
    assert res_n43_dup["duplicados_omitidos"] == 3

    # Verificar que tanto la factura de Bidafarma (888.25) como su abono (-104.50) quedaron 'pagada'
    bidafarma_invs = db.list_invoices(proveedor="Bidafarma")
    assert len(bidafarma_invs) == 2
    assert all(i["estado_pago"] == "pagada" for i in bidafarma_invs)

    # 2. Importar CAMT.053 XML (Alquiler -1530.00 €)
    res_camt = reconciler.import_statement(
        content=build_sample_camt053_xml(),
        filename="extracto.xml",
        formato="camt053",
        banco="CaixaBank",
        auto_reconcile=True,
    )
    assert res_camt["nuevos_insertados"] == 1
    assert res_camt["conciliacion_automatica"]["conciliados_automaticos"] == 1

    # 3. Importar CSV y probar emparejamiento manual con diferencia (ej. descuento pronto pago)
    res_csv = reconciler.import_statement(
        content=build_sample_csv_statement(),
        filename="extracto.csv",
        formato="csv",
        banco="BBVA",
        auto_reconcile=False,
    )
    assert res_csv["nuevos_insertados"] == 1
    pending_txs = db.list_bank_transactions(estado_conciliacion="pendiente")
    assert len(pending_txs) == 1
    tx_asesoria = pending_txs[0]

    inv_asesoria = db.list_invoices(proveedor="Asesoría")[0]
    # Conciliar manualmente imputando 240.00 € sobre un movimiento de -242.00 € (diferencia 2.00 €)
    manual_res = reconciler.reconcile_manual(
        movimiento_id=tx_asesoria["id"],
        asignaciones=[{"factura_id": inv_asesoria["id"], "importe_asignado": 240.00}],
        notas="Ajuste comisión",
        aprender_regla=True,
    )
    assert manual_res["estado_conciliacion"] == "conciliado"
    assert manual_res["diferencia_pendiente"] == 2.00
    assert manual_res["cuadra_exacto"] is False

    # Verificar que se aprendió la regla 'concepto_proveedor'
    learned_rules = [r for r in db.list_rules() if r["tipo_regla"] == "concepto_proveedor" and r["aprendida"] == 1]
    assert len(learned_rules) >= 1


def test_excel_accounting_export_from_sqlite(tmp_path: Path):
    db = Database(db_path=tmp_path / "excel.sqlite", backup_dir=tmp_path / "backups")
    db.set_settings({"carpeta_salida": str(tmp_path / "archivo")})
    pipeline = InvoiceIngestionPipeline(db=db)

    spec = get_sample_invoices_specs()[0]
    pipeline.ingest_pdf(build_invoice_pdf_bytes(spec["lines"]), filename=spec["filename"])

    exporter = AccountingExcelExporter(db=db)
    xlsx_bytes = exporter.generate_workbook_bytes()
    wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes))
    assert "Facturas Recibidas" in wb.sheetnames
    assert "Resumen Mensual y Categorías" in wb.sheetnames
    assert "Por Proveedor" in wb.sheetnames
    assert "Conciliación Bancaria" in wb.sheetnames


def test_updater_sha256_integrity_and_atomic_replacement(tmp_path: Path):
    assert is_newer_version("v0.2.0", "0.1.0") is True
    assert is_newer_version("0.1.0", "0.1.0") is False

    new_bin = tmp_path / "FarmaFacturas_new.exe"
    old_bin = tmp_path / "FarmaFacturas.exe"
    new_content = b"NEW_BINARY_PAYLOAD_V020"
    old_content = b"OLD_BINARY_PAYLOAD_V010"
    new_bin.write_bytes(new_content)
    old_bin.write_bytes(old_content)

    good_sha256 = hashlib.sha256(new_content).hexdigest()
    bad_sha256 = "0" * 64

    release_notes = f"Release v0.2.0\n\nSHA256:\n{good_sha256}  FarmaFacturas_new.exe\n"
    extracted = extract_sha256_from_release_body(release_notes, "FarmaFacturas_new.exe")
    assert extracted == good_sha256
    assert verify_sha256_checksum(new_bin, good_sha256) is True
    assert verify_sha256_checksum(new_bin, bad_sha256) is False

    # Intento con hash incorrecto: debe rechazar la actualización y mantener el binario original
    rc_bad = updater.apply_update(source_path=new_bin, target_path=old_bin, expected_sha256=bad_sha256)
    assert rc_bad == 3
    assert old_bin.read_bytes() == old_content

    # Intento con hash correcto: debe crear backup .bak y reemplazar el ejecutable
    rc_ok = updater.apply_update(source_path=new_bin, target_path=old_bin, expected_sha256=good_sha256)
    assert rc_ok == 0
    assert old_bin.read_bytes() == new_content
    assert old_bin.with_suffix(".exe.bak").read_bytes() == old_content
