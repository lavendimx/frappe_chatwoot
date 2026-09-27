# Copyright (c) 2026, lavendi.mx
# License: MIT
"""Tests del job de campañas de email (`utils/campana_email.py`).

Foco: el gate por plan del SITIO. El scheduler corre como Administrator (y
`plan.exigir_plan_minimo` exime a la agencia), así que sin este gate un sitio
Lite con una `Campana Email` activa mandaba correo igual. Con plan < `pro` el
job es no-op silencioso (dict con `motivo`), nunca una excepción.

Sin red ni scheduler: `frappe.db` y `frappe.get_all` se mockean.
"""

from unittest.mock import patch

from frappe.tests.utils import FrappeTestCase

from frappe_chatwoot.utils import campana_email


class TestPlanGateJob(FrappeTestCase):
    """`avanzar` respeta el plan del sitio, no el del usuario."""

    def _avanzar(self, plan_sitio, campanas=0, campanas_list=None):
        with patch.object(campana_email.plan, "plan_de_este_sitio",
                          return_value=plan_sitio), \
                patch.object(campana_email.frappe.db, "get_single_value",
                             return_value=campanas) as gsv, \
                patch.object(campana_email.frappe, "get_all",
                             return_value=campanas_list or []) as gall:
            return campana_email.avanzar(), gsv, gall

    def test_lite_es_no_op_y_no_toca_la_base(self):
        result, gsv, gall = self._avanzar("lite")
        self.assertEqual(result, {"activo": False, "motivo": "plan"})
        gsv.assert_not_called()
        gall.assert_not_called()

    def test_gratuito_es_no_op(self):
        result, _, _ = self._avanzar("gratuito")
        self.assertEqual(result.get("motivo"), "plan")

    def test_pro_procede(self):
        # Llega al trabajo real (lee el freno global y lista campañas); el
        # `None` es el freno normal apagado, no el no-op de plan.
        result, gsv, gall = self._avanzar("pro", campanas=1)
        self.assertIsNone(result)
        gsv.assert_called_once()
        gall.assert_called_once()

    def test_enterprise_procede(self):
        result, gsv, gall = self._avanzar("enterprise", campanas=1)
        self.assertIsNone(result)
        gsv.assert_called_once()
        gall.assert_called_once()
