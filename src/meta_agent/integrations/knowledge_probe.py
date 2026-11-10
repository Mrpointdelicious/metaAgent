"""
创建日期：2026-11-10
文件功能：检查 knowledge-search-v1 接入配置或发送一次契约探测，不打印凭据与知识正文。
"""

import argparse
import asyncio
import json
from pathlib import Path
from uuid import uuid4

import httpx
from jsonschema import ValidationError as SchemaError
from pydantic import ValidationError
from pydantic_settings.exceptions import SettingsError

from meta_agent.config import Settings
from meta_agent.integrations.knowledge import KnowledgeServiceError, search, validate_contract


class ProbeConfigurationError(ValueError):
    pass


def _prepare(args):
    if args.env_file is not None and not args.env_file.is_file():
        raise ProbeConfigurationError("env_file_not_found")
    settings = Settings() if args.env_file is None else Settings(_env_file=args.env_file)
    source = next((item for item in settings.knowledge_sources if item.id == args.source), None)
    if source is None:
        raise ProbeConfigurationError("knowledge_source_not_configured")
    if args.domain not in source.domains:
        raise ProbeConfigurationError("knowledge_domain_not_configured")
    scope = {}
    if args.domain in source.scoped_domains:
        if not args.space_id or not args.space_id.strip():
            raise ProbeConfigurationError("knowledge_space_required")
        scope["space_id"] = args.space_id
    if not source.credential():
        raise ProbeConfigurationError("knowledge_credential_missing")
    payload = {
        "query": args.query,
        "domain": args.domain,
        "scope": scope,
        "limits": {"max_context_tokens": settings.knowledge_context_tokens},
    }
    validate_contract("KnowledgeSearchRequest", payload)
    return settings, source, payload


def _report(exit_code, **fields):
    print(json.dumps({"ok": exit_code == 0, **fields}, ensure_ascii=False))
    return exit_code


def main(argv=None):
    parser = argparse.ArgumentParser(description="Probe one configured knowledge-search-v1 source")
    parser.add_argument(
        "--env-file", type=Path, help="Explicit dotenv file; process env takes priority"
    )
    parser.add_argument("--source", required=True, help="Logical source ID in KNOWLEDGE_SOURCES")
    parser.add_argument("--domain", choices=("health", "help", "product"), required=True)
    parser.add_argument("--query", required=True, help="Non-sensitive integration test question")
    parser.add_argument("--space-id", help="Required for the source's scoped domains")
    parser.add_argument("--check-config", action="store_true", help="Validate locally without HTTP")
    args = parser.parse_args(argv)
    try:
        settings, source, payload = _prepare(args)
    except ProbeConfigurationError as exc:
        return _report(2, code=str(exc))
    except (ValidationError, SettingsError, OSError):
        # Validation errors can contain the input config, including inline credentials.
        return _report(2, code="invalid_configuration")
    except SchemaError:
        return _report(2, code="invalid_probe_request")

    info = {"source": source.id, "protocol": source.protocol, "transport": source.transport}
    if args.check_config:
        return _report(0, **info, check="configuration", network_verified=False)
    request_id = "knowledge-probe-" + uuid4().hex
    info.update(request_id=request_id)
    try:
        result = asyncio.run(
            search(
                source,
                payload,
                request_id=request_id,
                max_bytes=settings.max_tool_response_bytes,
            )
        )
    except KnowledgeServiceError as exc:
        return _report(3, **info, code=str(exc))
    except httpx.HTTPStatusError as exc:
        return _report(3, **info, code="knowledge_http_error", http_status=exc.response.status_code)
    except (httpx.TimeoutException, TimeoutError):
        return _report(3, **info, code="knowledge_timeout")
    except httpx.RequestError:
        return _report(3, **info, code="knowledge_unreachable")
    except (ValueError, SchemaError):
        return _report(4, **info, code="invalid_knowledge_response")
    return _report(
        0,
        **info,
        check="request",
        network_verified=True,
        status=result["status"],
        source_count=len(result["sources"]),
        gap_count=len(result["gaps"]),
        trace_id=result["trace_id"],
    )


if __name__ == "__main__":
    raise SystemExit(main())
