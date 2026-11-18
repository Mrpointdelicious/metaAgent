"""
创建日期：2026-08-29
文件功能：校验可信接入方的 Bearer Token，防止前端直接伪造患者范围。
"""

import hmac
from dataclasses import dataclass, field

from fastapi import HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class ClientIdentity:
    """认证后的接入方身份：决定工具可见性、默认分支与专属提示词。"""

    name: str
    excluded_tools: frozenset[str] = field(default_factory=frozenset)
    # None 表示不限制；非空即白名单，只有列出的工具可见。
    allowed_tools: frozenset[str] | None = None
    # 强制流程分支（default / consultation_room / ai_* 独立流程）；
    # 空表示按 inputs.pos 判定。
    flow: str = ""
    prompt_suffix: str = ""


def client_registry(settings) -> list[tuple[bytes, ClientIdentity]]:
    """把旧 primary/secondary 与 service_clients 合并成一张令牌表。

    旧字段始终排在前面，保证既有接入方行为不变；同一令牌重复由配置校验拦截。
    """
    entries: list[tuple[bytes, ClientIdentity]] = []
    if settings.service_bearer_token.strip():
        entries.append((settings.service_bearer_token.strip().encode(), ClientIdentity("primary")))
    if settings.service_bearer_token_secondary.strip():
        entries.append(
            (
                settings.service_bearer_token_secondary.strip().encode(),
                ClientIdentity("secondary", frozenset(settings.secondary_excluded_tools)),
            )
        )
    for client in settings.service_clients:
        entries.append(
            (
                client.token.get_secret_value().encode(),
                ClientIdentity(
                    name=client.name,
                    excluded_tools=frozenset(client.excluded_tools),
                    allowed_tools=frozenset(client.allowed_tools) if client.allowed_tools else None,
                    flow=client.flow,
                    prompt_suffix=client.prompt_suffix,
                ),
            )
        )
    return entries


async def require_service_identity(request: Request) -> ClientIdentity:
    """验证调用方服务身份并识别客户端；开发环境允许显式不配置 Token。

    命中任一已配置令牌即返回对应 ClientIdentity，并写入 request.state.client
    供路由透传进请求上下文。旧 primary / secondary 与新增的多客户端令牌等价。
    """
    settings = request.app.state.container.settings
    credentials: HTTPAuthorizationCredentials | None = await _bearer(request)
    registry = client_registry(settings)

    if not registry:
        if settings.app_env != "production":
            client = ClientIdentity("primary")
            request.state.client = client
            return client
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少服务 Bearer Token",
        )

    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少服务 Bearer Token",
        )

    provided = credentials.credentials.encode()
    matched: ClientIdentity | None = None
    # 遍历全部令牌，避免命中位置影响响应耗时。
    for expected, identity in registry:
        if hmac.compare_digest(provided, expected) and matched is None:
            matched = identity
    if matched is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="服务 Bearer Token 无效",
        )

    request.state.client = matched
    return matched
