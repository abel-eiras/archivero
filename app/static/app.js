/* Lógica de cliente de Archivero (100% local, pensada para uso sencillo en farmacia) */

const state = {
  categorias: [],
  tiposDocumento: {},
  selectedBankTx: null,
  unpaidInvoices: [],
  bankTransactionsById: {},
  currentEditingInvoice: null,
};

const RULE_TYPE_LABELS = {
  remitente_categoria: "Correo del remitente",
  remitente_proveedor: "Correo del remitente",
  nif_categoria: "NIF o nombre de proveedor",
  concepto_proveedor: "Concepto del banco",
};

const MAIL_TYPE_LABELS = {
  imap: "Correo con contraseña",
  oauth_gmail: "Gmail (Google)",
  oauth_outlook: "Outlook / Hotmail",
};

function fmtEur(val) {
  const num = Number(val || 0);
  return num.toLocaleString("es-ES", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + " €";
}

function showToast(msg) {
  const container = document.getElementById("toast-container");
  const el = document.createElement("div");
  el.className = "toast";
  el.textContent = msg;
  container.appendChild(el);
  setTimeout(() => el.remove(), 4500);
}

function categoryName(code) {
  const found = state.categorias.find((c) => c.codigo === code);
  return found ? found.nombre : code || "Otros";
}

async function apiFetch(url, options = {}) {
  const resp = await fetch(url, options);
  if (!resp.ok) {
    let detail = `Error (${resp.status})`;
    try {
      const errJson = await resp.json();
      detail = errJson.detail || JSON.stringify(errJson);
    } catch (_) {}
    throw new Error(detail);
  }
  return resp.json();
}

/* =========================================================
   Ayudas comunes: botones ocupados y paginación
   ========================================================= */
// Desactiva el botón mientras dura la petición para evitar envíos dobles.
async function withBusyButton(btn, fn) {
  if (btn && btn.disabled) return;
  if (btn) {
    btn.disabled = true;
    btn.setAttribute("aria-busy", "true");
  }
  try {
    return await fn();
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.removeAttribute("aria-busy");
    }
  }
}

// Registra el envío de un formulario desactivando sus botones de envío mientras se procesa.
function onSubmitBusy(formId, handler) {
  const form = document.getElementById(formId);
  if (!form) return;
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (form.dataset.busy === "1") return;
    const buttons = form.querySelectorAll('button[type="submit"], button:not([type]), input[type="submit"]');
    form.dataset.busy = "1";
    buttons.forEach((b) => {
      b.disabled = true;
      b.setAttribute("aria-busy", "true");
    });
    try {
      await handler(e);
    } catch (err) {
      showToast("Error: " + err.message);
    } finally {
      form.dataset.busy = "";
      buttons.forEach((b) => {
        b.disabled = false;
        b.removeAttribute("aria-busy");
      });
    }
  });
}

const pageState = {
  facturas: { offset: 0, limit: 50 },
  revision: { offset: 0, limit: 50 },
  bank: { offset: 0, limit: 50 },
};

function renderPager(containerId, key, total, reload) {
  const box = document.getElementById(containerId);
  if (!box) return;
  const ps = pageState[key];
  if (ps.offset >= total && total > 0) {
    ps.offset = Math.max(0, Math.floor((total - 1) / ps.limit) * ps.limit);
  }
  if (total <= ps.limit) {
    box.innerHTML = total ? `<span class="pager-info">${Number(total)} en total</span>` : "";
    return;
  }
  const from = ps.offset + 1;
  const to = Math.min(ps.offset + ps.limit, total);
  box.innerHTML = `
    <button type="button" class="btn btn-secondary btn-sm" data-dir="prev" aria-label="Página anterior" ${ps.offset === 0 ? "disabled" : ""}>‹ Anterior</button>
    <span class="pager-info">${from}–${to} de ${Number(total)}</span>
    <button type="button" class="btn btn-secondary btn-sm" data-dir="next" aria-label="Página siguiente" ${to >= total ? "disabled" : ""}>Siguiente ›</button>
  `;
  box.querySelectorAll("button").forEach((b) =>
    b.addEventListener("click", () => {
      ps.offset = b.dataset.dir === "prev" ? Math.max(0, ps.offset - ps.limit) : ps.offset + ps.limit;
      reload();
    })
  );
}

function docTypeBadge(inv) {
  if (inv.tipo_documento === "albaran") return ` <span class="badge badge-neutral">Albarán</span>`;
  if (inv.tipo_documento === "abono") return ` <span class="badge badge-info">Abono</span>`;
  return "";
}

/* =========================================================
   Navegación por pestañas
   ========================================================= */
function setupTabs() {
  const buttons = document.querySelectorAll(".nav-btn");
  const panels = document.querySelectorAll(".tab-panel");
  const titleMap = {
    "tab-dashboard": "Resumen General",
    "tab-revision": "Bandeja de Revisión",
    "tab-facturas": "Todas las Facturas",
    "tab-conciliacion": "Cuadrar con el Banco",
    "tab-actividad": "Actividad y Avisos",
    "tab-configuracion": "Configuración Fácil",
    "tab-ayuda": "Ayuda Paso a Paso",
  };

  buttons.forEach((btn) => {
    btn.addEventListener("click", () => {
      const target = btn.getAttribute("data-tab");
      buttons.forEach((b) => b.classList.remove("active"));
      panels.forEach((p) => p.classList.remove("active"));
      btn.classList.add("active");
      document.getElementById(target).classList.add("active");
      document.getElementById("current-screen-title").textContent = titleMap[target] || "Archivero";

      if (target === "tab-dashboard") loadDashboard();
      if (target === "tab-revision") loadReviewInbox();
      if (target === "tab-facturas") loadInvoicesList();
      if (target === "tab-conciliacion") loadBankReconciliation();
      if (target === "tab-configuracion") loadConfiguration();
      if (target === "tab-actividad") loadActivityScreen();
    });
  });
}

/* =========================================================
   1. Resumen General (Dashboard)
   ========================================================= */
async function loadDashboard() {
  try {
    const data = await apiFetch("/api/dashboard");
    state.categorias = (data.catalogos && data.catalogos.categorias) || [];
    state.tiposDocumento = (data.catalogos && data.catalogos.tipos_documento) || {};
    populateCategorySelects();

    const r = data.resumen || {};
    document.getElementById("kpi-gasto-total").textContent = fmtEur(r.gasto_total);
    document.getElementById("kpi-base-iva").textContent = `Base sin IVA: ${fmtEur(r.base_total)} · IVA: ${fmtEur(r.iva_total)}`;
    document.getElementById("kpi-total-facturas").textContent = String(r.total_facturas || 0);
    document.getElementById("kpi-pendientes-revision").textContent = String(r.pendientes_revision || 0);
    document.getElementById("kpi-importe-impagado").textContent = fmtEur(r.importe_pendiente_pago);
    document.getElementById("kpi-facturas-impagadas").textContent = `${r.facturas_impagadas || 0} facturas pendientes de pago`;

    const badgeRev = document.getElementById("badge-pendientes-revision");
    if (r.pendientes_revision > 0) {
      badgeRev.style.display = "inline-block";
      badgeRev.textContent = String(r.pendientes_revision);
    } else {
      badgeRev.style.display = "none";
    }

    const propCount = (data.conciliacion && data.conciliacion.propuestos) || 0;
    const badgeBank = document.getElementById("badge-propuestas-banco");
    if (propCount > 0) {
      badgeBank.style.display = "inline-block";
      badgeBank.textContent = String(propCount);
    } else {
      badgeBank.style.display = "none";
    }

    renderBarList(
      "chart-por-mes",
      (data.gasto_por_mes || []).map((x) => ({ label: x.mes, count: x.cantidad, total: x.total }))
    );
    renderBarList(
      "chart-por-categoria",
      (data.gasto_por_categoria || []).map((x) => ({
        label: categoryName(x.categoria),
        count: x.cantidad,
        total: x.total,
      }))
    );
    renderBarList(
      "chart-por-proveedor",
      (data.gasto_por_proveedor || []).map((x) => ({
        label: x.proveedor,
        count: x.cantidad,
        total: x.total,
      }))
    );

    const tbodyPend = document.getElementById("tbody-pendientes-pago");
    tbodyPend.innerHTML = "";
    (data.pendientes_pago || []).forEach((inv) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${escapeHtml(inv.fecha_emision || "-")}</td>
        <td>${escapeHtml(inv.proveedor_nombre)}</td>
        <td>${escapeHtml(inv.numero_factura)}</td>
        <td>${escapeHtml(categoryName(inv.categoria))}</td>
        <td class="num">${fmtEur(inv.importe_pendiente ?? inv.total)}</td>
      `;
      tbodyPend.appendChild(tr);
    });
    if (!data.pendientes_pago || data.pendientes_pago.length === 0) {
      tbodyPend.innerHTML = `<tr><td colspan="5" style="text-align:center;color:#64748b;">Todas las facturas registradas están ya pagadas.</td></tr>`;
    }
  } catch (err) {
    showToast("Error cargando el resumen: " + err.message);
  }
  loadLiquidity();
  refreshUnreadCount();
}

function renderBarList(containerId, items) {
  const container = document.getElementById(containerId);
  container.innerHTML = "";
  if (!items || items.length === 0) {
    container.innerHTML = `<div style="color:#64748b;font-size:0.84rem;">Todavía no hay facturas registradas.</div>`;
    return;
  }
  const maxAbs = Math.max(...items.map((i) => Math.abs(Number(i.total || 0))), 1);
  items.forEach((item) => {
    const pct = Math.min(100, Math.round((Math.abs(Number(item.total || 0)) / maxAbs) * 100));
    const div = document.createElement("div");
    div.innerHTML = `
      <div class="bar-item-head">
        <span><strong>${escapeHtml(item.label)}</strong> <small style="color:#64748b;">(${item.count})</small></span>
        <span><strong>${fmtEur(item.total)}</strong></span>
      </div>
      <div class="bar-track"><div class="bar-fill" style="width:${pct}%;"></div></div>
    `;
    container.appendChild(div);
  });
}

function populateCategorySelects() {
  const selects = [
    document.getElementById("filter-categoria"),
    document.getElementById("edit-categoria"),
    document.getElementById("rule-categoria"),
  ];
  selects.forEach((sel, idx) => {
    if (!sel) return;
    const currentVal = sel.value;
    const firstOpt =
      idx === 0
        ? `<option value="">Todas las categorías</option>`
        : idx === 2
        ? `<option value="">(Dejar la que detecte)</option>`
        : "";
    sel.innerHTML =
      firstOpt +
      state.categorias
        .map((c) => `<option value="${escapeHtml(c.codigo)}">${escapeHtml(c.nombre)}</option>`)
        .join("");
    if (currentVal) sel.value = currentVal;
  });

  const docSel = document.getElementById("edit-tipo-documento");
  if (docSel) {
    const cur = docSel.value;
    docSel.innerHTML = Object.entries(state.tiposDocumento)
      .map(([k, v]) => `<option value="${escapeHtml(k)}">${escapeHtml(v)}</option>`)
      .join("");
    if (cur) docSel.value = cur;
  }
}

/* =========================================================
   2. Bandeja de Revisión
   ========================================================= */
async function loadReviewInbox() {
  try {
    const ps = pageState.revision;
    const data = await apiFetch(
      `/api/invoices?estado_revision=pendiente_revision&limit=${ps.limit}&offset=${ps.offset}`
    );
    renderPager("pager-revision", "revision", data.total || 0, loadReviewInbox);
    const tbody = document.getElementById("tbody-revision");
    tbody.innerHTML = "";

    (data.items || []).forEach((inv) => {
      const confPct = Math.round(Number(inv.nivel_confianza || 0) * 100);
      const motivos =
        (inv.errores_validacion || []).join(" · ") ||
        `Lectura con seguridad del ${confPct}% (requiere confirmación)`;
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${escapeHtml(inv.fecha_emision || "-")}</td>
        <td><strong>${escapeHtml(inv.proveedor_nombre || "Sin proveedor")}</strong>${docTypeBadge(inv)}</td>
        <td>${escapeHtml(inv.nif || "-")}</td>
        <td>${escapeHtml(inv.numero_factura || "-")}</td>
        <td>${escapeHtml(categoryName(inv.categoria))}</td>
        <td class="num">${fmtEur(inv.base_imponible)}</td>
        <td class="num">${fmtEur(inv.iva)}</td>
        <td class="num">${fmtEur(inv.total)}</td>
        <td>
          <span class="badge badge-warning">Revisar</span>
          <div style="font-size:0.76rem;color:#92400e;margin-top:0.2rem;">${escapeHtml(motivos)}</div>
        </td>
        <td style="white-space:nowrap;">
          <button class="btn btn-primary btn-sm" onclick="openInvoiceModal(${inv.id})">Corregir / Ver PDF</button>
          <button class="btn btn-success btn-sm" onclick="quickConfirmInvoice(${inv.id})">Dar por buena</button>
        </td>
      `;
      tbody.appendChild(tr);
    });

    if (!data.items || data.items.length === 0) {
      tbody.innerHTML = `<tr><td colspan="10" style="text-align:center;color:#64748b;padding:1.5rem;">¡Todo al día! No tienes ninguna factura pendiente de revisar.</td></tr>`;
    }
  } catch (err) {
    showToast("Error cargando bandeja de revisión: " + err.message);
  }
}

async function quickConfirmInvoice(invoiceId) {
  try {
    await apiFetch(`/api/invoices/${invoiceId}/confirm`, { method: "POST" });
    showToast("Factura confirmada correctamente.");
    loadReviewInbox();
    loadDashboard();
  } catch (err) {
    showToast("Error confirmando factura: " + err.message);
  }
}

/* =========================================================
   3. Todas las Facturas
   ========================================================= */
async function loadInvoicesList(resetPage) {
  if (resetPage === true) pageState.facturas.offset = 0;
  try {
    const params = new URLSearchParams();
    const q = document.getElementById("filter-busqueda").value.trim();
    const mes = document.getElementById("filter-mes").value.trim();
    const cat = document.getElementById("filter-categoria").value;
    const rev = document.getElementById("filter-estado-revision").value;
    const pago = document.getElementById("filter-estado-pago").value;

    if (q) params.set("busqueda", q);
    if (mes) params.set("mes", mes);
    if (cat) params.set("categoria", cat);
    if (rev) params.set("estado_revision", rev);
    if (pago) params.set("estado_pago", pago);
    if (document.getElementById("filter-incluir-sustituidas").checked) params.set("incluir_sustituidas", "true");
    const tipo = document.getElementById("filter-tipo-documento").value;
    if (tipo) params.set("tipo_documento", tipo);
    params.set("limit", String(pageState.facturas.limit));
    params.set("offset", String(pageState.facturas.offset));

    const data = await apiFetch(`/api/invoices?${params.toString()}`);
    renderPager("pager-facturas", "facturas", data.total || 0, loadInvoicesList);
    const tbody = document.getElementById("tbody-facturas");
    tbody.innerHTML = "";

    (data.items || []).forEach((inv) => {
      let revBadge =
        inv.estado_revision === "confirmada"
          ? `<span class="badge badge-success">Confirmada</span>`
          : `<span class="badge badge-warning">Por revisar</span>`;
      if (inv.sustituida_por) {
        revBadge += ` <span class="badge badge-info">Sustituida por #${escapeHtml(inv.sustituida_por)}</span>`;
      }
      if (inv.sustituye_a) {
        revBadge += ` <span class="badge badge-info">Sustituye a #${escapeHtml(inv.sustituye_a)}</span>`;
      }

      let pagoBadge = `<span class="badge badge-danger">Pendiente</span>`;
      if (inv.tipo_documento === "albaran") pagoBadge = `<span class="badge badge-neutral">No se paga</span>`;
      else if (inv.estado_pago === "pagada") pagoBadge = `<span class="badge badge-success">Pagada</span>`;
      else if (inv.estado_pago === "parcial") pagoBadge = `<span class="badge badge-info">Pago parcial</span>`;

      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${escapeHtml(inv.fecha_emision || "-")}</td>
        <td>${escapeHtml(inv.fecha_vencimiento || "-")}</td>
        <td><strong>${escapeHtml(inv.proveedor_nombre)}</strong>${docTypeBadge(inv)}</td>
        <td>${escapeHtml(inv.nif)}</td>
        <td>${escapeHtml(inv.numero_factura)}</td>
        <td>${escapeHtml(categoryName(inv.categoria))}</td>
        <td class="num">${fmtEur(inv.base_imponible)}</td>
        <td class="num">${fmtEur(inv.iva)}</td>
        <td class="num">${fmtEur(inv.total)}</td>
        <td>${revBadge}</td>
        <td>${pagoBadge}</td>
        <td style="white-space:nowrap;">
          <button class="btn btn-secondary btn-sm" onclick="openPdfFile(${inv.id})">Ver PDF</button>
          <button class="btn btn-secondary btn-sm" onclick="openInvoiceModal(${inv.id})">Editar</button>
        </td>
      `;
      tbody.appendChild(tr);
    });

    if (!data.items || data.items.length === 0) {
      tbody.innerHTML = `<tr><td colspan="12" style="text-align:center;color:#64748b;padding:1.5rem;">No se encontraron facturas con esos criterios de búsqueda.</td></tr>`;
    }
  } catch (err) {
    showToast("Error listando facturas: " + err.message);
  }
}

async function openPdfFile(invoiceId) {
  try {
    const resp = await fetch(`/api/invoices/${invoiceId}/pdf`, { method: "HEAD" });
    if (!resp.ok) {
      const getResp = await fetch(`/api/invoices/${invoiceId}/pdf`);
      const errData = await getResp.json();
      alert(errData.detail || "No se encontró el archivo PDF en la carpeta archivada.");
      return;
    }
    window.open(`/api/invoices/${invoiceId}/pdf`, "_blank");
  } catch (err) {
    showToast("No se pudo abrir el PDF: " + err.message);
  }
}

/* =========================================================
   Modal de Revisión y Edición con Comprobación de Suma en Vivo
   ========================================================= */
async function openInvoiceModal(invoiceId) {
  try {
    const inv = await apiFetch(`/api/invoices/${invoiceId}`);
    state.currentEditingInvoice = inv;

    document.getElementById("edit-invoice-id").value = inv.id;
    document.getElementById("edit-proveedor").value = inv.proveedor_nombre || "";
    document.getElementById("edit-nif").value = inv.nif || "";
    document.getElementById("edit-numero").value = inv.numero_factura || "";
    document.getElementById("edit-fecha-emision").value = (inv.fecha_emision || "").slice(0, 10);
    document.getElementById("edit-fecha-vencimiento").value = (inv.fecha_vencimiento || "").slice(0, 10);
    document.getElementById("edit-categoria").value = inv.categoria || "compras_mercancia";
    document.getElementById("edit-tipo-documento").value = inv.tipo_documento || "factura_mercancia";
    document.getElementById("edit-base").value = Number(inv.base_imponible || 0).toFixed(2);
    document.getElementById("edit-iva").value = Number(inv.iva || 0).toFixed(2);

    const extra = inv.datos_extra || {};
    document.getElementById("edit-recargo").value = Number(extra.recargo_equivalencia || 0).toFixed(2);
    document.getElementById("edit-irpf").value = Number(extra.irpf || 0).toFixed(2);
    document.getElementById("edit-total").value = Number(inv.total || 0).toFixed(2);
    document.getElementById("edit-notas").value = inv.notas || "";

    const alertsBox = document.getElementById("modal-validation-alerts");
    const errs = inv.errores_validacion || [];
    let replaceHtml = "";
    if (inv.sustituye_a) {
      replaceHtml += `<div class="alert alert-info">Sustituye a la factura #${escapeHtml(inv.sustituye_a)} (mismo proveedor y número).</div>`;
    }
    if (inv.sustituida_por) {
      replaceHtml += `<div class="alert alert-info">Esta factura ha sido sustituida por la #${escapeHtml(inv.sustituida_por)} y se conserva solo como historial.</div>`;
    }
    if (errs.length > 0) {
      alertsBox.innerHTML = replaceHtml + `<div class="alert alert-warning"><strong>Puntos a revisar en esta factura:</strong><br/>${errs.map(escapeHtml).join("<br/>")}</div>`;
    } else {
      alertsBox.innerHTML = replaceHtml;
    }

    const pdfUrl = `/api/invoices/${inv.id}/pdf`;
    document.getElementById("modal-open-pdf-newtab").href = pdfUrl;
    document.getElementById("modal-pdf-frame").src = inv.archivo_disponible ? pdfUrl : "about:blank";

    updateLiveMathCheck();
    state.modalReturnFocus = document.activeElement;
    document.getElementById("invoice-modal").classList.add("open");
    const firstField = document.getElementById("edit-proveedor");
    if (firstField) firstField.focus();
  } catch (err) {
    showToast("Error cargando factura: " + err.message);
  }
}

function updateLiveMathCheck() {
  const base = parseFloat(document.getElementById("edit-base").value || "0");
  const iva = parseFloat(document.getElementById("edit-iva").value || "0");
  const rec = parseFloat(document.getElementById("edit-recargo").value || "0");
  const irpf = parseFloat(document.getElementById("edit-irpf").value || "0");
  const total = parseFloat(document.getElementById("edit-total").value || "0");

  const expected = Math.round((base + iva + rec - irpf) * 100) / 100;
  const diff = Math.abs(expected - total);
  const box = document.getElementById("edit-math-check");

  if (diff <= 0.05) {
    box.style.color = "var(--success)";
    box.textContent = `✓ Los importes cuadran: Base (${fmtEur(base)}) + IVA (${fmtEur(iva)}) + R.E. (${fmtEur(rec)}) - IRPF (${fmtEur(irpf)}) = ${fmtEur(expected)}`;
  } else {
    box.style.color = "var(--danger)";
    box.textContent = `⚠ Los importes no cuadran por ${fmtEur(diff)}: la suma da ${fmtEur(expected)} pero en Total figura ${fmtEur(total)}`;
  }
}

/* =========================================================
   4. Cuadrar con el Banco (Conciliación Bancaria)
   ========================================================= */
async function loadBankReconciliation() {
  try {
    const statusFilter = document.getElementById("filter-bank-status").value;
    const ps = pageState.bank;
    const params = new URLSearchParams({ limit: String(ps.limit), offset: String(ps.offset) });
    if (statusFilter) params.set("estado_conciliacion", statusFilter);
    const [txData, invData] = await Promise.all([
      apiFetch(`/api/bank/transactions?${params.toString()}`),
      apiFetch("/api/invoices?tipo_documento=_sin_albaranes&limit=1000"),
    ]);
    renderPager("pager-bank", "bank", txData.total || 0, loadBankReconciliation);

    state.unpaidInvoices = (invData.items || []).filter((i) => i.estado_pago !== "pagada");
    renderManualCandidateInvoices();

    const tbody = document.getElementById("tbody-bank-transactions");
    tbody.innerHTML = "";
    state.bankTransactionsById = {};

    (txData.items || []).forEach((tx) => {
      state.bankTransactionsById[tx.id] = tx;
      let badge = `<span class="badge badge-danger">Sin factura asignada</span>`;
      if (tx.estado_conciliacion === "conciliado") badge = `<span class="badge badge-success">Cuadrado con factura</span>`;
      else if (tx.estado_conciliacion === "propuesto") badge = `<span class="badge badge-warning">Coincidencia encontrada</span>`;

      const links = tx.vinculos || [];
      const linksHtml =
        links.length > 0
          ? links
              .map(
                (l) =>
                  `<div><strong>${escapeHtml(l.proveedor_nombre)}</strong> · Fra. ${escapeHtml(l.numero_factura)} (${fmtEur(l.importe_asignado)})</div>`
              )
              .join("")
          : `<span style="color:#64748b;">Ninguna factura unida aún</span>`;

      let actionsHtml = "";
      if (tx.estado_conciliacion === "propuesto") {
        actionsHtml = `
          <button class="btn btn-success btn-sm" onclick="confirmBankProposal(${tx.id})">Confirmar</button>
          <button class="btn btn-danger btn-sm" onclick="rejectBankProposal(${tx.id})">No es esta</button>
        `;
      } else if (tx.estado_conciliacion === "conciliado") {
        actionsHtml = `
          <button class="btn btn-secondary btn-sm" onclick="rejectBankProposal(${tx.id})">Deshacer unión</button>
        `;
      } else {
        actionsHtml = `
          <button class="btn btn-primary btn-sm" onclick="selectTxForManualById(${Number(tx.id)})">Unir a mano</button>
        `;
      }

      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${escapeHtml(tx.fecha)}</td>
        <td><strong>${escapeHtml(tx.banco)}</strong><br/><small style="color:#64748b;">${escapeHtml(tx.cuenta_iban || "")}</small></td>
        <td>${escapeHtml(tx.concepto)}</td>
        <td class="num" style="color:${tx.importe < 0 ? "var(--danger)" : "var(--success)"};">${fmtEur(tx.importe)}</td>
        <td>${badge}</td>
        <td>${linksHtml}</td>
        <td class="num">${tx.diferencia_pendiente ? fmtEur(tx.diferencia_pendiente) : "-"}</td>
        <td style="white-space:nowrap;">${actionsHtml}</td>
      `;
      tbody.appendChild(tr);
    });

    if (!txData.items || txData.items.length === 0) {
      tbody.innerHTML = `<tr><td colspan="8" style="text-align:center;color:#64748b;padding:1.5rem;">Todavía no has subido ningún archivo de movimientos del banco.</td></tr>`;
    }
  } catch (err) {
    showToast("Error cargando movimientos del banco: " + err.message);
  }
}

function selectTxForManualById(id) {
  const tx = state.bankTransactionsById[id];
  if (tx) selectTxForManual(tx);
}

function selectTxForManual(tx) {
  state.selectedBankTx = tx;
  document.getElementById("manual-selected-tx-label").textContent =
    `Movimiento seleccionado (${tx.fecha}) · ${tx.concepto} · Importe: ${fmtEur(tx.importe)}`;
  document.getElementById("btn-save-manual-reconcile").disabled = false;
  renderManualCandidateInvoices();
  recalculateManualSummary();
}

function renderManualCandidateInvoices() {
  const tbody = document.getElementById("tbody-manual-candidate-invoices");
  tbody.innerHTML = "";
  state.unpaidInvoices.forEach((inv) => {
    const pendingVal = Number(inv.importe_pendiente ?? inv.total ?? 0).toFixed(2);
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><input type="checkbox" class="manual-inv-check" data-id="${inv.id}" onchange="recalculateManualSummary()" /></td>
      <td><strong>${escapeHtml(inv.proveedor_nombre)}</strong> · <small>${escapeHtml(inv.numero_factura)}</small></td>
      <td>${escapeHtml(inv.fecha_vencimiento || inv.fecha_emision || "-")}</td>
      <td class="num">${fmtEur(pendingVal)}</td>
      <td class="num">
        <input type="number" step="0.01" class="manual-inv-amount" data-id="${inv.id}" value="${pendingVal}" style="width:95px;text-align:right;" oninput="recalculateManualSummary()" />
      </td>
    `;
    tbody.appendChild(tr);
  });
}

function recalculateManualSummary() {
  const tx = state.selectedBankTx;
  const txAmt = tx ? Number(tx.importe || 0) : 0;
  const expectedInvoicesNet = -txAmt;

  let sumAssigned = 0;
  document.querySelectorAll(".manual-inv-check:checked").forEach((chk) => {
    const id = chk.getAttribute("data-id");
    const amtInput = document.querySelector(`.manual-inv-amount[data-id="${id}"]`);
    sumAssigned += parseFloat(amtInput ? amtInput.value : "0") || 0;
  });

  const diff = Math.round((expectedInvoicesNet - sumAssigned) * 100) / 100;
  document.getElementById("manual-sum-summary").innerHTML =
    `Cargo en banco: <strong>${fmtEur(txAmt)}</strong> · Suma de facturas marcadas: <strong>${fmtEur(sumAssigned)}</strong> · Diferencia: <strong style="color:${Math.abs(diff) <= 0.05 ? "var(--success)" : "var(--warning)"};">${fmtEur(diff)}</strong>`;
}

async function confirmBankProposal(movId) {
  try {
    await apiFetch(`/api/bank/transactions/${movId}/confirm-proposal`, { method: "POST" });
    showToast("Pago confirmado y factura marcada como pagada.");
    loadBankReconciliation();
    loadDashboard();
  } catch (err) {
    showToast("Error: " + err.message);
  }
}

async function rejectBankProposal(movId) {
  try {
    await apiFetch(`/api/bank/transactions/${movId}/reject-proposal`, { method: "POST" });
    showToast("Unión deshecha; el movimiento vuelve a quedar pendiente.");
    loadBankReconciliation();
    loadDashboard();
  } catch (err) {
    showToast("Error: " + err.message);
  }
}

/* =========================================================
   5. Configuración Fácil
   ========================================================= */
const CUSTOM_MODEL_VALUE = "__otro__";

const PROVIDER_GUIDES = {
  google_ai_studio: `
    <strong>Cómo configurar Google Gemini (gratis):</strong>
    <ol style="padding-left:1.2rem; margin:0.3rem 0;">
      <li>Abre <strong>aistudio.google.com/apikey</strong> e inicia sesión con una cuenta de Google.</li>
      <li>Pulsa <strong>"Create API key"</strong> y copia la clave (empieza por <code>AIza...</code>).</li>
      <li>Pégala en <em>Clave de conexión</em>, deja el modelo <strong>Gemini Flash-Lite (recomendado)</strong> y pulsa <strong>Probar Clave</strong>.</li>
      <li>Si sale bien, pulsa <strong>Guardar Opciones</strong>.</li>
    </ol>
    <span>El modelo recomendado usa el alias <code>-latest</code>: Google lo actualiza solo a su versión más reciente, así el programa no deja de funcionar cuando retiran un modelo antiguo. Flash-Lite es el que más lecturas gratuitas diarias permite; si alguna vez ves "límite de usos superado", espera unas horas o activa el plan de pago.</span>`,
  openai_compatible: `
    <strong>Cómo configurar OpenAI:</strong>
    <ol style="padding-left:1.2rem; margin:0.3rem 0;">
      <li>Entra en <strong>platform.openai.com/api-keys</strong>, carga saldo en <em>Settings → Billing</em> (5 € suelen durar mucho).</li>
      <li>Pulsa <strong>"Create new secret key"</strong>, copia la clave (<code>sk-...</code>) y pégala arriba.</li>
      <li>Deja vacía la <em>Dirección del servicio</em>, elige el modelo y pulsa <strong>Probar Clave</strong>.</li>
    </ol>
    <span><strong>IA local (Ollama):</strong> no envía las facturas fuera del ordenador. Escribe la dirección <code>http://localhost:11434/v1</code>, elige "Otro modelo" y pon el nombre del modelo instalado. No necesita clave. Las IA locales no leen PDFs escaneados como imagen.</span>`,
  anthropic: `
    <strong>Cómo configurar Anthropic Claude:</strong>
    <ol style="padding-left:1.2rem; margin:0.3rem 0;">
      <li>Entra en <strong>console.anthropic.com/settings/keys</strong> y añade saldo en <em>Billing</em>.</li>
      <li>Pulsa <strong>"Create Key"</strong>, copia la clave (<code>sk-ant-...</code>) y pégala arriba.</li>
      <li>Elige el modelo (Haiku es el más económico) y pulsa <strong>Probar Clave</strong>.</li>
    </ol>`,
  heuristico_local: `
    <strong>Modo básico sin Internet:</strong> no necesita clave ni envía nada fuera del ordenador.
    Lee bien las facturas con texto digital y formato habitual, pero no las escaneadas como foto; esas quedarán en la <em>Bandeja de Revisión</em>.`,
};

function renderModelOptions(provider, currentModel) {
  const info = (state.llmCatalog || {})[provider] || { modelos: [], modelo_defecto: "" };
  const select = document.getElementById("cfg-modelo-select");
  const custom = document.getElementById("cfg-modelo-custom");
  const group = document.getElementById("cfg-modelo-group");
  const endpointGroup = document.getElementById("cfg-endpoint-group");

  group.style.display = provider === "heuristico_local" ? "none" : "";
  endpointGroup.style.display = provider === "openai_compatible" ? "" : "none";
  document.getElementById("cfg-provider-guide").innerHTML = PROVIDER_GUIDES[provider] || "";

  select.innerHTML = "";
  (info.modelos || []).forEach((m) => {
    const opt = document.createElement("option");
    opt.value = m.id;
    opt.textContent = m.nombre;
    select.appendChild(opt);
  });
  const otherOpt = document.createElement("option");
  otherOpt.value = CUSTOM_MODEL_VALUE;
  otherOpt.textContent = "Otro modelo (escribir el nombre a mano)";
  select.appendChild(otherOpt);

  const wanted = currentModel || info.modelo_defecto;
  const known = (info.modelos || []).some((m) => m.id === wanted);
  if (known) {
    select.value = wanted;
    custom.value = "";
  } else if (wanted) {
    select.value = CUSTOM_MODEL_VALUE;
    custom.value = wanted;
  } else {
    select.value = CUSTOM_MODEL_VALUE;
    custom.value = "";
  }
  updateModelHint();
}

function updateModelHint() {
  const provider = document.getElementById("cfg-proveedor-modelo").value;
  const info = (state.llmCatalog || {})[provider] || { modelos: [] };
  const selected = document.getElementById("cfg-modelo-select").value;
  const custom = document.getElementById("cfg-modelo-custom");
  const hint = document.getElementById("cfg-modelo-hint");
  if (selected === CUSTOM_MODEL_VALUE) {
    custom.style.display = "";
    hint.textContent =
      "Escribe el identificador exacto que indica el proveedor. Si lo dejas vacío se usará el recomendado.";
  } else {
    custom.style.display = "none";
    const m = (info.modelos || []).find((x) => x.id === selected);
    hint.textContent = m ? `${m.descripcion} (identificador: ${m.id})` : "";
  }
}

function getSelectedModel() {
  const selected = document.getElementById("cfg-modelo-select").value;
  if (selected === CUSTOM_MODEL_VALUE) {
    return document.getElementById("cfg-modelo-custom").value.trim();
  }
  return selected || "";
}

async function loadConfiguration() {
  try {
    const [settings, mailData, rulesData] = await Promise.all([
      apiFetch("/api/settings"),
      apiFetch("/api/mail/accounts"),
      apiFetch("/api/rules"),
    ]);

    state.llmCatalog = settings.catalogo_proveedores || {};
    document.getElementById("cfg-proveedor-modelo").value = settings.proveedor_modelo || "google_ai_studio";
    document.getElementById("cfg-llm-endpoint").value = settings.llm_endpoint_url || "";
    renderModelOptions(settings.proveedor_modelo || "google_ai_studio", settings.modelo_llm || "");
    document.getElementById("cfg-estrategia-ocr").value = settings.estrategia_ocr || "vision_llm";
    document.getElementById("cfg-umbral-confianza").value = settings.umbral_confianza ?? 0.85;
    document.getElementById("cfg-umbral-val").textContent = Math.round((settings.umbral_confianza ?? 0.85) * 100) + "%";
    document.getElementById("cfg-capa-almacenamiento").value = settings.capa_almacenamiento || "local";
    document.getElementById("cfg-carpeta-salida").value = settings.carpeta_salida || "";
    document.getElementById("cfg-carpeta-datos-actual").textContent = settings.carpeta_datos || "-";

    const keyConfigured = settings.estado_secretos && settings.estado_secretos.llm_api_key_configurada;
    document.getElementById("cfg-key-status").textContent = keyConfigured
      ? "✓ Tu clave ya está guardada de forma segura en este ordenador."
      : "Aún no has puesto ninguna clave externa (el programa funcionará usando el lector básico del ordenador).";

    const tbodyMail = document.getElementById("tbody-mail-accounts");
    tbodyMail.innerHTML = "";
    (mailData.items || []).forEach((acc) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td><strong>${escapeHtml(acc.nombre)}</strong><br/><small style="color:#64748b;">${escapeHtml(MAIL_TYPE_LABELS[acc.tipo_conexion] || acc.tipo_conexion)}</small></td>
        <td>${escapeHtml(acc.email)}</td>
        <td>${acc.credencial_configurada ? '<span class="badge badge-success">Guardada</span>' : '<span class="badge badge-warning">Falta ponerla</span>'}</td>
        <td><small>${escapeHtml(acc.ultima_sincronizacion_ok ? fmtDateTime(acc.ultima_sincronizacion_ok) : acc.ultima_sincronizacion ? fmtDateTime(acc.ultima_sincronizacion) : "Aún no revisado")}</small></td>
        <td>${
          acc.ultimo_error
            ? `<small style="color:#991b1b;">${escapeHtml(acc.ultimo_error)}</small>`
            : '<small style="color:#64748b;">Ninguno</small>'
        }${
          Number(acc.mensajes_con_error || 0) > 0
            ? `<div><small style="color:#92400e;">${Number(acc.mensajes_con_error)} correo(s) sin procesar</small></div>`
            : ""
        }</td>
        <td style="white-space:nowrap;">
          ${
            Number(acc.mensajes_con_error || 0) > 0 || acc.ultimo_error
              ? `<button type="button" class="btn btn-secondary btn-sm" onclick="retryMailErrors(${Number(acc.id)}, this)">Reintentar correos con error</button>`
              : ""
          }
          <button type="button" class="btn btn-danger btn-sm" aria-label="Quitar el buzón ${escapeHtml(acc.email)}" onclick="deleteMailAccount(${Number(acc.id)}, ${escapeHtml(JSON.stringify(acc.email || ""))})">Quitar</button>
        </td>
      `;
      tbodyMail.appendChild(tr);
    });
    if (!mailData.items || mailData.items.length === 0) {
      tbodyMail.innerHTML = `<tr><td colspan="6" style="text-align:center;color:#64748b;">No hay ningún buzón de correo añadido todavía.</td></tr>`;
    }

    document.getElementById("cfg-plazo-defecto").value = settings.plazo_pago_defecto_dias ?? 30;
    loadPaymentTerms();

    const tbodyRules = document.getElementById("tbody-rules");
    tbodyRules.innerHTML = "";
    (rulesData.items || []).forEach((r) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${escapeHtml(RULE_TYPE_LABELS[r.tipo_regla] || r.tipo_regla)}</td>
        <td><strong>${escapeHtml(r.patron)}</strong></td>
        <td>${escapeHtml(r.proveedor_nombre || "-")}</td>
        <td>${escapeHtml(categoryName(r.categoria))}</td>
        <td>${r.aprendida ? '<span class="badge badge-info">Automática</span>' : '<span class="badge badge-success">Puesta por ti</span>'}</td>
        <td><button type="button" class="btn btn-danger btn-sm" aria-label="Quitar la regla ${escapeHtml(r.patron)}" onclick="deleteRule(${Number(r.id)})">Quitar</button></td>
      `;
      tbodyRules.appendChild(tr);
    });
    if (!rulesData.items || rulesData.items.length === 0) {
      tbodyRules.innerHTML = `<tr><td colspan="6" style="text-align:center;color:#64748b;">Todavía no hay reglas creadas.</td></tr>`;
    }
  } catch (err) {
    showToast("Error cargando opciones: " + err.message);
  }
}

async function deleteMailAccount(id, email) {
  const quien = email ? ` (${email})` : "";
  if (
    !confirm(
      `¿Quitar este buzón de correo${quien}? El programa dejará de revisarlo y se borrará su contraseña guardada. Las facturas ya archivadas no se tocan.`
    )
  )
    return;
  try {
    await apiFetch(`/api/mail/accounts/${id}`, { method: "DELETE" });
    showToast("Cuenta de correo eliminada.");
    loadConfiguration();
  } catch (err) {
    showToast("No se pudo quitar el buzón: " + err.message);
  }
}

async function retryMailErrors(id, btn) {
  await withBusyButton(btn, async () => {
    try {
      showToast("Volviendo a intentar los correos con error...");
      const res = await apiFetch(`/api/mail/accounts/${id}/retry-errors`, { method: "POST" });
      const sync = res.sincronizacion || {};
      showToast(
        sync.error
          ? "No se pudo conectar con el correo: " + sync.error
          : `Reintento terminado: ${Number(sync.facturas_insertadas || 0)} factura(s) nueva(s).`
      );
      loadConfiguration();
      loadDashboard();
      if (typeof loadHealth === "function") loadHealth();
    } catch (err) {
      showToast("No se pudo reintentar: " + err.message);
    }
  });
}

async function deleteRule(id) {
  try {
    await apiFetch(`/api/rules/${id}`, { method: "DELETE" });
    showToast("Regla eliminada.");
    loadConfiguration();
  } catch (err) {
    showToast("No se pudo quitar la regla: " + err.message);
  }
}

function closeInvoiceModal() {
  const modal = document.getElementById("invoice-modal");
  if (!modal.classList.contains("open")) return;
  modal.classList.remove("open");
  const back = state.modalReturnFocus;
  state.modalReturnFocus = null;
  if (back && typeof back.focus === "function") back.focus();
}

function escapeHtml(str) {
  return String(str || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/* =========================================================
   Inicialización de eventos
   ========================================================= */
document.addEventListener("DOMContentLoaded", () => {
  setupTabs();
  loadDashboard();

  // Subida rápida de PDFs
  document.getElementById("quick-upload-input").addEventListener("change", async (e) => {
    const files = e.target.files;
    if (!files || files.length === 0) return;
    const fd = new FormData();
    for (const f of files) fd.append("files", f);
    try {
      showToast(`Leyendo y archivando ${files.length} factura(s) PDF...`);
      const res = await apiFetch("/api/invoices/upload", { method: "POST", body: fd });
      showToast(`Listo: ${res.insertadas} factura(s) nueva(s), ${res.duplicadas} ya estaban guardadas.`);
      (res.resultados || [])
        .filter((r) => r.status === "error")
        .forEach((r) => showToast(`${r.filename}: ${r.detail || "no se pudo procesar"}`));
      loadDashboard();
      loadInvoicesList();
      loadReviewInbox();
    } catch (err) {
      showToast("Error al añadir PDF: " + err.message);
    } finally {
      e.target.value = "";
    }
  });

  // Botón cargar datos de ejemplo
  document.getElementById("btn-load-demo").addEventListener("click", (ev) => {
    if (
      !confirm(
        "Se añadirán 7 facturas de ejemplo y varios movimientos del banco a tu base de datos real (marcados como ejemplo).\n\nCuando termines de probar, bórralos con «Eliminar datos de ejemplo». ¿Continuar?"
      )
    )
      return;
    withBusyButton(ev.currentTarget, async () => {
      try {
        showToast("Cargando facturas y movimientos bancarios de ejemplo...");
        const res = await apiFetch("/api/demo/load-sample-data", { method: "POST" });
        showToast(
          `Datos de ejemplo listos: ${res.facturas_insertadas} facturas y ${res.movimientos_insertados} movimientos del banco.`
        );
        loadDashboard();
        loadReviewInbox();
        loadInvoicesList();
        loadBankReconciliation();
      } catch (err) {
        showToast("Error cargando datos de ejemplo: " + err.message);
      }
    });
  });

  document.getElementById("btn-delete-demo").addEventListener("click", (ev) => {
    if (
      !confirm(
        "Se borrarán las facturas de ejemplo, sus PDF archivados y los movimientos del banco de ejemplo. Tus datos reales no se tocan. ¿Continuar?"
      )
    )
      return;
    withBusyButton(ev.currentTarget, async () => {
      try {
        const res = await apiFetch("/api/demo/sample-data", { method: "DELETE" });
        showToast(
          `Datos de ejemplo eliminados: ${Number(res.facturas_borradas || 0)} factura(s) y ${Number(res.movimientos_borrados || 0)} movimiento(s).`
        );
        loadDashboard();
        loadReviewInbox();
        loadInvoicesList();
        loadBankReconciliation();
      } catch (err) {
        showToast("No se pudieron borrar los datos de ejemplo: " + err.message);
      }
    });
  });

  // Botón revisar correo ahora
  document.getElementById("btn-sync-mail").addEventListener("click", (ev) => withBusyButton(ev.currentTarget, async () => {
    try {
      showToast("Buscando facturas nuevas en tu correo...");
      const res = await apiFetch("/api/mail/sync", { method: "POST" });
      const total = (res.sincronizaciones || []).reduce((acc, s) => acc + (s.facturas_insertadas || 0), 0);
      showToast(`Revisión terminada: ${total} factura(s) nueva(s) encontrada(s).`);
      loadDashboard();
    } catch (err) {
      showToast("Error revisando el correo: " + err.message);
    }
  }));

  // Filtros de facturas
  document.getElementById("btn-apply-filters").addEventListener("click", () => loadInvoicesList(true));
  document.getElementById("filter-tipo-documento").addEventListener("change", () => loadInvoicesList(true));
  document.getElementById("btn-clear-filters").addEventListener("click", () => {
    document.getElementById("filter-busqueda").value = "";
    document.getElementById("filter-mes").value = "";
    document.getElementById("filter-categoria").value = "";
    document.getElementById("filter-estado-revision").value = "";
    document.getElementById("filter-estado-pago").value = "";
    document.getElementById("filter-incluir-sustituidas").checked = false;
    document.getElementById("filter-tipo-documento").value = "_sin_albaranes";
    loadInvoicesList(true);
  });
  document.getElementById("btn-refresh-revision").addEventListener("click", loadReviewInbox);

  // Modal de revisión/edición
  document.getElementById("btn-close-modal").addEventListener("click", closeInvoiceModal);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && document.getElementById("invoice-modal").classList.contains("open")) {
      closeInvoiceModal();
    }
  });
  ["edit-base", "edit-iva", "edit-recargo", "edit-irpf", "edit-total"].forEach((id) => {
    document.getElementById(id).addEventListener("input", updateLiveMathCheck);
  });

  onSubmitBusy("form-edit-invoice", async (e) => {
    const id = document.getElementById("edit-invoice-id").value;
    const currentExtra = (state.currentEditingInvoice && state.currentEditingInvoice.datos_extra) || {};
    const payload = {
      proveedor_nombre: document.getElementById("edit-proveedor").value.trim(),
      nif: document.getElementById("edit-nif").value.trim(),
      numero_factura: document.getElementById("edit-numero").value.trim(),
      fecha_emision: document.getElementById("edit-fecha-emision").value || null,
      fecha_vencimiento: document.getElementById("edit-fecha-vencimiento").value || null,
      categoria: document.getElementById("edit-categoria").value,
      tipo_documento: document.getElementById("edit-tipo-documento").value,
      base_imponible: parseFloat(document.getElementById("edit-base").value || "0"),
      iva: parseFloat(document.getElementById("edit-iva").value || "0"),
      total: parseFloat(document.getElementById("edit-total").value || "0"),
      notas: document.getElementById("edit-notas").value.trim(),
      estado_revision: "confirmada",
      aprender_regla_proveedor: document.getElementById("edit-learn-rule").checked,
      datos_extra: {
        ...currentExtra,
        recargo_equivalencia: parseFloat(document.getElementById("edit-recargo").value || "0"),
        irpf: parseFloat(document.getElementById("edit-irpf").value || "0"),
      },
    };
    try {
      await apiFetch(`/api/invoices/${id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      showToast("Factura confirmada y guardada.");
      closeInvoiceModal();
      loadReviewInbox();
      loadInvoicesList();
      loadDashboard();
    } catch (err) {
      showToast("Error guardando factura: " + err.message);
    }
  });

  document.getElementById("btn-delete-invoice").addEventListener("click", async () => {
    const id = document.getElementById("edit-invoice-id").value;
    if (!id || !confirm("¿Seguro que quieres borrar esta factura del programa?")) return;
    try {
      await apiFetch(`/api/invoices/${id}`, { method: "DELETE" });
      closeInvoiceModal();
      showToast("Factura borrada.");
      loadReviewInbox();
      loadInvoicesList();
      loadDashboard();
    } catch (err) {
      showToast("No se pudo borrar la factura: " + err.message);
    }
  });

  // Conciliación bancaria
  document.getElementById("filter-bank-status").addEventListener("change", () => {
    pageState.bank.offset = 0;
    loadBankReconciliation();
  });
  onSubmitBusy("form-bank-import", async (e) => {
    const fileInput = document.getElementById("bank-import-file");
    if (!fileInput.files || fileInput.files.length === 0) return;

    const fd = new FormData();
    fd.append("file", fileInput.files[0]);
    fd.append("banco", document.getElementById("bank-import-name").value.trim());
    fd.append("formato", document.getElementById("bank-import-format").value);

    const colFecha = document.getElementById("csv-col-fecha").value.trim();
    const colConcepto = document.getElementById("csv-col-concepto").value.trim();
    const colImporte = document.getElementById("csv-col-importe").value.trim();
    if (colFecha) fd.append("columna_fecha", colFecha);
    if (colConcepto) fd.append("columna_concepto", colConcepto);
    if (colImporte) fd.append("columna_importe", colImporte);
    fd.append("separador", document.getElementById("csv-sep").value || ";");

    try {
      const res = await apiFetch("/api/bank/import", { method: "POST", body: fd });
      const saldos = res.saldos_importados || [];
      showToast(
        `Extracto leído: ${res.nuevos_insertados} movimientos nuevos añadidos (${res.duplicados_omitidos} repetidos ignorados).` +
          (saldos.length ? ` Saldo del banco: ${saldos.map((x) => fmtEur(x.saldo)).join(", ")}.` : "")
      );
      fileInput.value = "";
      loadBankReconciliation();
      loadDashboard();
    } catch (err) {
      showToast("Error subiendo el archivo del banco: " + err.message);
    }
  });

  document.getElementById("btn-run-auto-reconcile").addEventListener("click", (ev) =>
    withBusyButton(ev.currentTarget, async () => {
      try {
        const res = await apiFetch("/api/bank/auto-reconcile", { method: "POST" });
        showToast(
          `Búsqueda terminada: ${res.conciliados_automaticos || 0} pagos cuadrados solos y ${res.propuestas_revision || 0} propuestas para revisar.`
        );
        loadBankReconciliation();
        loadDashboard();
      } catch (err) {
        showToast("No se pudo buscar coincidencias: " + err.message);
      }
    })
  );

  document.getElementById("btn-save-manual-reconcile").addEventListener("click", async () => {
    if (!state.selectedBankTx) return;
    const asignaciones = [];
    document.querySelectorAll(".manual-inv-check:checked").forEach((chk) => {
      const id = parseInt(chk.getAttribute("data-id"), 10);
      const amtInput = document.querySelector(`.manual-inv-amount[data-id="${id}"]`);
      const val = parseFloat(amtInput ? amtInput.value : "0") || 0;
      asignaciones.push({ factura_id: id, importe_asignado: val });
    });
    if (asignaciones.length === 0) {
      showToast("Marca con un tic al menos una factura de la lista.");
      return;
    }
    try {
      const res = await apiFetch("/api/bank/reconcile-manual", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          movimiento_id: state.selectedBankTx.id,
          asignaciones,
          aprender_regla: document.getElementById("manual-learn-rule").checked,
        }),
      });
      showToast(
        res.cuadra_exacto
          ? "Pago unido con sus facturas correctamente (suma exacta)."
          : `Pago guardado dejando una diferencia pendiente de ${fmtEur(res.diferencia_pendiente)}.`
      );
      state.selectedBankTx = null;
      document.getElementById("btn-save-manual-reconcile").disabled = true;
      document.getElementById("manual-selected-tx-label").textContent =
        "Ningún movimiento del banco seleccionado todavía";
      loadBankReconciliation();
      loadDashboard();
    } catch (err) {
      showToast("Error guardando la unión: " + err.message);
    }
  });

  // Configuración
  document.getElementById("cfg-umbral-confianza").addEventListener("input", (e) => {
    document.getElementById("cfg-umbral-val").textContent = Math.round(parseFloat(e.target.value) * 100) + "%";
  });

  document.getElementById("cfg-proveedor-modelo").addEventListener("change", (e) => {
    // Al cambiar de proveedor se propone su modelo recomendado y su dirección oficial.
    document.getElementById("cfg-llm-endpoint").value = "";
    renderModelOptions(e.target.value, "");
    document.getElementById("cfg-test-result").textContent = "";
  });

  document.getElementById("cfg-modelo-select").addEventListener("change", () => {
    updateModelHint();
  });

  document.getElementById("btn-test-llm-key").addEventListener("click", async () => {
    const resBox = document.getElementById("cfg-test-result");
    resBox.style.color = "var(--text-muted)";
    resBox.textContent = "Probando conexión con el lector de facturas...";
    try {
      const res = await apiFetch("/api/settings/test-llm", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          proveedor_modelo: document.getElementById("cfg-proveedor-modelo").value,
          modelo_llm: getSelectedModel(),
          llm_endpoint_url: document.getElementById("cfg-llm-endpoint").value.trim(),
          llm_api_key: document.getElementById("cfg-llm-api-key").value.trim(),
        }),
      });
      resBox.style.color = res.ok ? "var(--success)" : "var(--danger)";
      resBox.textContent = res.mensaje;
      if (res.ok) loadConfiguration();
    } catch (err) {
      resBox.style.color = "var(--danger)";
      resBox.textContent = "Error al probar la clave: " + err.message;
    }
  });

  onSubmitBusy("form-general-settings", async (e) => {
    const payload = {
      proveedor_modelo: document.getElementById("cfg-proveedor-modelo").value,
      modelo_llm: getSelectedModel(),
      llm_endpoint_url: document.getElementById("cfg-llm-endpoint").value.trim(),
      estrategia_ocr: document.getElementById("cfg-estrategia-ocr").value,
      umbral_confianza: parseFloat(document.getElementById("cfg-umbral-confianza").value),
      capa_almacenamiento: document.getElementById("cfg-capa-almacenamiento").value,
      carpeta_salida: document.getElementById("cfg-carpeta-salida").value.trim(),
      llm_api_key: document.getElementById("cfg-llm-api-key").value.trim(),
    };
    try {
      await apiFetch("/api/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      document.getElementById("cfg-llm-api-key").value = "";
      showToast("Opciones guardadas correctamente.");
      loadConfiguration();
    } catch (err) {
      showToast("Error guardando opciones: " + err.message);
    }
  });

  onSubmitBusy("form-mail-account", async (e) => {
    const remitentes = document
      .getElementById("mail-remitentes")
      .value.split(",")
      .map((s) => s.trim())
      .filter(Boolean);
    const payload = {
      nombre: document.getElementById("mail-nombre").value.trim(),
      email: document.getElementById("mail-email").value.trim(),
      tipo_conexion: document.getElementById("mail-tipo").value,
      servidor_imap: document.getElementById("mail-host").value.trim(),
      puerto_imap: parseInt(document.getElementById("mail-port").value || "993", 10),
      password_o_token: document.getElementById("mail-secret").value,
      remitentes_permitidos: remitentes,
    };
    try {
      await apiFetch("/api/mail/accounts", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      document.getElementById("mail-secret").value = "";
      showToast("Cuenta de correo guardada de forma segura.");
      loadConfiguration();
    } catch (err) {
      showToast("Error guardando cuenta de correo: " + err.message);
    }
  });

  onSubmitBusy("form-rule", async (e) => {
    const payload = {
      tipo_regla: document.getElementById("rule-tipo").value,
      patron: document.getElementById("rule-patron").value.trim(),
      proveedor_nombre: document.getElementById("rule-proveedor").value.trim(),
      categoria: document.getElementById("rule-categoria").value,
    };
    await apiFetch("/api/rules", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    document.getElementById("rule-patron").value = "";
    showToast("Regla añadida.");
    loadConfiguration();
  });

  document.getElementById("btn-manual-backup").addEventListener("click", (ev) =>
    withBusyButton(ev.currentTarget, async () => {
      try {
        const res = await apiFetch("/api/database/backup", { method: "POST" });
        showToast(res.created ? `Copia de seguridad guardada en: ${res.backup_path}` : "Todavía no hay datos que copiar.");
      } catch (err) {
        showToast("No se pudo guardar la copia: " + err.message);
      }
    })
  );

  document.getElementById("btn-change-data-dir").addEventListener("click", async () => {
    const box = document.getElementById("data-dir-status-box");
    const ruta = document.getElementById("cfg-carpeta-datos-nueva").value.trim();
    if (!ruta) {
      box.textContent = "Escribe primero la carpeta nueva.";
      return;
    }
    box.textContent = "Preparando la nueva carpeta...";
    try {
      const res = await apiFetch("/api/settings/data-dir", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ruta }),
      });
      box.innerHTML = `${escapeHtml(res.mensaje || "")}<div class="alert alert-warning" style="margin-top:0.6rem;"><strong>Reinicia Archivero</strong> (ciérralo y vuelve a abrirlo) para empezar a usar la nueva carpeta. Hasta entonces no se recogerán facturas nuevas ni se revisará el correo.</div>`;
      showToast("Reinicia Archivero para usar la nueva carpeta de datos.");
    } catch (err) {
      box.textContent = "No se pudo cambiar la carpeta: " + err.message;
    }
  });

  document.getElementById("btn-check-updates").addEventListener("click", async () => {
    const box = document.getElementById("updater-status-box");
    box.textContent = "Comprobando si hay una versión nueva...";
    const res = await apiFetch("/api/updater/check");
    if (res.error) {
      box.textContent = `No se pudo comprobar ahora mismo: ${res.error}`;
    } else if (res.hay_actualizacion) {
      box.innerHTML = `<strong style="color:var(--success);">¡Hay una versión nueva disponible (${escapeHtml(res.version_remota)})!</strong> <button type="button" class="btn btn-primary btn-sm" id="btn-install-update">Instalar ahora</button>`;
      document.getElementById("btn-install-update").addEventListener("click", async (ev) => {
        ev.target.disabled = true;
        box.textContent = "Descargando y comprobando la actualización...";
        try {
          const r = await apiFetch("/api/updater/install", { method: "POST" });
          box.textContent = r.mensaje || "Actualización en curso.";
        } catch (err) {
          box.textContent = "No se pudo instalar la actualización: " + err.message;
        }
      });
    } else {
      box.textContent = `Ya tienes instalada la última versión disponible (${res.version_actual}).`;
    }
  });
});

/* =========================================================
   Liquidez y próximos pagos (Resumen General)
   ========================================================= */
const LIQ_ALERT = {
  verde: { cls: "badge-success", txt: "Todo bien" },
  ambar: { cls: "badge-warning", txt: "Atención" },
  rojo: { cls: "badge-danger", txt: "Peligro: saldo insuficiente" },
};

function fmtDate(iso) {
  if (!iso) return "-";
  const parts = String(iso).slice(0, 10).split("-");
  return parts.length === 3 ? `${parts[2]}/${parts[1]}/${parts[0]}` : String(iso);
}

function fmtDateTime(iso) {
  if (!iso) return "-";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return String(iso);
  return d.toLocaleString("es-ES", { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" });
}

async function loadLiquidity() {
  try {
    const data = await apiFetch("/api/liquidity?horizonte=90");
    const res = data.resumen || {};
    const alert = LIQ_ALERT[data.alerta] || LIQ_ALERT.ambar;
    const badge = document.getElementById("liq-alert-badge");
    badge.className = `badge ${alert.cls}`;
    badge.textContent = alert.txt;

    const hint = document.getElementById("liq-hint");
    if (!data.saldo_disponible) {
      hint.style.display = "block";
      hint.textContent =
        "Importa un extracto Norma 43 o CAMT en 'Cuadrar con el Banco' para ver tu saldo.";
    } else {
      hint.style.display = "none";
    }
    const motivosBox = document.getElementById("liq-motivos");
    const motivos = (data.motivos || []).filter((m) => data.saldo_disponible || !m.startsWith("Aún no"));
    if (motivos.length) {
      motivosBox.style.display = "block";
      motivosBox.className = data.alerta === "rojo" ? "alert alert-danger" : "alert alert-warning";
      motivosBox.innerHTML = motivos.map((m) => escapeHtml(m)).join("<br/>");
    } else {
      motivosBox.style.display = "none";
    }

    document.getElementById("liq-saldo").textContent = data.saldo_disponible ? fmtEur(data.saldo_actual) : "-";
    const cuentas = data.cuentas || [];
    document.getElementById("liq-saldo-fecha").textContent = cuentas.length
      ? cuentas.map((c) => `${c.cuenta}: ${fmtEur(c.saldo)} a ${fmtDate(c.fecha_saldo)}${c.antiguo ? " (antiguo)" : ""}`).join(" · ")
      : "Sin extracto importado";
    document.getElementById("liq-vencido").textContent = fmtEur(res.vencido);
    document.getElementById("liq-7").textContent = fmtEur(res.proximos_7);
    document.getElementById("liq-30").textContent = fmtEur(res.proximos_30);
    document.getElementById("liq-previsto").textContent =
      res.saldo_previsto_30 === null || res.saldo_previsto_30 === undefined ? "-" : fmtEur(res.saldo_previsto_30);
    const tile = document.getElementById("liq-tile-previsto");
    tile.className = `liq-tile liq-${["verde", "ambar", "rojo"].includes(data.alerta) ? data.alerta : "ambar"}`;

    renderLiquidityChart(data.serie_semanal || [], !!data.saldo_disponible);

    const tbody = document.getElementById("tbody-liq-pagos");
    tbody.innerHTML = "";
    const pagos = data.pagos || [];
    pagos.slice(0, 40).forEach((p) => {
      const tr = document.createElement("tr");
      const fechaBadge = p.vencida
        ? ' <span class="badge badge-danger">vencida</span>'
        : "";
      const estBadge = p.estimado ? ' <span class="badge badge-info">estimada</span>' : "";
      tr.innerHTML = `
        <td><strong>${escapeHtml(fmtDate(p.fecha))}</strong>${fechaBadge}${estBadge}</td>
        <td>${escapeHtml(p.proveedor || "Sin proveedor")}</td>
        <td>${escapeHtml(p.numero_factura || "-")}</td>
        <td class="num">${fmtEur(p.importe_pendiente)}</td>
        <td><button type="button" class="btn btn-secondary btn-sm" onclick="openInvoiceModal(${Number(p.factura_id)})">Ver factura</button></td>
      `;
      tbody.appendChild(tr);
    });
    if (pagos.length === 0) {
      tbody.innerHTML = `<tr><td colspan="5" style="text-align:center;color:#64748b;">No hay pagos pendientes en los próximos 90 días.</td></tr>`;
    } else if (pagos.length > 40) {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td colspan="5" style="text-align:center;color:#64748b;">Y ${pagos.length - 40} pagos más en los próximos 90 días.</td>`;
      tbody.appendChild(tr);
    }
  } catch (err) {
    showToast("Error cargando la previsión de pagos: " + err.message);
  }
}

function renderLiquidityChart(series, hasBalance) {
  const container = document.getElementById("liq-chart");
  if (!series.length) {
    container.innerHTML = "";
    return;
  }
  const values = series.map((w) => Number(hasBalance ? w.saldo_proyectado : w.pagos_acumulados) || 0);
  const W = 720;
  const H = 220;
  const padL = 70;
  const padR = 10;
  const padT = 14;
  const padB = 34;
  // Si todo el saldo es positivo, la escala parte de un mínimo cercano (no de 0 €)
  // para que se aprecie cuánto baja cada semana. Si hay negativos, se incluye el 0 € como referencia.
  const rawMax = Math.max(...values);
  const rawMin = Math.min(...values);
  const allPositive = rawMin >= 0;
  const margin = Math.max((rawMax - rawMin) * 0.15, Math.abs(rawMax) * 0.02, 1);
  const maxV = rawMax + margin;
  const minV = allPositive ? Math.max(0, rawMin - margin) : Math.min(0, rawMin - margin);
  const span = maxV - minV || 1;
  const y = (v) => padT + ((maxV - v) / span) * (H - padT - padB);
  const slot = (W - padL - padR) / values.length;
  const barW = Math.max(6, slot * 0.6);
  const baseV = minV <= 0 && maxV >= 0 ? 0 : minV;
  const baseY = y(baseV);

  let bars = "";
  let points = "";
  values.forEach((v, i) => {
    const x = padL + i * slot + (slot - barW) / 2;
    const top = Math.min(y(v), baseY);
    const h = Math.max(1, Math.abs(y(v) - baseY));
    const color = hasBalance && v < 0 ? "var(--danger)" : "var(--primary)";
    const w = series[i];
    const tip = `Semana ${w.semana} (${fmtDate(w.desde)} - ${fmtDate(w.hasta)}): ${
      hasBalance ? "saldo previsto" : "pagos acumulados"
    } ${fmtEur(v)}; pagos de la semana ${fmtEur(w.pagos)}`;
    bars += `<rect x="${x.toFixed(1)}" y="${top.toFixed(1)}" width="${barW.toFixed(1)}" height="${h.toFixed(1)}" rx="3" fill="${color}" opacity="0.85"><title>${escapeHtml(tip)}</title></rect>`;
    points += `${(x + barW / 2).toFixed(1)},${y(v).toFixed(1)} `;
    if (i % 2 === 0) {
      bars += `<text x="${(x + barW / 2).toFixed(1)}" y="${H - 12}" font-size="11" text-anchor="middle" fill="#64748b">${escapeHtml(fmtDate(w.desde).slice(0, 5))}</text>`;
    }
  });
  const gridVals = [maxV, (maxV + minV) / 2, minV];
  let axis = gridVals
    .map(
      (gv) => `
    <line x1="${padL}" x2="${W - padR}" y1="${y(gv).toFixed(1)}" y2="${y(gv).toFixed(1)}" stroke="#e2e8f0" stroke-width="1"/>
    <text x="${padL - 6}" y="${(y(gv) + 4).toFixed(1)}" font-size="11" text-anchor="end" fill="#64748b">${escapeHtml(fmtEur(gv))}</text>`
    )
    .join("");
  if (baseV === 0) {
    axis += `<line x1="${padL}" x2="${W - padR}" y1="${baseY.toFixed(1)}" y2="${baseY.toFixed(1)}" stroke="#94a3b8" stroke-width="1"/>
    <text x="${padL - 6}" y="${(baseY + 4).toFixed(1)}" font-size="11" text-anchor="end" fill="#64748b">0 €</text>`;
  }
  const line = `<polyline points="${points.trim()}" fill="none" stroke="#0f172a" stroke-width="1.5" stroke-opacity="0.6"/>`;
  bars += line;
  const caption = hasBalance
    ? "Cada barra es el saldo que quedaría en el banco al final de esa semana si pagas todo a su vencimiento (en rojo si queda en negativo)."
    : "Sin saldo del banco: cada barra muestra el total acumulado que habrá que pagar hasta esa semana.";
  container.innerHTML = `
    <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Saldo previsto por semana">${axis}${bars}</svg>
    <div class="field-hint">${escapeHtml(caption)}</div>
  `;
}

/* =========================================================
   Plazos de pago por proveedor (Configuración)
   ========================================================= */
const TERM_ORIGIN_LABELS = { fijado: "Fijado por ti", aprendido: "Aprendido", general: "Plazo general" };

async function loadPaymentTerms() {
  try {
    const data = await apiFetch("/api/suppliers/payment-terms");
    const tbody = document.getElementById("tbody-payment-terms");
    tbody.innerHTML = "";
    (data.items || []).forEach((t) => {
      const tr = document.createElement("tr");
      const aprendido =
        t.plazo_aprendido === null || t.plazo_aprendido === undefined
          ? "-"
          : `${Number(t.plazo_aprendido)} días <small style="color:#64748b;">(de ${Number(t.muestras_aprendidas)} facturas)</small>`;
      tr.innerHTML = `
        <td><strong>${escapeHtml(t.nombre)}</strong>${t.nif ? `<br/><small style="color:#64748b;">${escapeHtml(t.nif)}</small>` : ""}</td>
        <td class="num">${Number(t.total_facturas || 0)}</td>
        <td>${aprendido}</td>
        <td>${Number(t.plazo_efectivo)} días <span class="badge badge-info">${escapeHtml(TERM_ORIGIN_LABELS[t.origen] || t.origen)}</span></td>
        <td style="white-space:nowrap;">
          <input type="number" min="0" max="365" style="width:80px;" class="term-input" data-id="${Number(t.proveedor_id)}"
            value="${t.plazo_pago_dias === null || t.plazo_pago_dias === undefined ? "" : Number(t.plazo_pago_dias)}" placeholder="auto" />
          <button type="button" class="btn btn-secondary btn-sm" onclick="saveSupplierTerm(${Number(t.proveedor_id)})">Guardar</button>
        </td>
      `;
      tbody.appendChild(tr);
    });
    if (!data.items || data.items.length === 0) {
      tbody.innerHTML = `<tr><td colspan="5" style="text-align:center;color:#64748b;">Todavía no hay proveedores registrados.</td></tr>`;
    }
  } catch (err) {
    showToast("Error cargando los plazos de pago: " + err.message);
  }
}

async function saveSupplierTerm(supplierId) {
  const input = document.querySelector(`.term-input[data-id="${supplierId}"]`);
  const raw = input ? input.value.trim() : "";
  try {
    await apiFetch(`/api/suppliers/${supplierId}/payment-term`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ plazo_pago_dias: raw === "" ? null : parseInt(raw, 10) }),
    });
    showToast(raw === "" ? "Plazo en automático: se calculará con sus facturas." : `Plazo guardado: ${raw} días.`);
    loadPaymentTerms();
  } catch (err) {
    showToast("Error guardando el plazo: " + err.message);
  }
}

/* =========================================================
   Actividad, avisos y campana
   ========================================================= */
const EVENT_TYPE_LABELS = {
  inicio_programa: "Inicio del programa",
  correo_revisado: "Correo revisado",
  error_correo: "Error de correo",
  factura_nueva: "Factura nueva",
  factura_revision: "Factura para revisar",
  factura_sustituida: "Factura sustituida",
  factura_duplicada: "Factura repetida",
  error_ia: "Fallo de la IA",
  extracto_importado: "Extracto del banco",
  conciliacion: "Cuadre con el banco",
  copia_seguridad: "Copia de seguridad",
  actualizacion: "Actualización",
  config_cambiada: "Cambio de configuración",
  vencimiento: "Vencimientos",
};

const LEVEL_BADGES = {
  error: '<span class="badge badge-danger">Error</span>',
  aviso: '<span class="badge badge-warning">Aviso</span>',
  info: '<span class="badge badge-info">Info</span>',
};

const activityState = { offset: 0, limit: 50, total: 0 };

function eventLink(ev) {
  if (ev.entidad === "factura" && ev.entidad_id) {
    return `<button type="button" class="btn btn-secondary btn-sm" onclick="openInvoiceModal(${Number(ev.entidad_id)})">Ver factura</button>`;
  }
  return "";
}

async function refreshUnreadCount() {
  try {
    const data = await apiFetch("/api/activity/unread-count");
    const n = Number(data.no_leidos || 0);
    const important = Number(data.importantes || 0);
    const badge = document.getElementById("bell-badge");
    const nav = document.getElementById("badge-actividad");
    if (n > 0) {
      badge.style.display = "inline-block";
      badge.textContent = n > 99 ? "99+" : String(n);
      badge.classList.toggle("bell-badge-important", important > 0);
    } else {
      badge.style.display = "none";
    }
    // La campana y el menú muestran el mismo número (avisos sin leer) para no confundir.
    const title =
      important > 0
        ? `${n} avisos sin leer (${important} importantes: errores o cosas que revisar)`
        : `${n} avisos sin leer`;
    badge.title = title;
    nav.title = title;
    if (n > 0) {
      nav.style.display = "inline-block";
      nav.textContent = n > 99 ? "99+" : String(n);
    } else {
      nav.style.display = "none";
    }
  } catch (_) {
    // Sin conexión con el programa: se reintentará en la siguiente vuelta
  }
}

async function loadBellList() {
  const list = document.getElementById("bell-list");
  list.innerHTML = `<div class="field-hint">Cargando...</div>`;
  try {
    const data = await apiFetch("/api/activity?limit=40");
    const rank = { error: 0, aviso: 1, info: 2 };
    const items = (data.items || [])
      .slice()
      .sort((a, b) => (rank[a.nivel] ?? 3) - (rank[b.nivel] ?? 3) || (a.fecha < b.fecha ? 1 : -1))
      .slice(0, 10);
    if (!items.length) {
      list.innerHTML = `<div class="field-hint" style="padding:0.5rem;">No hay avisos todavía.</div>`;
      return;
    }
    list.innerHTML = items
      .map(
        (ev) => `
        <div class="bell-item ${ev.leido ? "" : "unread"}">
          ${LEVEL_BADGES[ev.nivel] || ""} ${escapeHtml(ev.mensaje)}
          <div class="bell-item-meta">${escapeHtml(fmtDateTime(ev.fecha))} · ${escapeHtml(EVENT_TYPE_LABELS[ev.tipo] || ev.tipo)}
            ${ev.entidad === "factura" && ev.entidad_id ? ` · <a href="#" onclick="openInvoiceModal(${Number(ev.entidad_id)}); return false;">Ver factura</a>` : ""}
          </div>
        </div>`
      )
      .join("");
  } catch (err) {
    list.innerHTML = `<div class="field-hint">No se pudieron cargar los avisos: ${escapeHtml(err.message)}</div>`;
  }
}

function toggleBellPanel(forceOpen) {
  const panel = document.getElementById("bell-panel");
  const btn = document.getElementById("btn-bell");
  const open = forceOpen === undefined ? !panel.classList.contains("open") : forceOpen;
  panel.classList.toggle("open", open);
  btn.setAttribute("aria-expanded", open ? "true" : "false");
  if (open) loadBellList();
}

async function markAllEventsRead() {
  try {
    await apiFetch("/api/activity/mark-read", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ all: true }),
    });
    refreshUnreadCount();
    if (document.getElementById("bell-panel").classList.contains("open")) loadBellList();
    if (document.getElementById("tab-actividad").classList.contains("active")) loadActivityList();
  } catch (err) {
    showToast("Error marcando los avisos: " + err.message);
  }
}

function goToActivityTab() {
  toggleBellPanel(false);
  const btn = document.querySelector('.nav-btn[data-tab="tab-actividad"]');
  if (btn) btn.click();
}

function populateActivityTypeFilter() {
  const sel = document.getElementById("activity-filter-tipo");
  if (sel.options.length > 1) return;
  sel.innerHTML =
    `<option value="">Todos</option>` +
    Object.entries(EVENT_TYPE_LABELS)
      .map(([k, v]) => `<option value="${escapeHtml(k)}">${escapeHtml(v)}</option>`)
      .join("");
}

function loadActivityScreen() {
  populateActivityTypeFilter();
  activityState.offset = 0;
  loadHealth();
  loadActivityList();
}

async function loadHealth() {
  const box = document.getElementById("health-status");
  const probBox = document.getElementById("health-problems");
  const tbody = document.getElementById("tbody-health-mailboxes");
  try {
    const h = await apiFetch("/api/activity/health");
    box.className = `health-status health-${h.estado === "ok" ? "ok" : h.estado === "error" ? "error" : "aviso"}`;
    box.innerHTML =
      h.estado === "ok"
        ? "<strong>✓ El programa está funcionando.</strong> Revisando el correo y guardando facturas con normalidad."
        : "<strong>Hay cosas que revisar:</strong>";
    probBox.innerHTML = (h.problemas || [])
      .map(
        (p) =>
          `<div class="alert ${p.nivel === "error" ? "alert-danger" : "alert-warning"}" style="margin-bottom:0.5rem;">${escapeHtml(p.mensaje)}</div>`
      )
      .join("");

    tbody.innerHTML = "";
    (h.buzones || []).forEach((b) => {
      const estado =
        b.estado === "ok"
          ? '<span class="badge badge-success">Funcionando</span>'
          : b.estado === "error"
          ? '<span class="badge badge-danger">Error</span>'
          : '<span class="badge badge-warning">Revisar</span>';
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td><strong>${escapeHtml(b.nombre || b.email)}</strong><br/><small style="color:#64748b;">${escapeHtml(b.email)}</small></td>
        <td>${escapeHtml(b.ultima_revision_ok ? fmtDateTime(b.ultima_revision_ok) : "Nunca")}${
          b.minutos_desde_revision !== null && b.minutos_desde_revision !== undefined
            ? ` <small style="color:#64748b;">(hace ${Number(b.minutos_desde_revision)} min)</small>`
            : ""
        }</td>
        <td>${estado}${b.ultimo_error ? `<div style="font-size:0.76rem;color:#991b1b;">${escapeHtml(b.ultimo_error)}</div>` : ""}</td>
        <td>${
          Number(b.mensajes_con_error || 0) > 0 || b.ultimo_error
            ? `<button type="button" class="btn btn-secondary btn-sm" onclick="retryMailErrors(${Number(b.id)}, this)">Reintentar correos con error</button>`
            : ""
        }</td>
      `;
      tbody.appendChild(tr);
    });
    if (!h.buzones || h.buzones.length === 0) {
      tbody.innerHTML = `<tr><td colspan="4" style="text-align:center;color:#64748b;">No hay buzones de correo activos. Añádelos en Configuración.</td></tr>`;
    }
  } catch (err) {
    box.className = "health-status health-aviso";
    box.textContent = "No se pudo comprobar el estado del programa: " + err.message;
  }
}

async function loadActivityList() {
  const params = new URLSearchParams();
  params.set("limit", String(activityState.limit));
  params.set("offset", String(activityState.offset));
  const nivel = document.getElementById("activity-filter-nivel").value;
  const tipo = document.getElementById("activity-filter-tipo").value;
  if (nivel) params.set("nivel", nivel);
  if (tipo) params.set("tipo", tipo);
  if (document.getElementById("activity-filter-unread").checked) params.set("solo_no_leidos", "true");
  try {
    const data = await apiFetch(`/api/activity?${params.toString()}`);
    activityState.total = Number(data.total || 0);
    const tbody = document.getElementById("tbody-activity");
    tbody.innerHTML = "";
    (data.items || []).forEach((ev) => {
      const tr = document.createElement("tr");
      if (!ev.leido) tr.className = "activity-unread";
      tr.innerHTML = `
        <td style="white-space:nowrap;">${escapeHtml(fmtDateTime(ev.fecha))}</td>
        <td>${LEVEL_BADGES[ev.nivel] || escapeHtml(ev.nivel)}<br/><small style="color:#64748b;">${escapeHtml(EVENT_TYPE_LABELS[ev.tipo] || ev.tipo)}</small></td>
        <td>${escapeHtml(ev.mensaje)}</td>
        <td>${eventLink(ev)}</td>
      `;
      tbody.appendChild(tr);
    });
    if (!data.items || data.items.length === 0) {
      tbody.innerHTML = `<tr><td colspan="4" style="text-align:center;color:#64748b;">No hay actividad con estos filtros.</td></tr>`;
    }
    const from = activityState.total ? activityState.offset + 1 : 0;
    const to = Math.min(activityState.offset + activityState.limit, activityState.total);
    document.getElementById("activity-page-info").textContent = `${from}-${to} de ${activityState.total}`;
    document.getElementById("btn-activity-prev").disabled = activityState.offset === 0;
    document.getElementById("btn-activity-next").disabled = to >= activityState.total;
  } catch (err) {
    showToast("Error cargando la actividad: " + err.message);
  }
}

document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("btn-bell").addEventListener("click", (e) => {
    e.stopPropagation();
    toggleBellPanel();
  });
  document.getElementById("bell-panel").addEventListener("click", (e) => e.stopPropagation());
  document.addEventListener("click", () => toggleBellPanel(false));
  document.getElementById("btn-bell-mark-all").addEventListener("click", markAllEventsRead);
  document.getElementById("btn-bell-see-all").addEventListener("click", goToActivityTab);
  document.getElementById("btn-activity-mark-all").addEventListener("click", markAllEventsRead);
  document.getElementById("btn-activity-refresh").addEventListener("click", () => {
    activityState.offset = 0;
    loadActivityList();
  });
  ["activity-filter-nivel", "activity-filter-tipo", "activity-filter-unread"].forEach((id) =>
    document.getElementById(id).addEventListener("change", () => {
      activityState.offset = 0;
      loadActivityList();
    })
  );
  document.getElementById("btn-activity-prev").addEventListener("click", () => {
    activityState.offset = Math.max(0, activityState.offset - activityState.limit);
    loadActivityList();
  });
  document.getElementById("btn-activity-next").addEventListener("click", () => {
    activityState.offset += activityState.limit;
    loadActivityList();
  });

  document.getElementById("btn-save-plazo-defecto").addEventListener("click", async () => {
    const val = parseInt(document.getElementById("cfg-plazo-defecto").value, 10);
    if (isNaN(val) || val < 0 || val > 365) {
      showToast("Escribe un plazo entre 0 y 365 días.");
      return;
    }
    try {
      await apiFetch("/api/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ plazo_pago_defecto_dias: val }),
      });
      showToast(`Plazo general guardado: ${val} días.`);
      loadPaymentTerms();
    } catch (err) {
      showToast("Error guardando el plazo general: " + err.message);
    }
  });

  refreshUnreadCount();
  setInterval(refreshUnreadCount, 60000);
});
