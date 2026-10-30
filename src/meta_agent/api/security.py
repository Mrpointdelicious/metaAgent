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
    """认证后的接入方身份：决定工具可见性等按客户端差异。"""

    name: str
    excluded_tools: frozenset[str] = field(default_factory=frozenset)


async def require_service_identity(request: Request) -> ClientIdentity:
    """验证调用方服务身份并识别客户端；开发环境允许显式不配置 Token。

    命中 primary / secondary token 分别返回对应 ClientIdentity，并写入
    request.state.client 供路由透传进请求上下文。
    """
    settings = request.app.state.container.settings
    credentials: HTTPAuthorizationCredentials | None = await _bearer(request)

    if not settings.service_bearer_token and not settings.service_bearer_token_secondary:
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

    provided = credentials.credentials
    if settings.service_bearer_token and hmac.compare_digest(
        provided, settings.service_bearer_token
    ):
        client = ClientIdentity("primary")
    elif settings.service_bearer_token_secondary and hmac.compare_digest(
        provided, settings.service_bearer_token_secondary
    ):
        client = ClientIdentity(
            "secondary", frozenset(settings.secondary_excluded_tools)
        )
    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="服务 Bearer Token 无效",
        )

    request.state.client = client
    return client
