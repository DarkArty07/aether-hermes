"""Loopback regressions for generic gateway model metadata.

The handlers and model identifiers in this module are deliberately synthetic.
They exercise the real HTTP request path without a live provider or credential.
"""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread
from unittest.mock import patch

import pytest


class _SyntheticModelsHandler(BaseHTTPRequestHandler):
    routes = {}

    def do_GET(self):  # noqa: N802 - stdlib handler API
        status, payload = self.routes.get(self.path, (404, {"error": "not found"}))
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


@contextmanager
def _synthetic_gateway(routes):
    handler = type(
        "SyntheticModelsHandler", (_SyntheticModelsHandler,), {"routes": routes}
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture(autouse=True)
def _clear_metadata_probe_caches():
    import agent.model_metadata as metadata

    metadata._endpoint_model_metadata_cache.clear()
    metadata._endpoint_model_metadata_cache_time.clear()
    metadata._endpoint_probe_path_cache.clear()
    metadata._endpoint_blackhole_cache.clear()
    yield
    metadata._endpoint_model_metadata_cache.clear()
    metadata._endpoint_model_metadata_cache_time.clear()
    metadata._endpoint_probe_path_cache.clear()
    metadata._endpoint_blackhole_cache.clear()


def _generic_models_payload():
    return {
        "object": "list",
        "data": [
            {
                "id": "synthetic/gateway-context-model",
                "context_length": 872000,
                "owned_by": "synthetic-gateway",
            }
        ],
    }


def test_generic_data_shape_is_parsed_even_when_detector_reports_lmstudio():
    """An HTTP-200 ``data`` payload reaches the generic parser."""
    from agent.model_metadata import (
        fetch_endpoint_model_metadata,
        get_model_context_length,
    )

    # The generic gateway shape is exposed only at LM Studio's probe path.
    # Alternate model-list paths are deliberately unavailable so this proves
    # the already-received payload is parsed rather than re-probed elsewhere.
    routes = {
        "/api/v1/models": (200, _generic_models_payload()),
    }
    with _synthetic_gateway(routes) as base_url:
        with patch(
            "agent.model_metadata.detect_local_server_type", return_value="lm-studio"
        ):
            metadata = fetch_endpoint_model_metadata(base_url, force_refresh=True)
            context_length = get_model_context_length(
                "synthetic/gateway-context-model",
                base_url=base_url,
                provider="custom",
            )

    assert metadata["synthetic/gateway-context-model"]["context_length"] == 872000
    assert context_length == 872000


def test_generic_data_shape_revalidates_stale_disk_lmstudio_verdict():
    """A legacy disk verdict cannot discard generic endpoint metadata."""
    from agent.model_metadata import fetch_endpoint_model_metadata

    routes = {
        "/api/v1/models": (200, _generic_models_payload()),
    }
    with _synthetic_gateway(routes) as base_url:
        with patch(
            "agent.model_metadata._local_probe_disk_get", return_value="lm-studio"
        ):
            metadata = fetch_endpoint_model_metadata(base_url, force_refresh=True)

    assert metadata["synthetic/gateway-context-model"]["context_length"] == 872000


def test_empty_lmstudio_models_falls_through_to_generic_data():
    """An empty native list cannot hide a populated generic model list."""
    from agent.model_metadata import fetch_endpoint_model_metadata

    routes = {
        "/api/v1/models": (200, {"models": [], **_generic_models_payload()}),
        "/v1/models": (200, _generic_models_payload()),
    }
    with _synthetic_gateway(routes) as base_url:
        with patch(
            "agent.model_metadata.detect_local_server_type", return_value="lm-studio"
        ):
            metadata = fetch_endpoint_model_metadata(base_url, force_refresh=True)

    assert metadata["synthetic/gateway-context-model"]["context_length"] == 872000


def test_true_lmstudio_models_shape_remains_supported():
    """A native LM Studio ``models`` payload still uses loaded context."""
    from agent.model_metadata import (
        detect_local_server_type,
        fetch_endpoint_model_metadata,
    )

    native_payload = {
        "models": [
            {
                "key": "synthetic/lmstudio-model",
                "id": "synthetic/lmstudio-model",
                "loaded_instances": [{"config": {"context_length": 196608}}],
            }
        ]
    }
    routes = {
        "/api/v1/models": (200, native_payload),
        "/v1/models": (404, {"error": "not found"}),
    }
    with _synthetic_gateway(routes) as base_url:
        with patch("agent.model_metadata._local_probe_disk_get", return_value=None):
            assert detect_local_server_type(base_url) == "lm-studio"
            metadata = fetch_endpoint_model_metadata(base_url, force_refresh=True)

    assert metadata["synthetic/lmstudio-model"]["context_length"] == 196608


def test_context_override_remains_available_when_gateway_has_no_context():
    """Explicit per-model configuration remains the compatibility fallback."""
    from agent.model_metadata import get_model_context_length

    routes = {
        "/api/v1/models": (
            200,
            {"object": "list", "data": [{"id": "synthetic/no-context"}]},
        ),
        "/v1/models": (
            200,
            {"object": "list", "data": [{"id": "synthetic/no-context"}]},
        ),
    }
    with _synthetic_gateway(routes) as base_url:
        context_length = get_model_context_length(
            "synthetic/no-context",
            base_url=base_url,
            provider="custom",
            config_context_length=123456,
        )

    assert context_length == 123456
