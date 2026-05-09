# -*- coding: utf-8 -*-
import os
import copy
import re
import logging
import json
import requests
import requests.exceptions
import random
import ask_sdk_core.utils as ask_utils

from ask_sdk_core.skill_builder import SkillBuilder
from ask_sdk_core.dispatch_components import AbstractRequestHandler, AbstractExceptionHandler
from ask_sdk_core.handler_input import HandlerInput
from ask_sdk_model.interfaces.alexa.presentation.apl import RenderDocumentDirective, ExecuteCommandsDirective, OpenUrlCommand
from ask_sdk_model import Response
from datetime import datetime, timezone

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

_APL_TOKEN = "ha_dashboard"


def load_config() -> dict:
    config: dict = {}
    try:
        with open("config.cfg", encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or '=' not in line:
                    continue
                name, value = line.split('=', 1)
                config[name] = value
    except Exception as e:
        logger.error(f"Error loading config: {str(e)}")
    return config


config = load_config()

home_assistant_url = config.get("home_assistant_url")
home_assistant_token = config.get("home_assistant_token")
home_assistant_agent_id = config.get("home_assistant_agent_id")
home_assistant_language = config.get("home_assistant_language")
# bool("False") == True — compare strings explicitly
home_assistant_room_recognition = config.get("home_assistant_room_recognition", "false").lower() == "true"
home_assistant_dashboard = config.get("home_assistant_dashboard") or "lovelace"
home_assistant_kioskmode = config.get("home_assistant_kioskmode", "false").lower() == "true"

if not home_assistant_url or not home_assistant_token or not home_assistant_agent_id or not home_assistant_language:
    raise ValueError("Required configuration missing in config.cfg")

# Module-level HTTP session — amortises TLS handshake across warm invocations
_http_session = requests.Session()
_http_session.headers.update({
    "Authorization": f"Bearer {home_assistant_token}",
    "Content-Type": "application/json",
})

# Locale string cache — keyed by locale code; eliminates per-request disk reads
_locale_cache: dict[str, dict[str, str]] = {}


def load_localization(locale: str) -> None:
    if locale not in _locale_cache:
        file_name = f"locale/{locale}.lang"
        if not os.path.exists(file_name):
            file_name = "locale/en-US.lang"
        locale_data: dict[str, str] = {}
        try:
            with open(file_name, encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line or '=' not in line:
                        continue
                    name, value = line.split('=', 1)
                    locale_data[name] = value
        except Exception as e:
            logger.error(f"Error loading locale file: {str(e)}")
        _locale_cache[locale] = locale_data
    globals().update(_locale_cache[locale])


# Pre-load default locale at cold start
load_localization("en-US")

# APL template cache — loaded once at cold start; deep-copied before mutation per request
_APL_TEMPLATES: dict[str, dict] = {}
for _tpl in ("apl_openha.json", "apl_empty.json"):
    try:
        with open(_tpl, encoding='utf-8') as _f:
            _APL_TEMPLATES[_tpl] = json.load(_f)
    except Exception as _e:
        logger.error(f"Error loading APL template {_tpl}: {str(_e)}")


class LaunchRequestHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return ask_utils.is_request_type("LaunchRequest")(handler_input)

    def handle(self, handler_input):
        locale = handler_input.request_envelope.request.locale
        load_localization(locale)

        session_attrs = handler_input.attributes_manager.session_attributes
        now = datetime.now(timezone.utc)
        current_date = now.strftime('%Y-%m-%d')
        last_interaction_date = session_attrs.get("last_interaction_date")

        if last_interaction_date != current_date:
            speak_output = globals().get("alexa_speak_welcome_message")
            session_attrs["last_interaction_date"] = current_date
        else:
            speak_output = globals().get("alexa_speak_next_message")

        device = handler_input.request_envelope.context.system.device
        is_apl_supported = device.supported_interfaces.alexa_presentation_apl is not None

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("device: " + repr(device))

        if is_apl_supported:
            handler_input.response_builder.add_directive(
                RenderDocumentDirective(
                    token=_APL_TOKEN,
                    document=load_template("apl_openha.json")
                )
            )

        return handler_input.response_builder.speak(speak_output).ask(speak_output).response


class GptQueryIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return ask_utils.is_intent_name("GptQueryIntent")(handler_input)

    def handle(self, handler_input):
        query = handler_input.request_envelope.request.intent.slots["query"].value
        logger.info(f"Query received: {query}")

        keywords = globals().get("keywords_to_open_dashboard", "").split(";")
        if any(keyword.strip().lower() in query.lower() for keyword in keywords):
            logger.info("Opening Home Assistant dashboard")
            open_page(handler_input)
            return handler_input.response_builder.speak(globals().get("alexa_speak_open_dashboard")).response

        device_id = ""
        if home_assistant_room_recognition:
            device_id = ". device_id: " + handler_input.request_envelope.context.system.device.device_id

        session_attrs = handler_input.attributes_manager.session_attributes
        conversation_id = session_attrs.get("conversation_id")

        response, conversation_id = process_conversation(f"{query}{device_id}", conversation_id)
        session_attrs["conversation_id"] = conversation_id

        logger.info(f"Response generated: {response}")
        return handler_input.response_builder.speak(response).ask(globals().get("alexa_speak_question")).response


def process_conversation(query: str, conversation_id: str | None) -> tuple[str, str | None]:
    try:
        data: dict = {
            "text": query,
            "language": home_assistant_language,
            "agent_id": home_assistant_agent_id,
        }
        if conversation_id:
            data["conversation_id"] = conversation_id

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(f"HA request data: {data}")

        # 6.5s leaves ~1.5s for Alexa response building before the 8s hard limit
        response = _http_session.post(home_assistant_url, json=data, timeout=6.5)

        logger.info(f"HA response status: {response.status_code}")
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(f"HA response data: {response.text}")

        response_data = response.json()

        if response.status_code == 200 and "response" in response_data:
            conversation_id = response_data.get("conversation_id", conversation_id)
            response_type = response_data["response"]["response_type"]
            if response_type in ("action_done", "query_answer"):
                speech = response_data["response"]["speech"]["plain"]["speech"]
            elif response_type == "error":
                speech = response_data["response"]["speech"]["plain"]["speech"]
                logger.error(f"Error code: {response_data['response']['data']['code']}")
            else:
                speech = globals().get("alexa_speak_error")
            return improve_response(speech), conversation_id
        else:
            error_message = response_data.get("message", "Unknown error")
            logger.error(f"HA request failed: {error_message}")
            return globals().get("alexa_speak_error"), conversation_id

    except requests.exceptions.Timeout as te:
        logger.error(f"Timeout communicating with Home Assistant: {str(te)}", exc_info=True)
        return globals().get("alexa_speak_timeout"), conversation_id

    except Exception as e:
        logger.error(f"Error generating response: {str(e)}", exc_info=True)
        return globals().get("alexa_speak_error"), conversation_id


def improve_response(speech: str) -> str:
    speech = speech.replace(':\n\n', '')
    speech = speech.replace('\n\n', '. ')
    speech = speech.replace('\n', ',')
    speech = speech.replace('-', '')
    speech = speech.replace('_', ' ')

    # Strip diaeresis variants that are encoding artifacts (ï is not a German umlaut)
    speech = speech.translate(str.maketrans('ïÏ', 'iI'))

    # Preserve German umlauts (ä ö ü ß) and common Romance accents; strip the rest
    speech = re.sub(r'[^A-Za-z0-9äöüßÄÖÜçÇáàâãéèêíóôõúñÁÀÂÃÉÈÊÍÓÔÕÚÑ\s.,!?]', '', speech)

    return speech


def load_template(filepath: str) -> dict:
    template = copy.deepcopy(_APL_TEMPLATES[filepath])
    if filepath == 'apl_openha.json':
        template['mainTemplate']['items'][0]['items'][2]['text'] = globals().get("echo_screen_welcome_text")
        template['mainTemplate']['items'][0]['items'][3]['text'] = globals().get("echo_screen_click_text")
        template['mainTemplate']['items'][0]['items'][4]['onPress']['source'] = get_hadash_url()
        template['mainTemplate']['items'][0]['items'][4]['item']['text'] = globals().get("echo_screen_button_text")
    return template


def open_page(handler_input) -> None:
    is_apl_supported = handler_input.request_envelope.context.system.device.supported_interfaces.alexa_presentation_apl is not None

    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(f"APL supported: {is_apl_supported}")

    if is_apl_supported:
        # Empty APL required before OpenUrlCommand can execute
        # https://amazon.developer.forums.answerhub.com/questions/220506/alexa-open-a-browser.html
        handler_input.response_builder.add_directive(
            RenderDocumentDirective(
                token=_APL_TOKEN,
                document=load_template("apl_empty.json")
            )
        )
        handler_input.response_builder.add_directive(
            ExecuteCommandsDirective(
                token=_APL_TOKEN,
                commands=[OpenUrlCommand(source=get_hadash_url())]
            )
        )


def get_hadash_url() -> str:
    source = home_assistant_url.replace('api/conversation/process', home_assistant_dashboard)
    if home_assistant_kioskmode:
        source += '?kiosk'
    return source


class HelpIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return ask_utils.is_intent_name("AMAZON.HelpIntent")(handler_input)

    def handle(self, handler_input):
        speak_output = globals().get("alexa_speak_help")
        return handler_input.response_builder.speak(speak_output).ask(speak_output).response


class CancelOrStopIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return ask_utils.is_intent_name("AMAZON.CancelIntent")(handler_input) or ask_utils.is_intent_name("AMAZON.StopIntent")(handler_input)

    def handle(self, handler_input):
        open_page(handler_input)
        speak_output = random.choice(globals().get("alexa_speak_exit").split(";"))
        return handler_input.response_builder.speak(speak_output).response


class SessionEndedRequestHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return ask_utils.is_request_type("SessionEndedRequest")(handler_input)

    def handle(self, handler_input):
        open_page(handler_input)
        return handler_input.response_builder.response


class CatchAllExceptionHandler(AbstractExceptionHandler):
    def can_handle(self, handler_input, exception):
        return True

    def handle(self, handler_input, exception):
        logger.error(exception, exc_info=True)
        speak_output = globals().get("alexa_speak_error")
        return handler_input.response_builder.speak(speak_output).ask(speak_output).response


sb = SkillBuilder()
sb.add_request_handler(LaunchRequestHandler())
sb.add_request_handler(GptQueryIntentHandler())
sb.add_request_handler(HelpIntentHandler())
sb.add_request_handler(CancelOrStopIntentHandler())
sb.add_request_handler(SessionEndedRequestHandler())
sb.add_exception_handler(CatchAllExceptionHandler())

lambda_handler = sb.lambda_handler()
