# Copyright (c) 2026, lavendi.mx
# License: MIT
"""Tests del gate por plan (`utils/plan.py`) y de su uso en Redes sociales.

Foco 1 — D-1 (2026-09-27): el default de `plan_de_este_sitio()` es
**fail-closed**. Un sitio sin `sofia_plan`, o con un valor que no está en
`NIVELES`, se trata como `gratuito`, el nivel más bajo. Antes era `enterprise`
y un sitio mal provisionado heredaba permiso por omisión.

Foco 2 — D-4 (2026-09-27): Redes sociales (Ayrshare) es capacidad de Lite+ y su
punto de entrada (`api/redes_sociales.py`) exige el gate. Un sitio `gratuito` no
debe conectar ni leer su configuración.

Sin red ni efectos: `frappe.conf` se sustituye por un dict y los endpoints de
redes se cortan en el gate (antes de tocar `Redes Sociales` o Ayrshare).
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from frappe_chatwoot.frappe_chatwoot.api import redes_sociales
from frappe_chatwoot.utils import plan


class TestPlanDefault(FrappeTestCase):
    """D-1: el default del sitio es el más restrictivo."""

    def _conf(self, **valores):
        return patch.object(plan.frappe, "conf", frappe._dict(valores))

    def test_sin_llave_es_gratuito(self):
        with self._conf():
            self.assertEqual(plan.plan_de_este_sitio(), "gratuito")

    def test_llave_desconocida_es_gratuito(self):
        with self._conf(sofia_plan="platinum"):
            self.assertEqual(plan.plan_de_este_sitio(), "gratuito")

    def test_llave_valida_se_respeta(self):
        for nivel in plan.NIVELES:
            with self._conf(sofia_plan=nivel):
                self.assertEqual(plan.plan_de_este_sitio(), nivel)

    def test_nivel_desconocido_es_el_minimo(self):
        # `_nivel_index` de un valor fuera de NIVELES debe ser el de `gratuito`,
        # no el de enterprise (antes devolvía `len(NIVELES)-1`).
        self.assertEqual(plan._nivel_index("platinum"), plan.NIVELES.index("gratuito"))


class TestExigirPlanMinimo(FrappeTestCase):
    """El gate de plan es del SITIO; la agencia queda exenta."""

    def test_gratuito_no_pasa_lite(self):
        with patch.object(plan, "plan_de_este_sitio", return_value="gratuito"), \
                patch.object(plan, "_es_agencia", return_value=False):
            with self.assertRaises(frappe.PermissionError):
                plan.exigir_plan_minimo("lite")

    def test_lite_pasa_lite(self):
        with patch.object(plan, "plan_de_este_sitio", return_value="lite"), \
                patch.object(plan, "_es_agencia", return_value=False):
            self.assertIsNone(plan.exigir_plan_minimo("lite"))

    def test_agencia_exenta_de_cualquier_minimo(self):
        with patch.object(plan, "plan_de_este_sitio", return_value="gratuito"), \
                patch.object(plan, "_es_agencia", return_value=True):
            self.assertIsNone(plan.exigir_plan_minimo("enterprise"))


class TestRedesGate(FrappeTestCase):
    """D-4: Redes sociales exige Lite+ y el gate corta ANTES de tocar config."""

    def test_gratuito_no_abre_la_conexion(self):
        with patch.object(plan, "plan_de_este_sitio", return_value="gratuito"), \
                patch.object(plan, "_es_agencia", return_value=False), \
                patch.object(redes_sociales, "_cfg") as cfg:
            with self.assertRaises(frappe.PermissionError):
                redes_sociales.generar_link_conexion()
            cfg.assert_not_called()

    def test_gratuito_no_lee_el_estado(self):
        with patch.object(plan, "plan_de_este_sitio", return_value="gratuito"), \
                patch.object(plan, "_es_agencia", return_value=False), \
                patch.object(redes_sociales, "_cfg") as cfg:
            with self.assertRaises(frappe.PermissionError):
                redes_sociales.estado()
            cfg.assert_not_called()

    def test_lite_pasa_el_gate(self):
        # Lite sí entra: con el módulo apagado `estado()` responde "apagado",
        # prueba de que llegó más allá del gate (que si no, ya habría lanzado
        # PermissionError antes de tocar `_cfg`).
        with patch.object(plan, "plan_de_este_sitio", return_value="lite"), \
                patch.object(plan, "_es_agencia", return_value=False), \
                patch.object(redes_sociales, "validate_role"), \
                patch.object(redes_sociales, "_cfg") as cfg:
            cfg.return_value = frappe._dict(enabled=0)
            out = redes_sociales.estado()
            self.assertFalse(out["activo"])
            cfg.assert_called_once()
