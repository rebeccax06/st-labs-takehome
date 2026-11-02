"""Small OpenRouter client. Credentials are loaded lazily and never logged."""

from pathlib import Path

import httpx

from app.ai.json_output import decode_object


class AIError(RuntimeError):
    def __init__(self, message, *, code="ai_invalid_response", status_code=502):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class OpenRouterClient:
    ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"

    def __init__(self, settings, *, transport=None):
        self.settings = settings
        self.transport = transport

    def _key(self):
        if self.settings.openrouter_api_key is not None:
            value = self.settings.openrouter_api_key.get_secret_value().strip()
        else:
            try:
                value = Path(self.settings.openrouter_token_path).read_text(encoding="utf-8").strip()
            except (OSError, UnicodeError):
                raise AIError("OpenRouter is not configured. Set OPENROUTER_API_KEY or provide token.txt.",
                              code="ai_unavailable", status_code=503) from None
        if not value or not value.isascii() or any(character.isspace() for character in value):
            raise AIError("OpenRouter credentials must contain one nonempty API key.", code="ai_unavailable", status_code=503)
        return value

    def complete(self, *, messages, schema, schema_name="cmdb_extraction"):
        key = self._key()
        if not key:
            raise AIError("OpenRouter API key is empty.", code="ai_unavailable", status_code=503)
        payload = dict(model=self.settings.openrouter_model, messages=messages, temperature=0,
                       max_tokens=self.settings.openrouter_max_tokens, provider={"require_parameters": True},
                       response_format={"type": "json_schema", "json_schema": {
                           "name": schema_name, "strict": False, "schema": schema}})
        try:
            with httpx.Client(timeout=self.settings.openrouter_timeout_seconds, transport=self.transport) as client:
                with client.stream("POST", self.ENDPOINT, headers={
                    "Authorization": "Bearer " + key, "Content-Type": "application/json",
                }, json=payload) as response:
                    if response.status_code in (401, 402, 403, 429) or response.status_code >= 500:
                        raise AIError("OpenRouter is unavailable, rate-limited, or could not authorize the request.",
                                      code="ai_unavailable", status_code=503)
                    if response.status_code != 200:
                        raise AIError("OpenRouter rejected the model request; check the model/schema settings or use a smaller input.")
                    chunks, size = [], 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > 1024 * 1024:
                            raise AIError("OpenRouter response exceeds the supported size.")
                        chunks.append(chunk)
            data = decode_object(b"".join(chunks))
            if data.get("error") is not None:
                raise AIError("OpenRouter did not complete the model request.")
            choices = data.get("choices")
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                raise ValueError("Invalid completion choices")
            choice = choices[0]
            if choice.get("finish_reason") != "stop":
                raise AIError("Model response was incomplete. Use a smaller input or adjust the configured output limit.")
            message = choice.get("message")
            if not isinstance(message, dict):
                raise ValueError("Invalid completion message")
            output = message.get("content")
            if not isinstance(output, str) or not output.strip():
                raise AIError("OpenRouter returned no model output.")
            return output
        except httpx.TimeoutException:
            raise AIError("OpenRouter timed out; retry the request.", code="ai_timeout", status_code=504) from None
        except httpx.RequestError:
            raise AIError("Could not connect to OpenRouter.", code="ai_unavailable", status_code=503) from None
        except (ValueError, TypeError, KeyError, IndexError, RecursionError):
            raise AIError("OpenRouter returned an invalid response envelope.") from None
