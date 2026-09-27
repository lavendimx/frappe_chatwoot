# Copyright (c) 2026, lavendi.mx
# License: MIT
"""Tests del filtro allowlist del autofollowup (`utils/autofollowup.py`).

Foco 1 (2026-09-27) — allowlist fail-closed de inboxes: `barrer()` solo corre
en los inbox declarados en `site_config.autofollow_inboxes`. Si la clave falta
o está vacía, NO barre ningún inbox; un inbox fuera de la lista se omite aunque
su `Agente IA` esté activo, **incluso con `forzar_inbox`** (el bypass manual no
se salta la allowlist).

Foco 2 (2026-09-27) — guard `erpnext` en `_es_cliente`: sin el doctype
`Customer` el vínculo no puede existir y el filtro queda inerte por diseño; se
retorna `False` explícito sin consultas muertas (mismo patrón que
`cliente_erp._asegurar_customer`).

Sin red ni efectos: `frappe.conf`, `frappe.get_all`, `frappe.get_doc` y la capa
`frappe.db` se mockean; `_crear_followups_nuevos`/`_procesar_vencidos` no se
ejecutan (se mockean) para que la prueba sea pura.
"""

from contextlib import ExitStack
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from frappe_chatwoot.utils import autofollowup

APPS_SIN_ERP = ["frappe", "crm", "frappe_chatwoot"]
APPS_CON_ERP = ["frappe", "crm", "erpnext", "frappe_chatwoot"]


def _agente(**over):
    base = {
        "followup_activo": 1,
        "followup_pasos": [{"espera_valor": 1, "espera_unidad": "Horas"}],
        "followup_max_por_corrida": 4,
    }
    base.update(over)
    return frappe._dict(base)


class TestParseInboxes(FrappeTestCase):
    """Normalización del valor crudo — un valor mal escrito achica la lista."""

    def test_string_separado_por_coma_o_espacio(self):
        self.assertEqual(autofollowup._parse_inboxes("5, 7"), {5, 7})
        self.assertEqual(autofollowup._parse_inboxes("5 7"), {5, 7})

    def test_lista_con_basura_no_revienta(self):
        self.assertEqual(autofollowup._parse_inboxes([5, "7", "x", None]), {5, 7})

    def test_vacio_es_conjunto_vacio(self):
        for vacio in (None, "", [], ()):
            self.assertEqual(autofollowup._parse_inboxes(vacio), set())

    def test_escalar(self):
        self.assertEqual(autofollowup._parse_inboxes(5), {5})


class TestAllowlistBarrer(FrappeTestCase):
    """`barrer()` es fail-closed: sin allowlist declarada no toca la DB."""

    def _conf(self, **valores):
        return patch.object(autofollowup.frappe, "conf", frappe._dict(valores))

    def test_sin_llave_no_barre(self):
        with self._conf():
            with patch.object(autofollowup.frappe, "get_all") as g:
                out = autofollowup.barrer()
                g.assert_not_called()
        self.assertIn("omitido", out)

    def test_llave_vacia_no_barre(self):
        for vacio in ("", [], None):
            with self._conf(autofollow_inboxes=vacio):
                with patch.object(autofollowup.frappe, "get_all") as g:
                    out = autofollowup.barrer()
                    g.assert_not_called()
            self.assertIn("omitido", out)

    def test_forzar_inbox_fuera_de_allowlist_no_corre(self):
        with self._conf(autofollow_inboxes=[5]):
            with patch.object(autofollowup.frappe, "get_all") as g:
                out = autofollowup.barrer(forzar_inbox=7)
                g.assert_not_called()
        self.assertIn("omitido", out)

    def _correr_barrer(self, agentes, permitidos, forzar=None, agente=None):
        """Corre `barrer()` con la DB y el motor mockeados. Devuelve
        `(resultado, mock_get_doc, mock_crear, mock_procesar)`."""
        with self._conf(autofollow_inboxes=permitidos):
            with ExitStack() as stack:
                stack.enter_context(patch.object(
                    autofollowup.frappe, "get_all", return_value=agentes))
                g_doc = stack.enter_context(patch.object(autofollowup.frappe, "get_doc"))
                g_doc.return_value.as_dict.return_value = agente or _agente()
                stack.enter_context(patch.object(autofollowup, "_ahora"))
                stack.enter_context(patch.object(
                    autofollowup, "_dentro_de_ventana", return_value=True))
                m_crear = stack.enter_context(
                    patch.object(autofollowup, "_crear_followups_nuevos", return_value=0))
                m_procesar = stack.enter_context(
                    patch.object(autofollowup, "_procesar_vencidos",
                                 return_value={"enviados": 0, "saltados": 0, "errores": 0}))
                out = autofollowup.barrer(forzar_inbox=forzar)
                return out, g_doc, m_crear, m_procesar

    def test_barre_solo_el_inbox_permitido(self):
        out, g_doc, _, m_procesar = self._correr_barrer(
            agentes=["5", "7"], permitidos=[5])
        # El permitido corre; el otro se registra como omitido, no se procesa.
        self.assertIn("nuevos", out["5"])
        self.assertIn("omitido", out["7"])
        self.assertEqual(g_doc.call_count, 1)
        self.assertEqual(g_doc.call_args.args, ("Agente IA", "5"))
        self.assertEqual(m_procesar.call_count, 1)

    def test_inbox_permitido_con_activo_cero_no_corre(self):
        out, _, _, m_procesar = self._correr_barrer(
            agentes=["5"], permitidos=[5], agente=_agente(followup_activo=0))
        self.assertEqual(out, {})
        m_procesar.assert_not_called()

    def test_forzar_inbox_permitido_corre_aunque_activo_cero(self):
        out, g_doc, _, m_procesar = self._correr_barrer(
            agentes=["5"], permitidos=[5], forzar=5, agente=_agente(followup_activo=0))
        self.assertIn("5", out)
        self.assertNotIn("omitido", out["5"])
        self.assertEqual(m_procesar.call_count, 1)
        self.assertEqual(g_doc.call_args.args, ("Agente IA", "5"))


class TestEsClienteGuard(FrappeTestCase):
    """Guard `erpnext`: sin `Customer` no hay vínculo → `False` sin consultar."""

    def test_sin_erpnext_retorna_false_sin_consultar(self):
        with patch.object(autofollowup.frappe, "get_installed_apps",
                          return_value=APPS_SIN_ERP):
            with patch.object(autofollowup.frappe.db, "get_value") as gv:
                self.assertFalse(autofollowup._es_cliente("CONT-1"))
                gv.assert_not_called()

    def test_contacto_vacio_retorna_false_sin_consultar(self):
        with patch.object(autofollowup.frappe, "get_installed_apps",
                          return_value=APPS_CON_ERP):
            with patch.object(autofollowup.frappe.db, "get_value") as gv:
                self.assertFalse(autofollowup._es_cliente(None))
                gv.assert_not_called()

    def test_con_erpnext_sin_vinculo_es_false(self):
        with patch.object(autofollowup.frappe, "get_installed_apps",
                          return_value=APPS_CON_ERP):
            with patch.object(autofollowup.frappe.db, "get_value", return_value=None):
                self.assertFalse(autofollowup._es_cliente("CONT-1"))

    def test_con_erpnext_y_customer_vinculado_es_true(self):
        with patch.object(autofollowup.frappe, "get_installed_apps",
                          return_value=APPS_CON_ERP):
            with patch.object(autofollowup.frappe.db, "get_value",
                              side_effect=["ghl-1", "CUST-1"]):
                self.assertTrue(autofollowup._es_cliente("CONT-1"))
