# Copyright (c) 2026, lavendi.mx
# License: MIT
"""Tests de `api/meta_conexion.py::completar_wiring()` — el wiring automático
(best-effort) que corre tras conectar una página de Messenger/Instagram a
Chatwoot (pasos 1-4 del `runbook-conectar-redes.md`: canal en bandeja, aviso
a `agente-ia`, colaborador del inbox, clonar `Agente IA`).

Mismo criterio que `utils/test_seguimiento_cotizado.py`: nada aquí manda una
petición HTTP real (ni a Chatwoot ni a `agente-ia`) ni toca la BD de verdad —
todo lo que `completar_wiring` llama (`frappe.*`, `cw.*`, `urllib.request`) va
mockeado.

`_agregar_canal_bandeja` (paso 1 — escritura local simple en
`Chatwoot Settings.canales_bandeja`) se deja mockeada como no-op en TODOS los
casos de `TestCompletarWiring`: no es el riesgo que esta tarea busca cubrir
(sin dependencias externas, su propio docstring no documenta ningún punto de
falla relevante) y dejarla correr contra un `frappe.get_single` mockeado a
medias ensuciaría las aserciones de `frappe.log_error` de los otros 3 pasos
con una falla ajena al caso bajo prueba. Queda cubierta aparte en
`TestAgregarCanalBandeja`.

El contrato bajo prueba: NINGÚN sub-paso (`_registrar_canal_en_agente`,
`_agregar_colaborador_inbox`, `_clonar_agente_ia` — los 3 que sí tienen un
punto de falla externo real) puede tumbar `completar_wiring()`, que siempre
debe devolver `{"inbox_id": ..., "inbox_origen": ...}`.
"""

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from frappe_chatwoot.frappe_chatwoot.api import meta_conexion as mc
from frappe_chatwoot.utils import chatwoot_client as cw

INBOX_ID = "22"
INBOX_ORIGEN = "5"

CONFIG_AGENDA_OK = {"url": "http://agente-ia.local:8095", "token": "tok-123"}


def _agente_doc(system_prompt="Eres Sofía Ventas, el agente de WhatsApp de lavendi.mx.",
                 tiene_followup_activo=True):
    """Doc de prueba para `Agente IA`, con `.meta.has_field()` controlable —
    mismo patrón que el helper `_deal()` de test_seguimiento_cotizado.py."""
    doc = MagicMock()
    doc.system_prompt = system_prompt
    doc.meta.has_field.return_value = tiene_followup_activo
    return doc


def _exists_solo_para(nombre_que_existe: str):
    """`frappe.db.exists("Agente IA", name)` de prueba: True solo para el
    `name` indicado, False para cualquier otro — evita que un único
    `return_value` fijo confunda las dos llamadas distintas que hace
    `_clonar_agente_ia` (una por `inbox_id`, otra por `inbox_origen`)."""
    return lambda doctype, name: name == nombre_que_existe


class TestCompletarWiring(FrappeTestCase):
    """`completar_wiring()` orquestando sus 4 sub-pasos best-effort."""

    def test_camino_feliz_completa_los_3_pasos_externos_y_devuelve_el_dict_esperado(self):
        """Con todo configurado y respondiendo bien: colaborador agregado,
        Agente IA clonado, aviso a agente-ia enviado, sin ningún
        `frappe.log_error` — y el dict de retorno de siempre."""
        origen = _agente_doc()
        nuevo = _agente_doc()

        with patch.object(mc, "_agregar_canal_bandeja"), \
             patch.object(mc, "_config_agenda", return_value=CONFIG_AGENDA_OK), \
             patch("urllib.request.urlopen") as urlopen, \
             patch.object(cw, "agent_id_for_email", return_value=99) as agent_id_for_email, \
             patch.object(cw, "_post") as post, \
             patch.object(mc.frappe.db, "exists", side_effect=_exists_solo_para(INBOX_ORIGEN)), \
             patch.object(mc.frappe, "get_doc", return_value=origen), \
             patch.object(mc.frappe, "copy_doc", return_value=nuevo), \
             patch.object(mc.frappe.db, "commit"), \
             patch.object(mc.frappe, "log_error") as log_error:
            urlopen.return_value.__enter__.return_value.read.return_value = b"{}"

            resultado = mc.completar_wiring(INBOX_ID, inbox_origen=INBOX_ORIGEN,
                                             canal_nombre="Instagram")

        self.assertEqual(resultado, {"inbox_id": INBOX_ID, "inbox_origen": INBOX_ORIGEN})
        agent_id_for_email.assert_called_once_with("agente-ia@lavendi.mx")
        post.assert_called_once_with("/inbox_members", {"inbox_id": 22, "user_ids": [99]})
        nuevo.insert.assert_called_once_with(ignore_permissions=True)
        log_error.assert_not_called()

    def test_403_al_agregar_colaborador_no_propaga_y_los_demas_pasos_siguen(self):
        """(2) Simula el 403 documentado en el docstring de
        `_agregar_colaborador_inbox` (el `api_token` de Chatwoot Settings no
        es de un usuario `administrator`): `cw._post` lanza SOLO en esta
        llamada — no hay otro `cw._post` en el camino de `completar_wiring`
        (el paso 1 va mockeado aparte, `_registrar_canal_en_agente` usa
        `urllib.request`, `_clonar_agente_ia` no usa `cw` para nada), así que
        aislar la falla no requiere lógica adicional."""
        origen = _agente_doc()
        nuevo = _agente_doc()

        with patch.object(mc, "_agregar_canal_bandeja"), \
             patch.object(mc, "_config_agenda", return_value=CONFIG_AGENDA_OK), \
             patch("urllib.request.urlopen") as urlopen, \
             patch.object(cw, "agent_id_for_email", return_value=99), \
             patch.object(cw, "_post",
                          side_effect=cw.ChatwootAPIError("Chatwoot: not authorized (403)")) as post, \
             patch.object(mc.frappe.db, "exists", side_effect=_exists_solo_para(INBOX_ORIGEN)), \
             patch.object(mc.frappe, "get_doc", return_value=origen), \
             patch.object(mc.frappe, "copy_doc", return_value=nuevo), \
             patch.object(mc.frappe.db, "commit"), \
             patch.object(mc.frappe, "log_error") as log_error:
            urlopen.return_value.__enter__.return_value.read.return_value = b"{}"

            resultado = mc.completar_wiring(INBOX_ID, inbox_origen=INBOX_ORIGEN)

        self.assertEqual(resultado, {"inbox_id": INBOX_ID, "inbox_origen": INBOX_ORIGEN})
        post.assert_called_once()
        log_error.assert_called_once()
        self.assertIn("colaborador", log_error.call_args.kwargs.get("title", ""))
        # El paso 4 (clonar) no depende del 3 (colaborador) — el 403 de uno
        # no debe impedir que el otro corra.
        nuevo.insert.assert_called_once_with(ignore_permissions=True)

    def test_agent_id_for_email_none_no_intenta_el_post_y_deja_constancia(self):
        """(3) `agente-ia@lavendi.mx` no es agente de Chatwoot (`GET /agents`
        no lo encuentra): ni siquiera se intenta `cw._post("/inbox_members")`
        — el docstring de `_agregar_colaborador_inbox` es explícito en que
        sin `user_id` no hay nada que postear."""
        origen = _agente_doc()
        nuevo = _agente_doc()

        with patch.object(mc, "_agregar_canal_bandeja"), \
             patch.object(mc, "_config_agenda", return_value=CONFIG_AGENDA_OK), \
             patch("urllib.request.urlopen") as urlopen, \
             patch.object(cw, "agent_id_for_email", return_value=None) as agent_id_for_email, \
             patch.object(cw, "_post") as post, \
             patch.object(mc.frappe.db, "exists", side_effect=_exists_solo_para(INBOX_ORIGEN)), \
             patch.object(mc.frappe, "get_doc", return_value=origen), \
             patch.object(mc.frappe, "copy_doc", return_value=nuevo), \
             patch.object(mc.frappe.db, "commit"), \
             patch.object(mc.frappe, "log_error") as log_error:
            urlopen.return_value.__enter__.return_value.read.return_value = b"{}"

            resultado = mc.completar_wiring(INBOX_ID, inbox_origen=INBOX_ORIGEN)

        self.assertEqual(resultado, {"inbox_id": INBOX_ID, "inbox_origen": INBOX_ORIGEN})
        agent_id_for_email.assert_called_once_with("agente-ia@lavendi.mx")
        post.assert_not_called()
        log_error.assert_called_once()
        self.assertIn("no es agente", log_error.call_args.kwargs.get("title", ""))

    def test_clonar_agente_ia_idempotente_si_ya_existe_no_llama_copy_doc_ni_insert(self):
        """(4) `frappe.db.exists("Agente IA", inbox_id)` ya es True (conexión
        repetida, o `completar_wiring` llamado dos veces para el mismo
        inbox): no se clona de nuevo — ni `frappe.get_doc` (del origen) ni
        `frappe.copy_doc` ni `.insert()` se llegan a invocar, solo se
        loggea."""
        with patch.object(mc, "_agregar_canal_bandeja"), \
             patch.object(mc, "_config_agenda", return_value=CONFIG_AGENDA_OK), \
             patch("urllib.request.urlopen") as urlopen, \
             patch.object(cw, "agent_id_for_email", return_value=99), \
             patch.object(cw, "_post"), \
             patch.object(mc.frappe.db, "exists", side_effect=_exists_solo_para(INBOX_ID)), \
             patch.object(mc.frappe, "get_doc") as get_doc, \
             patch.object(mc.frappe, "copy_doc") as copy_doc, \
             patch.object(mc.frappe.db, "commit"), \
             patch.object(mc.frappe, "log_error") as log_error:
            urlopen.return_value.__enter__.return_value.read.return_value = b"{}"

            resultado = mc.completar_wiring(INBOX_ID, inbox_origen=INBOX_ORIGEN)

        self.assertEqual(resultado, {"inbox_id": INBOX_ID, "inbox_origen": INBOX_ORIGEN})
        get_doc.assert_not_called()
        copy_doc.assert_not_called()
        log_error.assert_called_once()
        self.assertIn("ya existía", log_error.call_args.kwargs.get("title", ""))

    def test_falla_de_red_al_registrar_canal_en_agente_no_propaga(self):
        """(5) `urllib.request.urlopen` lanza (proceso `agente-ia` caído, o
        la red simplemente falla) — mismo criterio que el resto: se loggea
        y el inbox ya creado en Chatwoot no se revierte ni se bloquea el
        resto del wiring."""
        origen = _agente_doc()
        nuevo = _agente_doc()

        with patch.object(mc, "_agregar_canal_bandeja"), \
             patch.object(mc, "_config_agenda", return_value=CONFIG_AGENDA_OK), \
             patch("urllib.request.urlopen", side_effect=OSError("Connection refused")), \
             patch.object(cw, "agent_id_for_email", return_value=99), \
             patch.object(cw, "_post") as post, \
             patch.object(mc.frappe.db, "exists", side_effect=_exists_solo_para(INBOX_ORIGEN)), \
             patch.object(mc.frappe, "get_doc", return_value=origen), \
             patch.object(mc.frappe, "copy_doc", return_value=nuevo), \
             patch.object(mc.frappe.db, "commit"), \
             patch.object(mc.frappe, "log_error") as log_error:
            resultado = mc.completar_wiring(INBOX_ID, inbox_origen=INBOX_ORIGEN)

        self.assertEqual(resultado, {"inbox_id": INBOX_ID, "inbox_origen": INBOX_ORIGEN})
        log_error.assert_called_once()
        self.assertIn("no respondió", log_error.call_args.kwargs.get("title", ""))
        # Los pasos 3 y 4, independientes del aviso a agente-ia, deben seguir
        # corriendo igual.
        post.assert_called_once()
        nuevo.insert.assert_called_once_with(ignore_permissions=True)

    def test_agenda_sin_configurar_no_intenta_la_red_y_deja_constancia(self):
        """Caso adicional: `Chatwoot Settings` sin `agenda_url`/`agenda_token`
        — ni se construye el `Request` ni se llama `urlopen`, se loggea con
        un título distinto al de la falla de red (para que el mensaje en
        `frappe.log_error` oriente a configurar, no a revisar la red)."""
        origen = _agente_doc()
        nuevo = _agente_doc()

        with patch.object(mc, "_agregar_canal_bandeja"), \
             patch.object(mc, "_config_agenda", return_value={"url": "", "token": ""}), \
             patch("urllib.request.urlopen") as urlopen, \
             patch.object(cw, "agent_id_for_email", return_value=99), \
             patch.object(cw, "_post"), \
             patch.object(mc.frappe.db, "exists", side_effect=_exists_solo_para(INBOX_ORIGEN)), \
             patch.object(mc.frappe, "get_doc", return_value=origen), \
             patch.object(mc.frappe, "copy_doc", return_value=nuevo), \
             patch.object(mc.frappe.db, "commit"), \
             patch.object(mc.frappe, "log_error") as log_error:
            resultado = mc.completar_wiring(INBOX_ID, inbox_origen=INBOX_ORIGEN)

        self.assertEqual(resultado, {"inbox_id": INBOX_ID, "inbox_origen": INBOX_ORIGEN})
        urlopen.assert_not_called()
        log_error.assert_called_once()
        self.assertIn("sin configurar", log_error.call_args.kwargs.get("title", ""))

    def test_inbox_id_vacio_si_lanza(self):
        """Contraste con los 4 sub-pasos (todos best-effort): la validación
        de entrada de `completar_wiring` NO es best-effort — un `inbox_id`
        vacío es un error de quien llama, no un best-effort que deba
        tragarse en silencio."""
        with self.assertRaises(frappe.ValidationError):
            mc.completar_wiring("")


class TestAgregarCanalBandeja(FrappeTestCase):
    """`_agregar_canal_bandeja()` — paso 1, aislado de `completar_wiring`.

    A diferencia de los otros 3 pasos, este no tiene ninguna dependencia
    externa (solo lee/escribe el doc Single `Chatwoot Settings`), así que se
    mockea con un `_dict` en vez de un `MagicMock` — permite que
    `(settings.canales_bandeja or "").split(",")` opere sobre un string real
    en vez de sobre otro Mock."""

    def _settings(self, canales_bandeja="", default_inbox_id=0):
        settings = frappe._dict(canales_bandeja=canales_bandeja, default_inbox_id=default_inbox_id)
        settings.save = MagicMock()
        return settings

    def test_primera_vez_siembra_el_default_antes_de_sumar_el_nuevo(self):
        """Campo vacío + `default_inbox_id` configurado: el default (ej. el
        WhatsApp de siempre) se siembra explícito antes de agregar el inbox
        nuevo, para que "Todos los canales" no deje de incluirlo."""
        settings = self._settings(canales_bandeja="", default_inbox_id=5)
        with patch.object(mc.frappe, "get_single", return_value=settings), \
             patch.object(mc.frappe, "db") as db, \
             patch.object(mc.frappe.utils, "cint", side_effect=lambda v: int(v or 0)):
            mc._agregar_canal_bandeja("22")

        self.assertEqual(settings.canales_bandeja, "5,22")
        settings.save.assert_called_once_with(ignore_permissions=True)
        db.commit.assert_called_once()

    def test_inbox_ya_presente_no_duplica_ni_guarda(self):
        settings = self._settings(canales_bandeja="5,12,22")
        with patch.object(mc.frappe, "get_single", return_value=settings), \
             patch.object(mc.frappe, "db") as db:
            mc._agregar_canal_bandeja("22")

        self.assertEqual(settings.canales_bandeja, "5,12,22")
        settings.save.assert_not_called()
        db.commit.assert_not_called()
