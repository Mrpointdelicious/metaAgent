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
    name: str = "primary"
    excluded_tools: frozenset[str] = field(default_factory=frozenset)


async def require_service_identity(request: Request) -> ClientIdentity:
    """验证调用方服务身份；开发环境允许显式不配置 Token。"""
    settings = request.app.state.container.settings
    credentials: HTTPAuthorizationCredentials | None = await _bearer(request)
    expected = settings.service_bearer_token
    secondary = settings.service_bearer_token_secondary
    if not expected and not secondary and settings.app_env != "production":
        client = ClientIdentity()
        request.state.client = client
        return client
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少服务 Bearer Token",
        )
    if expected and hmac.compare_digest(credentials.credentials, expected):
        client = ClientIdentity()
    elif secondary and hmac.compare_digest(credentials.credentials, secondary):
        client = ClientIdentity("secondary", frozenset(settings.secondary_excluded_tools))
    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="服务 Bearer Token 无效",
        )
    request.state.client = client
    return client
