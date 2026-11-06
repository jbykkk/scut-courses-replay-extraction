import unittest
from types import SimpleNamespace
import httpx
from assistant_service.connection_check import check


def llm_settings(provider="deepseek", model="example-model", key="unit-test-key"):
    spec = {"deepseek": ("DeepSeek", "https://api.deepseek.com"),
            "openai": ("ChatGPT", "https://api.openai.com/v1")}[provider]
    cfg = {"provider": provider, "label": spec[0], "base_url": spec[1], "model": model, "key": key}
    return SimpleNamespace(llm=lambda: cfg)


class ConnectionTests(unittest.TestCase):
    def test_authentication_probe_has_no_course_text_and_no_key_in_response(self):
        settings=llm_settings()
        def handle(request):
            self.assertEqual(str(request.url),"https://api.deepseek.com/models")
            self.assertEqual(request.method,"GET")
            self.assertEqual(request.content,b"")
            return httpx.Response(200,json={"data":[{"id":"example-model"}]})
        result=check(settings,httpx.MockTransport(handle))
        self.assertTrue(result["ok"])
        self.assertNotIn("unit-test-key",str(result))

    def test_openai_probe_uses_the_official_openai_endpoint(self):
        settings=llm_settings(provider="openai",model="gpt-5-mini")
        def handle(request):
            self.assertEqual(str(request.url),"https://api.openai.com/v1/models")
            self.assertEqual(request.headers["authorization"],"Bearer unit-test-key")
            return httpx.Response(200,json={"data":[{"id":"gpt-5-mini"}]})
        result=check(settings,httpx.MockTransport(handle))
        self.assertTrue(result["ok"])

    def test_failed_auth_and_missing_model_are_not_reported_as_connected(self):
        settings=llm_settings()
        for response in [httpx.Response(401,text="private upstream error"),httpx.Response(200,json={"data":[]})]:
            result=check(settings,httpx.MockTransport(lambda request:response))
            self.assertFalse(result["ok"])
            self.assertNotIn("private upstream",str(result))

    def test_legacy_flash_name_matches_the_current_model_listing(self):
        settings=llm_settings(model="deepseek-v4-flash")
        transport=httpx.MockTransport(lambda request:httpx.Response(200,json={"data":[{"id":"deepseek-flash"}]}))
        self.assertTrue(check(settings,transport)["ok"])
