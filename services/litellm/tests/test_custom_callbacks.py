import asyncio
import base64
import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path


def _install_litellm_stubs() -> None:
    module_names = (
        "litellm",
        "litellm.llms",
        "litellm.llms.anthropic",
        "litellm.llms.anthropic.common_utils",
        "litellm._logging",
        "litellm.integrations",
        "litellm.integrations.custom_logger",
        "litellm.integrations.generic_api",
        "litellm.integrations.generic_api.generic_api_callback",
        "litellm.litellm_core_utils",
        "litellm.litellm_core_utils.safe_json_dumps",
        "litellm.responses",
        "litellm.responses.sse_output_recovery",
    )
    for name in module_names:
        sys.modules[name] = types.ModuleType(name)

    common_utils = sys.modules["litellm.llms.anthropic.common_utils"]
    common_utils.is_anthropic_oauth_key = lambda value: bool(
        value and value.removeprefix("Bearer ").startswith("sk-ant-oat")
    )

    sys.modules["litellm._logging"].verbose_proxy_logger = object()
    sys.modules["litellm.integrations.custom_logger"].CustomLogger = object
    generic_api = sys.modules["litellm.integrations.generic_api.generic_api_callback"]
    generic_api.GenericAPILogger = type("GenericAPILogger", (), {})
    sys.modules["litellm.litellm_core_utils.safe_json_dumps"].safe_dumps = json.dumps
    recovery = sys.modules["litellm.responses.sse_output_recovery"]
    recovery.record_output_item_chunk = lambda *args, **kwargs: None
    recovery.record_output_text_chunk = lambda *args, **kwargs: None


def _load_callbacks_module():
    _install_litellm_stubs()
    path = Path(__file__).parents[1] / "custom_callbacks.py"
    spec = importlib.util.spec_from_file_location("custom_callbacks_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _jwt(account_id: str) -> str:
    def encode(value: dict) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    claims = {"https://api.openai.com/auth": {"chatgpt_account_id": account_id}}
    return f"{encode({'alg': 'none'})}.{encode(claims)}.signature"


class ChatGPTAuthForwardHandlerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.callbacks = _load_callbacks_module()

    def test_forwards_raw_authorization_when_public_copy_is_redacted(self) -> None:
        token = _jwt("account-test")
        data = {
            "proxy_server_request": {"headers": {"Authorization": "[REDACTED]"}},
            "secret_fields": {"raw_headers": {"AuThOrIzAtIoN": f"Bearer {token}"}},
        }

        result = asyncio.run(
            self.callbacks.ChatGPTAuthForwardHandler().async_pre_call_hook(
                user_api_key_dict=None,
                cache=None,
                data=data,
                call_type="responses",
            )
        )

        self.assertEqual(result["extra_headers"]["Authorization"], f"Bearer {token}")
        self.assertEqual(result["extra_headers"]["ChatGPT-Account-Id"], "account-test")
        self.assertNotIn(token, repr(result["proxy_server_request"]))

    def test_does_not_forward_redacted_header_without_raw_secret(self) -> None:
        data = {"proxy_server_request": {"headers": {"authorization": "[REDACTED]"}}}

        result = asyncio.run(
            self.callbacks.ChatGPTAuthForwardHandler().async_pre_call_hook(
                user_api_key_dict=None,
                cache=None,
                data=data,
                call_type="responses",
            )
        )

        self.assertNotIn("extra_headers", result)


if __name__ == "__main__":
    unittest.main()
