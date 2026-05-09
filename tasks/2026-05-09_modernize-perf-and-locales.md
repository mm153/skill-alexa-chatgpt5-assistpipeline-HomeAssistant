# Modernize skill: dependencies, performance, bugs, and locale rework (en + de only)

## 1. Context

Linear Issue: _none provided_.

This Alexa Hosted skill bridges Echo to Home Assistant's `conversation/process` endpoint and is constrained by Alexa's hard 8-second external-call timeout. Audit ([CLAUDE.md](../CLAUDE.md), [lambda/lambda_function.py](../lambda/lambda_function.py)) found stale dependency pins, several roundtrip-time wastes (no `requests.Session`, repeated disk I/O, timeout set equal to the Alexa limit), and bugs that affect correctness (cross-user state leak via module globals, broken cancel handler, broken bool config parsing, hardcoded UTC-3 timezone, APL token misuse, locale-specific word substitution running for all locales). The skill currently ships pt-BR/pt-PT/es-ES/fr-FR/it-IT/en-US/en-GB but the user only needs **English (en-US, en-GB)** and **German (de-DE)**. en-GB is also declared in `skill.json` without a matching interaction model — currently broken.

## 2. Goal

Modernize the toolchain (uv for local dev, current-version dep pins) and reduce HA roundtrip latency, fix correctness bugs, and rework localization to English (en-US + en-GB) and German (de-DE) only — so the skill reliably stays under the 8s budget on a properly-tuned HA OpenAI agent.

## 3. Non-Goals

- Migrating off Alexa Hosted to AWS-hosted Lambda.
- Adding new skill features (no new intents, no streaming, no new APL screens).
- Implementing GPT-5 model selection inside the skill (model lives in HA agent config).
- Refactoring the localization-via-`globals()` pattern beyond what the bug fixes require.
- Adding tests for handlers (no test harness currently exists; out of scope for this pass).
- Translating documentation under `doc/pt/` to German — docs stay as-is, only `doc/en/` is the supported reference.

## 4. Assumptions & Constraints

- **Alexa Hosted runtime is Python 3.11 and installs from `lambda/requirements.txt`.** `uv` is *not* run by the Hosted build pipeline. uv is therefore a **local dev tool**; the canonical deploy artifact remains `requirements.txt`, generated via `uv pip compile` (or `uv export`) from `pyproject.toml`.
- **8s hard limit** on outbound HTTPS from skill to HA — every micro-optimization counts but the dominant factor is HA agent latency, which is out of scope here.
- **Module-level state survives only within a warm Lambda container.** Multiple users hitting the same warm container today share `conversation_id` and `last_interaction_date` — must move to per-session attributes.
- **Locale fallback already exists**: `load_localization()` falls back to `en-US.lang` if a locale file is missing. en-GB therefore works at runtime even without an `en-GB.lang` (it inherits en-US strings). en-GB *interaction model* is the actual gap.
- **APL `token` field** can be any opaque string — using the HA bearer token there leaks no secrets to Alexa cloud (it's already exchanged out-of-band) but is semantically confusing; replace with a constant.
- Latest stable versions targeted (verify at execution time): `ask-sdk-core` ~1.19.x, `requests` ~2.32.x, `boto3` ~1.35.x. Pin to upper bounds (`<2`, `<3`) rather than exact versions to ease security patching on Hosted rebuilds.

## 5. Proposed approach

Three-stage rework, each independently shippable:

**Stage A — toolchain & deps.** Add `pyproject.toml` + `uv.lock` for local development, regenerate `lambda/requirements.txt` from it with current versions. Document the uv workflow in `doc/en/INSTALLATION.md`.

**Stage B — performance & bug fixes.** All inside `lambda/lambda_function.py`, no API surface changes. Cache template/locale file reads at module load, reuse a single `requests.Session`, drop request `timeout` to ~6.5s, move `conversation_id`/`last_interaction_date` to session attributes, fix the cancel handler, fix the `bool("False") == True` config trap, replace UTC-3 with `datetime.now(timezone.utc)` (or device-locale timezone if cheap), replace APL token with a constant, gate `replace_words` to pt-BR-only locales (or remove since we're dropping Portuguese). Demote DEBUG logs of full HA bodies to `INFO` (or guard with `logger.isEnabledFor`).

**Stage C — locale rework.** Add `de-DE` (lang file, interaction model, skill.json publishing block). Add `en-GB` interaction model (mirror en-US). Remove `pt-BR`, `pt-PT`, `es-ES`, `fr-FR`, `it-IT` from `lambda/locale/`, `skill-package/interactionModels/custom/`, and the `publishingInformation.locales` block in `skill-package/skill.json`. Update README accordingly.

## 6. Phase breakdown

1. **Bootstrap uv + pyproject** — author `pyproject.toml`, lock, regenerate `lambda/requirements.txt`.
2. **Bump deps** — pin `ask-sdk-core`, `requests`, `boto3` to current latest; verify Hosted Python 3.11 compatibility.
3. **Cache module-load I/O** — read both APL templates and the locale dict once at cold start; eliminate per-request file reads.
4. **Reuse HTTP connection** — module-level `requests.Session`, set `timeout=6.5`.
5. **Fix cross-user state leak** — move `conversation_id` and `last_interaction_date` into `handler_input.attributes_manager.session_attributes` (and persistent attributes if cross-session continuity is desired — see Open Questions).
6. **Fix cancel handler** — use the local `speak_output` variable instead of `globals().get("speak_output")`.
7. **Fix bool config parsing** — string-compare `.lower() == "true"` for `home_assistant_room_recognition` and `home_assistant_kioskmode`.
8. **Fix timezone hardcoding** — replace `timezone(timedelta(hours=-3))` with UTC or request-locale-derived TZ.
9. **Fix APL token misuse** — constant string `"ha_dashboard"`.
10. **Scope `replace_words`** — remove (Portuguese is being dropped).
11. **Demote noisy DEBUG logging** — guard full-body logs.
12. **Add de-DE locale** — `.lang`, interaction model, manifest entry.
13. **Add en-GB interaction model** — mirror en-US, fix the manifest-vs-models gap.
14. **Remove unsupported locales** — pt-BR, pt-PT, es-ES, fr-FR, it-IT.
15. **Update docs & README** — supported languages list, uv workflow.

## 7. File touch map

| Path | Status | Range | Reason |
|------|--------|-------|--------|
| `pyproject.toml` | New | — | uv-managed project metadata + dep declarations. |
| `uv.lock` | New | — | Reproducible local lockfile. |
| `lambda/requirements.txt` | Existing | full rewrite | Bump pins to current versions, regenerated from uv. |
| `lambda/lambda_function.py` | Existing | 23-46 | Cache config + templates at module load. |
| `lambda/lambda_function.py` | Existing | 50-68 | Cache locale dicts (read once per locale, reuse). |
| `lambda/lambda_function.py` | Existing | 75-77 | Remove module globals `conversation_id`, `last_interaction_date`. |
| `lambda/lambda_function.py` | Existing | 79-119 | `LaunchRequestHandler`: read date from session/persistent attrs; UTC time. |
| `lambda/lambda_function.py` | Existing | 121-194 | `GptQueryIntentHandler`+`process_conversation`: session-scoped `conversation_id`; `requests.Session`; `timeout=6.5`. |
| `lambda/lambda_function.py` | Existing | 196-198 | Drop `replace_words` (Portuguese removed). |
| `lambda/lambda_function.py` | Existing | 217-229 | `load_template`: read once at cold start, return cached. |
| `lambda/lambda_function.py` | Existing | 231-253 | `open_page`: use cached empty template; APL token constant. |
| `lambda/lambda_function.py` | Existing | 274-281 | `CancelOrStopIntentHandler`: speak the local `speak_output`. |
| `lambda/lambda_function.py` | Existing | 45-47 | Bool config parsing fix. |
| `lambda/locale/de-DE.lang` | New | — | German strings (mirror en-US.lang keys). |
| `lambda/locale/en-GB.lang` | New | — | Optional — only if en-GB strings should differ from en-US. Otherwise omit and rely on fallback. |
| `lambda/locale/pt-BR.lang` | Existing | delete | Drop unsupported locale. |
| `lambda/locale/pt-PT.lang` | Existing | delete | Drop unsupported locale. |
| `lambda/locale/es-ES.lang` | Existing | delete | Drop unsupported locale. |
| `lambda/locale/fr-FR.lang` | Existing | delete | Drop unsupported locale. |
| `lambda/locale/it-IT.lang` | Existing | delete | Drop unsupported locale. |
| `skill-package/interactionModels/custom/de-DE.json` | New | — | German interaction model (mirror en-US, translate sample utterances + invocationName). |
| `skill-package/interactionModels/custom/en-GB.json` | New | — | Mirror en-US.json; declared in manifest but currently missing. |
| `skill-package/interactionModels/custom/pt-BR.json` | Existing | delete | Drop. |
| `skill-package/interactionModels/custom/pt-PT.json` | Existing | delete | Drop. |
| `skill-package/interactionModels/custom/es-ES.json` | Existing | delete | Drop. |
| `skill-package/interactionModels/custom/fr-FR.json` | Existing | delete | Drop. |
| `skill-package/interactionModels/custom/it-IT.json` | Existing | delete | Drop. |
| `skill-package/skill.json` | Existing | 120-205 | Replace `publishingInformation.locales` map: keep `en-US`, `en-GB`; add `de-DE`; remove the rest. |
| `README.md` | Existing | 45-56, 105-113 | Supported-languages section: English (US/GB) + German only. |
| `doc/en/INSTALLATION.md` | Existing | append section | Document uv workflow and `requirements.txt` regeneration command. |
| `doc/pt/` | Existing | leave as-is | Portuguese docs no longer relevant but not deleted in this pass. |
| `CLAUDE.md` | Existing | "Add a language" + "Common Tasks" sections | Reflect new locale set + uv workflow. |

## 8. Acceptance criteria (user-visible outcomes)

- Saying any supported wake-up phrase in **en-US**, **en-GB**, or **de-DE** opens the skill and returns a localized welcome message; the German welcome is in German, the English welcomes are in English.
- The skill is no longer published in pt-BR, pt-PT, es-ES, fr-FR, or it-IT — the manifest declares only the three supported locales.
- A complex HA query that takes ~7s end-to-end now succeeds (it currently can hit `timeout=8` and lose the response-build budget); the skill speaks the HA response instead of the timeout phrase.
- Two users hitting the same warm Lambda container do not see each other's `conversation_id` context — each gets their own conversational thread.
- Saying "stop" or "cancel" speaks the localized goodbye phrase out loud (today it's silent).
- With `home_assistant_room_recognition=False` in [lambda/config.cfg](../lambda/config.cfg), no `device_id` suffix is appended to the HA query (today it always is, due to the bool bug).
- Cold-start invocation latency does not regress; warm-invocation HA call latency drops measurably (target: −150 to −400ms median) due to `Session` reuse and removed per-request file reads.
- Running `uv sync` locally produces a working dev environment; `uv pip compile` regenerates a `lambda/requirements.txt` that the Alexa Hosted build accepts.
- The README's "Supported languages" section lists only English (US/GB) and German.

## 9. Definition of done

- [ ] `pyproject.toml` + `uv.lock` checked in; `lambda/requirements.txt` regenerated and matches lock.
- [ ] `lambda/requirements.txt` pins resolve to current latest stable on PyPI at time of execution.
- [ ] All 8 perf items + 6 bugs from the audit are addressed (or explicitly deferred with rationale in this plan).
- [ ] `de-DE`, `en-US`, `en-GB` are present in **all three** of: `lambda/locale/` (en-GB optional, falls back), `skill-package/interactionModels/custom/`, `skill-package/skill.json` publishing locales.
- [ ] Removed locales (`pt-*`, `es-ES`, `fr-FR`, `it-IT`) absent from all three places above.
- [ ] Manual end-to-end test on a real Echo (or simulator) for each of the 3 locales: launch + one HA query + cancel + dashboard-open keyword.
- [ ] CloudWatch logs at INFO level do not contain full HA response bodies on the happy path.
- [ ] README, [CLAUDE.md](../CLAUDE.md), and `doc/en/INSTALLATION.md` reflect new state.
- [ ] No Linear issue to update (none provided). If one is created mid-flight, post a summary comment.

## 10. Risks and mitigations

- **uv not supported by Alexa Hosted build.** Mitigation: keep `lambda/requirements.txt` as the deploy artifact; uv is local-only. Hosted pipeline never runs uv.
- **Bumping `ask-sdk-core` breaks an import path.** Mitigation: `ask-sdk-core` 1.x has been API-stable for years; do a grep for any deprecated imports after the bump and fix at the call site. The skill uses only `SkillBuilder`, `AbstractRequestHandler`, `AbstractExceptionHandler`, `HandlerInput`, APL directives — all stable.
- **Session-attribute migration regresses conversational continuity.** Mitigation: store `conversation_id` in session attributes (per-session). For cross-session continuity (today's accidental warm-Lambda behavior is *not* a feature — it's a bug), use persistent attributes only if the user explicitly wants it (see Open Questions).
- **Removing locales breaks live skill listings for users in those regions.** Mitigation: confirm with stakeholder before publishing. If skill is unpublished/personal, no impact.
- **APL `token` change** is purely cosmetic — risk near-zero.
- **Timezone change to UTC may shift the "first launch of the day" boundary** for some users. Mitigation: derive offset from request locale if cheap; otherwise UTC is acceptable since the welcome-vs-next-message difference is minor.

## 11. Open questions

**Blocking:**

1. _None._ Defaults below stand unless contradicted before implementation.

**Non-blocking (defaults chosen):**

1. **Drop es-ES, fr-FR, it-IT entirely?** Default: **yes, remove**, since the user said "we need German and English" and "Portuguese is not relevant" by name; the other Romance locales weren't mentioned as keepers and "English + German only" is the simplest read of the request.
2. **Cross-session conversation continuity** — should `conversation_id` survive across sessions? Default: **no, per-session only.** Aligns with how the HA conversation API is typically used and avoids stale context bleeding into unrelated requests.
3. **Add an `en-GB.lang` file** with British-English specific strings (e.g. "Cheers" already in en-US `alexa_speak_exit`)? Default: **no**, rely on `en-US.lang` fallback.
4. **uv version pin policy** — exact pins or compatible-release (`~=`)? Default: **`>=current,<next-major`** to keep security patches flowing.
5. **Keep `boto3` in requirements?** It's listed but not imported in `lambda_function.py`. Default: **drop it** — `boto3` is provided by the Lambda runtime, no need to pin.
6. **Translate `doc/en/` to `doc/de/` for German users?** Default: **no, out of scope this pass.**

## 12. Resolved questions

_(none yet)_

## 13. Revision history

- 2026-05-09 — Initial plan drafted.

## References

- Alexa Hosted skill timeout: [Alexa Skills Kit – Timeouts](https://developer.amazon.com/en-US/docs/alexa/custom-skills/handle-requests-sent-by-alexa.html#timeouts)
- Alexa Hosted Python runtime: [Alexa-hosted skills overview](https://developer.amazon.com/en-US/docs/alexa/hosted-skills/build-a-skill-end-to-end-using-an-alexa-hosted-skill.html)
- ASK SDK for Python: [alexa/alexa-skills-kit-sdk-for-python](https://github.com/alexa/alexa-skills-kit-sdk-for-python)
- Session vs persistent attributes: [Manage attributes (ASK SDK Python)](https://developer.amazon.com/en-US/docs/alexa/alexa-skills-kit-sdk-for-python/manage-attributes.html)
- HA conversation API: [Intent / Conversation API – Home Assistant Developers](https://developers.home-assistant.io/docs/intent_conversation_api)
- uv package manager: [astral-sh/uv – README](https://github.com/astral-sh/uv)
- requests `Session` reuse: [requests Advanced Usage – Session Objects](https://requests.readthedocs.io/en/latest/user/advanced/#session-objects)
