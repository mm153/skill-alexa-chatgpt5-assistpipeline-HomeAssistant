# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Alexa **Hosted** skill that bridges Echo devices to a Home Assistant `conversation/process` endpoint (Assist, OpenAI, Extended OpenAI, or Google Generative AI as the configured agent). On Echo Show devices it also renders an APL document that opens a Home Assistant dashboard URL.

Hard constraint: Alexa Hosted skills time out external HTTPS calls at **8 seconds**. The Home Assistant agent must respond in ~6–7s; slow LLMs will not work and the AWS-hosted variant should be used instead (linked in README).

## Repository Layout

- `lambda/` — Python Lambda (the skill backend). Deployed as-is by Alexa Hosted.
  - `lambda_function.py` — single-file skill handlers + HA HTTP client.
  - `config.cfg` — runtime configuration (HA URL, token, agent, language, dashboard, flags). **Must be edited before deploy.** Loaded by `load_config()` at cold start.
  - `locale/<bcp47>.lang` — per-locale translation files in `key=value` format. `load_localization()` injects keys into Python `globals()` (so handlers reference strings via `globals().get("alexa_speak_…")`).
  - `apl_openha.json` — APL doc shown on screen-capable devices at launch (button opens HA dashboard).
  - `apl_empty.json` — empty APL used as a target for `OpenUrlCommand` (workaround for Alexa requiring a rendered doc before `OpenURL`).
  - `requirements.txt` — pins `ask-sdk-core`, `requests`, `boto3`.
- `skill-package/` — Alexa skill package (deployed alongside the Lambda).
  - `skill.json` — manifest, ARNs per region, supported viewports, locale metadata.
  - `interactionModels/custom/<locale>.json` — utterances/slots per locale; the main intent is `GptQueryIntent` with a `query` slot.
  - `assets/images/` — store icons referenced from `skill.json`.
- `doc/{en,pt}/` — installation/update documentation.

## Architecture Notes

- **State across invocations.** `conversation_id` and `last_interaction_date` are module-level globals. Lambda warm starts retain them, giving conversational context until the container recycles. Don't refactor these to per-request state without understanding the welcome-message-once-per-day behavior in `LaunchRequestHandler`.
- **Localization model.** `load_localization(locale)` writes string keys directly into `globals()`. This is intentional but means *all* localization keys are effectively a flat global namespace — adding a new key requires updating every `locale/*.lang` file or the default `en-US.lang` will mask the gap.
- **Dashboard URL derivation.** `get_hadash_url()` reuses `home_assistant_url` and string-replaces `api/conversation/process` with the configured dashboard path. Changing the configured URL away from that endpoint will break dashboard opening.
- **Room recognition.** When `home_assistant_room_recognition=True`, the Echo `device_id` is appended to the user query (`". device_id: <id>"`) so the HA conversation agent can map device → area. The HA agent must be configured to understand this hint.
- **Open-dashboard intent is keyword-based**, not a separate intent: `GptQueryIntentHandler` checks the localized `keywords_to_open_dashboard` (semicolon-separated) against the query before forwarding to HA. Add keywords by editing each `locale/*.lang`.
- **APL is required for dashboard open.** `open_page()` no-ops on audio-only Echos (no APL interface).
- **Response sanitization.** `improve_response()` strips markdown/punctuation that the TTS engine mispronounces. Keep this in sync with characters Alexa's voice can't speak; see the regex/translate calls.

## Common Tasks

This is an Alexa Hosted skill, so build/deploy is done via the Alexa Developer Console or ASK CLI — there is no Makefile, package.json, or test suite in the repo.

- **Configure before deploy.** Edit `lambda/config.cfg` (HA URL must end in `/api/conversation/process`, token is a long-lived HA access token, `home_assistant_agent_id` is the entity ID of the chosen conversation agent in HA).
- **Deploy.** Push to the Alexa-hosted git remote, or use `ask deploy` if the project is wired to ASK CLI. The hosted environment installs `lambda/requirements.txt` automatically.
- **Add a language.** Create `lambda/locale/<bcp47>.lang` (copy `en-US.lang` as a template), add `skill-package/interactionModels/custom/<bcp47>.json`, and add the locale block under `publishingInformation.locales` in `skill-package/skill.json`.
- **Add a localization key.** Add `key=value` to **every** `lambda/locale/*.lang` file; reference via `globals().get("key")` in `lambda_function.py`.
- **Test latency.** Always exercise the chosen HA agent with both simple and complex prompts before shipping — anything over ~7s round-trip will trigger the timeout path (`alexa_speak_timeout`).

## Known Issues / Gotchas

- `CancelOrStopIntentHandler.handle` calls `globals().get("speak_output")` (string literal) instead of using the local `speak_output` variable — this returns `None` and the response will not actually speak the localized exit phrase. Preserve or fix deliberately when touching that handler.
- `home_assistant_room_recognition` and `home_assistant_kioskmode` are read with `bool(config.get(...))`, which is truthy for the string `"False"`. Treat any non-empty value as `True` until this is fixed, or compare strings explicitly when changing the flag logic.
- The skill is in **early alpha** per README — breaking changes between releases are expected.
