"""Hermes plugin for the loopback-only AISHPerf open-web tool service."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


TOOLSET = "aishperf_open_web"


def _base_url() -> str:
    value = os.getenv("AISH_PERF_OPEN_WEB_TOOL_URL", "http://127.0.0.1:5341").rstrip("/")
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("AISHPerf 工具服务必须使用本机 HTTP 地址。")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise RuntimeError("AISHPerf 工具服务地址格式无效。")
    return value


def _post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    token = os.getenv("AISH_PERF_OPEN_WEB_TOOL_TOKEN", "").strip()
    if not token:
        raise RuntimeError("AISH_PERF_OPEN_WEB_TOOL_TOKEN 未配置。")
    request = urllib.request.Request(
        _base_url() + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            value = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(f"AISHPerf 工具返回 HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"AISHPerf 工具服务不可达：{exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError("AISHPerf 工具返回值不是 JSON 对象。")
    return value


def _tool_response(path: str, args: dict[str, Any]) -> str:
    try:
        return json.dumps(_post(path, args), ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"ok": False, "error": str(exc)[:1000]}, ensure_ascii=False)


def _handle_search(args: dict, **kwargs) -> str:
    return _tool_response("/v1/search", args)


def _handle_preview(args: dict, **kwargs) -> str:
    return _tool_response("/v1/preview", args)


def _handle_selection(args: dict, **kwargs) -> str:
    return _tool_response("/v1/selection", args)


def _load_contract():
    import hashlib
    from pathlib import Path
    contract = json.loads(Path(__file__).with_name('tool-contract.json').read_text(encoding='utf-8'))
    body = {key: value for key, value in contract.items() if key != 'tool_contract_sha256'}
    encoded = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    digest = hashlib.sha256(encoded.encode('utf-8')).hexdigest()
    if (contract.get('tool_contract_sha256') != digest or contract.get('tool_version') != '3.0.0'
            or contract.get('contract_version') != 3 or contract.get('pipeline') != 'hermes-open-web-v3'
            or [tool['name'] for tool in contract.get('tools', [])] != ['open_web_search', 'open_web_preview', 'open_web_submit_selection']):
        raise RuntimeError('AISHPerf V3 tool contract hash/version mismatch')
    # Check the shipped manifest without requiring project/Pydantic imports.
    manifest = Path(__file__).with_name('plugin.yaml').read_text(encoding='utf-8')
    if 'version: 3.0.0' not in manifest or 'tool_contract_sha256: ' + digest not in manifest:
        raise RuntimeError('AISHPerf plugin manifest contract mismatch')
    return contract


TOOL_CONTRACT = _load_contract()
SEARCH_SCHEMA, PREVIEW_SCHEMA, SUBMIT_SCHEMA = TOOL_CONTRACT['tools']


def register(ctx) -> None:
    for schema, handler, emoji in (
        (SEARCH_SCHEMA, _handle_search, "🔎"),
        (PREVIEW_SCHEMA, _handle_preview, "📄"),
        (SUBMIT_SCHEMA, _handle_selection, "✅"),
    ):
        ctx.register_tool(name=schema['name'], toolset=TOOLSET, schema=schema, handler=handler, emoji=emoji)
