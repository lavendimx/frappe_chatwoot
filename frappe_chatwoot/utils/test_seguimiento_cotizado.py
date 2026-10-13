# Copyright (c) 2026, lavendi.mx
# License: MIT
"""Tests de `utils/seguimiento_cotizado.py` — disparo de la Secuencia de seguimiento al
mover un `CRM Deal` a la etapa de cotización declarada por `site_config.json`.

Mismo criterio que `test_secuencias.py`: nada aquí manda WhatsApp/correo real ni toca la
BD de verdad. Se mockea `frappe.conf` (gate por sitio), `frappe.enqueue` (el job de
background nunca corre en estas pruebas) y `secuencias.inscribir_deal` (la pieza que sí
escribe) — igual que ese archivo mockea `_ejecutar_paso`/`frappe.db.set_value` para probar
`avanzar()` sin efectos de red.
"""

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from frappe_chatwoot.utils import secuencias
from frappe_chatwoot.utils import seguimiento_cotizado as sc

CONFIG_LAVENDI = {
    "estado_cotizado": "Cotizado",
    "secuencia": "4. Seguimientos — PVP",
    "estados_previos": ["Lead", "Contactado", "Oportunidades futuras"],
}


def _conf(valor):
    """`frappe.conf` de prueba: un `_dict` con (o sin) la clave `seguimiento_cotizado`,
    igual que lo que devuelve `frappe.conf.get(...)` contra un `site_config.json` real."""
    return frappe._dict({"seguimiento_cotizado": valor} if valor is not None else {})


def _deal(status, anterior_status=None, name="CRM-DEAL-TEST"):
    """Un `CRM Deal` de prueba con `get_doc_before_save()` resuelto a mano — mismo
    patrón que el helper `_ins()` de test_secuencias.py."""
    doc = frappe._dict({"name": name, "status": status})
    anterior = frappe._dict({"status": anterior_status}) if anterior_status is not None else None
    doc.get_doc_before_save = lambda: anterior
    return doc


class TestConfig(FrappeTestCase):
    """`_config()` — gate explícito por `site_config.json`, sin ningún default."""

    def test_sin_clave_devuelve_none(self):
        with patch.object(sc.frappe, "conf", _conf(None)):
            self.assertIsNone(sc._config())

    def test_clave_incompleta_sin_secuencia_devuelve_none(self):
        incompleta = {k: v for k, v in CONFIG_LAVENDI.items() if k != "secuencia"}
        with patch.object(sc.frappe, "conf", _conf(incompleta)):
            self.assertIsNone(sc._config())

    def test_estados_previos_vacio_devuelve_none(self):
        incompleta = {**CONFIG_LAVENDI, "estados_previos": []}
        with patch.object(sc.frappe, "conf", _conf(incompleta)):
            self.assertIsNone(sc._config())

    def test_clave_completa_devuelve_la_config(self):
        with patch.object(sc.frappe, "conf", _conf(CONFIG_LAVENDI)):
            self.assertEqual(sc._config(), CONFIG_LAVENDI)


class TestOnDealUpdate(FrappeTestCase):
    """`on_deal_update()` — el hook de `CRM Deal.on_update`."""

    def _disparar(self, doc, config=CONFIG_LAVENDI):
        with patch.object(sc.frappe, "conf", _conf(config)), \
             patch.object(sc.frappe, "enqueue") as enqueue:
            sc.on_deal_update(doc)
        return enqueue

    def test_a_transicion_valida_dispara_la_inscripcion(self):
        """(a) Mover un deal, con el gate de site_config activo, de una etapa previa
        válida ("Contactado") a "Cotizado": encola `ejecutar`."""
        doc = _deal("Cotizado", anterior_status="Contactado")
        enqueue = self._disparar(doc)

        enqueue.assert_called_once()
        self.assertEqual(
            enqueue.call_args.args[0],
            "frappe_chatwoot.utils.seguimiento_cotizado.ejecutar",
        )
        kwargs = enqueue.call_args.kwargs
        self.assertEqual(kwargs["deal_name"], doc.name)
        self.assertEqual(kwargs["secuencia"], CONFIG_LAVENDI["secuencia"])
        self.assertTrue(kwargs["enqueue_after_commit"])

    def test_b_reguardado_ya_en_cotizado_no_reinscribe_ni_falla(self):
        """(b) Mover un deal que YA estaba en "Cotizado" (re-guardado sin cambio de
        etapa): no reinscribe ni lanza — mismo criterio que
        `onboarding.on_deal_update`/`nutricion.on_deal_update`."""
        doc = _deal("Cotizado", anterior_status="Cotizado")
        enqueue = self._disparar(doc)
        enqueue.assert_not_called()

    def test_c_sin_clave_en_site_config_el_hook_no_hace_nada(self):
        """(c) Sin `seguimiento_cotizado` declarada en `site_config.json`: el hook sale
        de inmediato, sin tocar nada — simulado, no corrido contra un sitio real sin
        la clave (p. ej. Estrublock, que tiene su PROPIA etapa "Cotizado")."""
        doc = _deal("Cotizado", anterior_status="Contactado")
        enqueue = self._disparar(doc, config=None)
        enqueue.assert_not_called()

    def test_etapa_distinta_a_la_declarada_no_hace_nada(self):
        doc = _deal("Negociaciones", anterior_status="Cotizado")
        enqueue = self._disparar(doc)
        enqueue.assert_not_called()

    def test_viene_de_etapa_no_declarada_como_previa_no_reinscribe(self):
        """Guard anti-regresión, mismo criterio que el de
        `agente-ia/server.js::programarSeguimientoCotizado` ("no regresar etapas"): si
        el deal venía de una etapa que no está en `estados_previos` (p. ej. ya iba en
        "3er Seguimiento"), no se toca — evita pisar avance real o reinscribir algo que
        ya está corriendo más adelante."""
        doc = _deal("Cotizado", anterior_status="3er Seguimiento")
        enqueue = self._disparar(doc)
        enqueue.assert_not_called()

    def test_deal_nuevo_sin_anterior_si_dispara(self):
        """Un Deal sin `get_doc_before_save()` (recién insertado ya en la etapa de
        cotización): mismo criterio que los otros 2 handlers de `CRM Deal.on_update`,
        que tampoco bloquean cuando no hay "anterior" con qué comparar."""
        doc = _deal("Cotizado", anterior_status=None)
        enqueue = self._disparar(doc)
        enqueue.assert_called_once()


class TestEjecutar(FrappeTestCase):
    """`ejecutar()` — eleva a Administrator y reusa `secuencias.inscribir_deal` tal
    cual, con su mismo contrato de idempotencia."""

    def test_inscribe_normalmente(self):
        with patch.object(sc.frappe, "set_user") as set_user, \
             patch.object(sc.frappe.db, "commit"), \
             patch.object(secuencias, "inscribir_deal",
                          return_value={"ok": True, "name": "SEC-INS-1"}) as inscribir:
            resultado = sc.ejecutar("CRM-DEAL-TEST", CONFIG_LAVENDI["secuencia"])

        set_user.assert_called_once_with("Administrator")
        inscribir.assert_called_once_with(
            secuencia=CONFIG_LAVENDI["secuencia"], deal="CRM-DEAL-TEST")
        self.assertTrue(resultado["ok"])

    def test_b_ya_inscrita_se_trata_como_exito_no_como_falla(self):
        """(b, idempotencia) `inscribir_deal` lanza "ya está inscrita" para una
        inscripción `Activa` repetida (p. ej. el deal se re-guarda en "Cotizado" tras
        un reintento del webhook) — eso es éxito, no un error que haya que avisar."""
        with patch.object(sc.frappe, "set_user"), \
             patch.object(sc.frappe.db, "commit"), \
             patch.object(
                 secuencias, "inscribir_deal",
                 side_effect=frappe.ValidationError(
                     "Esa oportunidad ya está inscrita en esa secuencia"),
             ):
            resultado = sc.ejecutar("CRM-DEAL-TEST", CONFIG_LAVENDI["secuencia"])

        self.assertTrue(resultado["ok"])
        self.assertTrue(resultado.get("ya_inscrita"))

    def test_otro_error_de_validacion_se_reporta_sin_propagar(self):
        """Un `ValidationError` que NO es el de "ya inscrita" (p. ej. la secuencia no
        existe en este sitio) se reporta como falla, pero no lanza — un hook de
        `on_update` que explota tumbaría el guardado del deal para el vendedor."""
        with patch.object(sc.frappe, "set_user"), \
             patch.object(sc.frappe.db, "commit"), \
             patch.object(sc.frappe, "log_error") as log_error, \
             patch.object(
                 secuencias, "inscribir_deal",
                 side_effect=frappe.ValidationError("Esa secuencia no existe"),
             ):
            resultado = sc.ejecutar("CRM-DEAL-TEST", "secuencia-inexistente")

        self.assertFalse(resultado["ok"])
        log_error.assert_called_once()
