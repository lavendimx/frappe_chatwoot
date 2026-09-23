# Copyright (c) 2026, lavendi.mx
# License: MIT
"""Tests del motor de secuencias (`utils/secuencias.py`).

Cubre lo que se puede ejercitar sin mandar WhatsApp ni correo:

  - `_dentro_de_ventana` / `_siguiente_hueco` — ventana L-V 12:00-18:00.
  - `_debe_salir` — salidas por cambio de etapa (doble vocabulario
    `ghl_status`/`status`), por respuesta, el puente multi-contacto y el
    posponer cuando Chatwoot no responde.
  - `avanzar` — selección/orden del lote vencido y el freno de ráfaga "del
    canal, no del motor" (commit ca397b9): el tope y el delay solo aplican a
    pasos de envío, los pasos internos avanzan sin gastar plaza.

Ninguna prueba realiza efectos de red: `_ejecutar_paso`, `_debe_salir`,
`_set_ins` y la capa `frappe.db` se mockean.
"""

import datetime as dt
from contextlib import ExitStack
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from frappe_chatwoot.utils import secuencias

# Miércoles 2026-09-23 — `isoweekday() == 3`, dentro de la ventana L-V.
MIERCOLES = dt.datetime(2026, 9, 23, 13, 0)
VIERNES = dt.datetime(2026, 9, 25, 13, 0)
SABADO = dt.datetime(2026, 9, 26, 13, 0)


def _sec(**over):
    base = {
        "name": "SEC-TEST",
        "activa": 1,
        "horario_habil": 0,
        "max_por_corrida": None,
        "pasos": [],
    }
    base.update(over)
    return base


def _ins(name, secuencia="SEC-TEST", deal="DEAL-TEST", paso=0,
         contacto="CONT-TEST", conversation_id="", ultimo_envio_at=None):
    return frappe._dict({
        "name": name,
        "secuencia": secuencia,
        "deal": deal,
        "contacto": contacto,
        "conversation_id": conversation_id,
        "paso_actual": paso,
        "ultimo_envio_at": ultimo_envio_at,
    })


def _avanzar_mock(inscripciones, sec, debe_salir=None, ejecutar=None):
    """Corre `avanzar()` sin tocar DB, Chatwoot ni reloj. Devuelve
    `(resultado, info)` donde `info` expone los mocks de interés."""
    ejecutados = []
    seteados = []

    def _fake_ejecutar(ins, paso, _sec):
        ejecutados.append(ins["name"])
        return "ok"

    def _fake_set_ins(name, campos, **kw):
        seteados.append((name, campos))

    with ExitStack() as stack:
        stack.enter_context(patch.object(secuencias, "_activo", return_value=True))
        stack.enter_context(patch.object(secuencias, "_ahora", return_value=MIERCOLES))
        g_all = stack.enter_context(
            patch.object(secuencias.frappe, "get_all", return_value=inscripciones))
        g_doc = stack.enter_context(patch.object(secuencias.frappe, "get_doc"))
        db_set = stack.enter_context(patch.object(secuencias.frappe.db, "set_value"))
        stack.enter_context(patch.object(secuencias.frappe.db, "commit"))
        stack.enter_context(
            patch.object(secuencias, "_dentro_de_ventana", return_value=True))
        stack.enter_context(
            patch.object(secuencias, "_debe_salir", return_value=debe_salir))
        stack.enter_context(
            patch.object(secuencias, "_ejecutar_paso",
                         side_effect=ejecutar or _fake_ejecutar))
        stack.enter_context(
            patch.object(secuencias, "_set_ins", side_effect=_fake_set_ins))
        g_doc.return_value.as_dict.return_value = sec
        result = secuencias.avanzar()

    return result, {"get_all": g_all, "db_set": db_set,
                    "ejecutados": ejecutados, "seteados": seteados}


class TestVentanaHoraria(FrappeTestCase):
    """`_dentro_de_ventana` — L-V 12:00-18:00, fin exclusivo."""

    def _sec_ventana(self, **over):
        return _sec(horario_habil=1, ventana_dias="1,2,3,4,5",
                    ventana_inicio="12:00", ventana_fin="18:00", **over)

    def test_dentro_en_dia_habil_horario(self):
        self.assertTrue(secuencias._dentro_de_ventana(self._sec_ventana(), MIERCOLES))

    def test_fuera_antes_de_inicio(self):
        self.assertFalse(secuencias._dentro_de_ventana(
            self._sec_ventana(), MIERCOLES.replace(hour=11, minute=59)))

    def test_limite_superior_exclusivo(self):
        sec = self._sec_ventana()
        self.assertTrue(secuencias._dentro_de_ventana(sec, MIERCOLES.replace(hour=17, minute=59)))
        self.assertFalse(secuencias._dentro_de_ventana(sec, MIERCOLES.replace(hour=18, minute=0)))

    def test_fuera_en_fin_de_semana(self):
        self.assertFalse(secuencias._dentro_de_ventana(self._sec_ventana(), SABADO))

    def test_horario_no_habil_abre_siempre(self):
        sec = _sec(horario_habil=0)
        self.assertTrue(secuencias._dentro_de_ventana(sec, SABADO.replace(hour=3)))

    def test_defaults_sin_campos_de_ventana(self):
        # Sin `ventana_dias` el default es L-V; sin horas, 12:00-18:00.
        sec = _sec(horario_habil=1)
        self.assertTrue(secuencias._dentro_de_ventana(sec, MIERCOLES))
        self.assertFalse(secuencias._dentro_de_ventana(sec, SABADO))


class TestSiguienteHueco(FrappeTestCase):
    """`_siguiente_hueco` — primer instante >= `desde` dentro de la ventana."""

    def _sec_ventana(self):
        return _sec(horario_habil=1, ventana_dias="1,2,3,4,5",
                    ventana_inicio="12:00", ventana_fin="18:00")

    def test_ya_dentro_lo_devuelve_igual(self):
        self.assertEqual(
            secuencias._siguiente_hueco(self._sec_ventana(), MIERCOLES), MIERCOLES)

    def test_antes_de_inicio_salta_al_inicio_del_mismo_dia(self):
        desde = MIERCOLES.replace(hour=9, minute=30)
        self.assertEqual(
            secuencias._siguiente_hueco(self._sec_ventana(), desde),
            MIERCOLES.replace(hour=12, minute=0, second=0, microsecond=0),
        )

    def test_despues_del_fin_salta_al_dia_habil_siguiente(self):
        desde = MIERCOLES.replace(hour=18, minute=30)
        self.assertEqual(
            secuencias._siguiente_hueco(self._sec_ventana(), desde),
            dt.datetime(2026, 9, 24, 12, 0),
        )

    def test_viernes_de_noche_salta_el_fin_de_semana(self):
        desde = VIERNES.replace(hour=19, minute=0)
        # Sábado y domingo no son hábiles; el primer hueco es el lunes.
        self.assertEqual(
            secuencias._siguiente_hueco(self._sec_ventana(), desde),
            dt.datetime(2026, 9, 28, 12, 0),
        )

    def test_horario_no_habil_devuelve_el_mismo_instante(self):
        desde = SABADO.replace(hour=3, minute=7)
        self.assertEqual(secuencias._siguiente_hueco(_sec(horario_habil=0), desde), desde)


class TestDebeSalir(FrappeTestCase):
    """`_debe_salir` — ramas de salida/posponer sin tocar Chatwoot real."""

    def _patch_db(self, deal_estado=None, parar=0, hermana=None):
        def fake(doctype, *args, **kwargs):
            if doctype == "CRM Deal":
                return deal_estado
            if doctype == "Secuencia":
                return parar
            if doctype == "Secuencia Inscripcion":
                return hermana
            return None
        return patch.object(secuencias.frappe.db, "get_value", side_effect=fake)

    def test_sale_si_ghl_status_ganado(self):
        ins = _ins("INS-1")
        with self._patch_db(deal_estado={"ghl_status": "won", "status": None}):
            self.assertEqual(secuencias._debe_salir(ins),
                             "la oportunidad pasó a won (ghl_status)")

    def test_sale_por_status_frappe_aunque_ghl_status_este_abierto(self):
        # Doble vocabulario: basta que UNO diga perdida.
        ins = _ins("INS-1")
        with self._patch_db(deal_estado={"ghl_status": "open", "status": "Lost"}):
            self.assertEqual(secuencias._debe_salir(ins),
                             "la oportunidad pasó a lost (status)")

    def test_no_sale_si_la_oportunidad_sigue_abierta(self):
        ins = _ins("INS-1")
        with self._patch_db(deal_estado={"ghl_status": "open", "status": "Open"}, parar=0):
            self.assertIsNone(secuencias._debe_salir(ins))

    def test_sale_si_otro_contacto_de_la_oportunidad_respondio(self):
        ins = _ins("INS-1")
        with self._patch_db(deal_estado={"ghl_status": "open", "status": "Open"},
                            parar=1, hermana=frappe._dict({"name": "INS-2", "contacto": "CONT-2"})):
            motivo = secuencias._debe_salir(ins)
        self.assertIn("otro contacto de la oportunidad respondió", motivo)
        self.assertIn("CONT-2", motivo)

    def test_sale_si_el_contacto_respondio_despues_del_ultimo_envio(self):
        ins = _ins("INS-1", conversation_id="77",
                   ultimo_envio_at=dt.datetime(2026, 9, 23, 13, 0))
        epoca = dt.datetime(2026, 9, 23, 14, 0).timestamp()
        with self._patch_db(deal_estado={"ghl_status": "open", "status": "Open"}, parar=1), \
                patch.object(secuencias.cw, "list_messages",
                             return_value={"payload": [{"message_type": 0, "created_at": epoca}]}), \
                patch.object(secuencias.frappe.db, "set_value") as set_value, \
                patch.object(secuencias, "_avisar_respondio") as avisar:
            self.assertEqual(secuencias._debe_salir(ins), "el contacto respondió")
        # Deja constancia de cuándo respondió y avisa al equipo.
        self.assertTrue(set_value.called)
        avisar.assert_called_once()

    def test_no_sale_si_el_ultimo_mensaje_es_anterior_al_envio(self):
        ins = _ins("INS-1", conversation_id="77",
                   ultimo_envio_at=dt.datetime(2026, 9, 23, 13, 0))
        epoca = dt.datetime(2026, 9, 23, 12, 0).timestamp()
        with self._patch_db(deal_estado={"ghl_status": "open", "status": "Open"}, parar=1), \
                patch.object(secuencias.cw, "list_messages",
                             return_value={"payload": [{"message_type": 0, "created_at": epoca}]}):
            self.assertIsNone(secuencias._debe_salir(ins))

    def test_pospone_si_chatwoot_falla(self):
        ins = _ins("INS-1", conversation_id="77",
                   ultimo_envio_at=dt.datetime(2026, 9, 23, 13, 0))
        with self._patch_db(deal_estado={"ghl_status": "open", "status": "Open"}, parar=1), \
                patch.object(secuencias.cw, "list_messages",
                             side_effect=RuntimeError("chatwoot down")), \
                patch.object(secuencias.frappe, "log_error") as log_error:
            self.assertEqual(secuencias._debe_salir(ins), "__posponer__")
        self.assertTrue(log_error.called)

    def test_sin_conversacion_no_revisa_respuesta(self):
        ins = _ins("INS-1", conversation_id="",
                   ultimo_envio_at=dt.datetime(2026, 9, 23, 13, 0))
        with self._patch_db(deal_estado={"ghl_status": "open", "status": "Open"}, parar=1):
            self.assertIsNone(secuencias._debe_salir(ins))


class TestAvanzarFrenoDeRafaga(FrappeTestCase):
    """`avanzar` — selección/orden y freno de ráfaga por canal (ca397b9)."""

    def test_seleccion_pide_desempate_estable(self):
        _, m = _avanzar_mock([], _sec())
        m["get_all"].assert_called_once()
        self.assertEqual(m["get_all"].call_args.kwargs["order_by"],
                         "proximo_en asc, name asc")

    def test_pasos_internos_avanzan_sin_gastar_plaza(self):
        # tope=1 pero 3 inscripciones en un paso interno: NINGUNA se salta.
        sec = _sec(max_por_corrida=1, pasos=[{"tipo": "Actualizar oportunidad"}])
        result, m = _avanzar_mock(
            [_ins("INS-1"), _ins("INS-2"), _ins("INS-3")], sec)
        self.assertEqual(result["ejecutados"], 3)
        self.assertEqual(m["ejecutados"], ["INS-1", "INS-2", "INS-3"])

    def test_envios_si_consumen_la_plaza(self):
        # tope=1: solo el primer envío sale; los otros dos se saltan sin avanzar.
        sec = _sec(max_por_corrida=1, pasos=[{"tipo": "Email"}])
        result, m = _avanzar_mock(
            [_ins("INS-1"), _ins("INS-2"), _ins("INS-3")], sec)
        self.assertEqual(result["ejecutados"], 1)
        self.assertEqual(m["ejecutados"], ["INS-1"])

    def test_delay_solo_entre_whatsapps_no_antes_del_primero(self):
        sec = _sec(max_por_corrida=None, pasos=[{"tipo": "WhatsApp"}])
        with patch.object(secuencias.time, "sleep") as dormir, \
                patch.object(secuencias.random, "uniform", return_value=1.0) as uniforme:
            result, _ = _avanzar_mock(
                [_ins("INS-1"), _ins("INS-2"), _ins("INS-3")], sec)
        self.assertEqual(result["ejecutados"], 3)
        # 3 envíos -> 2 esperas (nunca antes del primero).
        self.assertEqual(dormir.call_count, 2)
        self.assertEqual(uniforme.call_args.args, secuencias.DELAY_ENTRE_ENVIOS_SEG)

    def test_tope_default_cuando_el_campo_viene_vacio(self):
        sec = _sec(max_por_corrida=None,
                   pasos=[{"tipo": "WhatsApp"}] * secuencias.MAX_CORRIDA_DEFAULT)
        inscripciones = [_ins(f"INS-{i}", paso=0) for i in range(secuencias.MAX_CORRIDA_DEFAULT + 5)]
        with patch.object(secuencias.time, "sleep"), \
                patch.object(secuencias.random, "uniform", return_value=1.0):
            result, _ = _avanzar_mock(inscripciones, sec)
        self.assertEqual(result["ejecutados"], secuencias.MAX_CORRIDA_DEFAULT)

    def test_ventana_cerrada_pospone_y_no_ejecuta(self):
        sec = _sec(pasos=[{"tipo": "Email"}])
        with patch.object(secuencias, "_activo", return_value=True), \
                patch.object(secuencias, "_ahora", return_value=MIERCOLES), \
                patch.object(secuencias.frappe, "get_all",
                             return_value=[_ins("INS-1")]), \
                patch.object(secuencias.frappe, "get_doc") as g_doc, \
                patch.object(secuencias.frappe.db, "set_value") as db_set, \
                patch.object(secuencias.frappe.db, "commit"), \
                patch.object(secuencias, "_dentro_de_ventana", return_value=False), \
                patch.object(secuencias, "_ejecutar_paso") as ejecutar:
            g_doc.return_value.as_dict.return_value = sec
            result = secuencias.avanzar()
        self.assertEqual(result["pospuestos"], 1)
        self.assertEqual(result["ejecutados"], 0)
        ejecutar.assert_not_called()
        self.assertTrue(db_set.called)

    def test_salida_por_cambio_de_etapa(self):
        motivo = "la oportunidad pasó a lost (status)"
        result, m = _avanzar_mock([_ins("INS-1")], _sec(), debe_salir=motivo)
        self.assertEqual(result["salidas"], 1)
        self.assertEqual(result["ejecutados"], 0)
        name, campos = m["seteados"][0]
        self.assertEqual(campos["estado"], "Salió por cambio de etapa")
        self.assertEqual(campos["motivo"], motivo)

    def test_salida_por_respuesta_marca_estado_correcto(self):
        result, m = _avanzar_mock([_ins("INS-1")], _sec(),
                                  debe_salir="el contacto respondió")
        self.assertEqual(result["salidas"], 1)
        self.assertEqual(m["seteados"][0][1]["estado"], "Salió por respuesta")

    def test_posponer_reagenda_30_min(self):
        result, _ = _avanzar_mock([_ins("INS-1")], _sec(), debe_salir="__posponer__")
        self.assertEqual(result["pospuestos"], 1)
        self.assertEqual(result["ejecutados"], 0)

    def test_secuencia_completa_termina(self):
        sec = _sec(pasos=[{"tipo": "Email"}])
        result, m = _avanzar_mock([_ins("INS-1", paso=1)], sec)
        self.assertEqual(m["seteados"][0][1]["estado"], "Terminada")
        self.assertEqual(result["ejecutados"], 0)

    def test_secuencia_inactiva_se_salta(self):
        sec = _sec(activa=0, pasos=[{"tipo": "Email"}])
        result, m = _avanzar_mock([_ins("INS-1")], sec)
        self.assertEqual(result["ejecutados"], 0)
        self.assertEqual(m["ejecutados"], [])

    def test_constantes_del_contrato_de_freno(self):
        self.assertEqual(secuencias.MAX_CORRIDA_DEFAULT, 20)
        self.assertEqual(secuencias.DELAY_ENTRE_ENVIOS_SEG, (60, 120))
        self.assertEqual(secuencias.ESTADOS_QUE_SACAN, ("won", "lost", "abandoned"))
