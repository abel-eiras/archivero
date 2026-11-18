"""Tests de Fase 2: SQLite como fuente de verdad, backups de migración y almacenamiento seguro."""

from __future__ import annotations

from pathlib import Path

from app.db.database import Database
from app.services.pipeline import InvoiceIngestionPipeline
from app.services.storage import LocalFolderStorage
from scripts.generate_sample_data import build_invoice_pdf_bytes, get_sample_invoices_specs


def test_database_migrations_and_backup(tmp_path: Path):
    db_file = tmp_path / "test.sqlite"
    backup_dir = tmp_path / "backups"
    db = Database(db_path=db_file, backup_dir=backup_dir)

    assert db.get_schema_version() >= 1
    backup_path = db.create_backup(reason="unit_test")
    assert backup_path is not None
    assert backup_path.is_file()


def test_storage_folder_structure_and_missing_file_resilience(tmp_path: Path):
    archive_dir = tmp_path / "archivo_gestoria"
    staging_dir = tmp_path / "staging"
    db = Database(db_path=tmp_path / "db.sqlite", backup_dir=tmp_path / "backups")
    db.set_settings({"carpeta_salida": str(archive_dir)})

    storage = LocalFolderStorage(base_output_dir=archive_dir, staging_dir=staging_dir)
    pipeline = InvoiceIngestionPipeline(db=db, storage_override=storage)

    spec = get_sample_invoices_specs()[0]
    pdf_bytes = build_invoice_pdf_bytes(spec["lines"])

    # 1. Ingesta inicial
    res1 = pipeline.ingest_pdf(pdf_bytes, filename="cofares.pdf", sender_email=spec["sender"])
    assert res1.status == "insertada"
    assert res1.invoice is not None

    archived_path = Path(res1.invoice["ruta_archivo"])
    assert archived_path.is_file()
    # Verificar estructura fija: Año / Mes / Categoría / Proveedor / {fecha}_{proveedor}_{numero}.pdf
    rel_parts = archived_path.relative_to(archive_dir).parts
    assert len(rel_parts) == 5
    assert rel_parts[0] == "2026"
    assert rel_parts[1] == "11-Noviembre"
    assert rel_parts[2] == "compras_mercancia"
    assert "Cofares" in rel_parts[3]
    assert rel_parts[4].startswith("2026-11-02_")

    # 2. Detección de duplicados por hash aunque llegue con otro nombre de archivo
    res2 = pipeline.ingest_pdf(pdf_bytes, filename="otro_nombre_distinto.pdf")
    assert res2.status == "duplicada"
    assert res2.invoice_id == res1.invoice_id

    # 3. Principio 2 y Sección 6: si alguien borra o mueve el PDF archivado, el dashboard sigue funcionando
    archived_path.unlink()
    inv_after_delete = db.get_invoice_by_id(res1.invoice_id)
    assert inv_after_delete is not None
    assert inv_after_delete["archivo_disponible"] is False
    metrics = db.get_dashboard_metrics()
    assert metrics["resumen"]["total_facturas"] == 1
    assert metrics["resumen"]["gasto_total"] == 1254.00
