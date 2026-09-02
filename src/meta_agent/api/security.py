"""
创建日期：2026-08-29
文件功能：校验可信接入方的 Bearer Token，防止前端直接伪造患者范围。
"""

import hmac

from fastapi import HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

_bearer = HTTPBearer(auto_error=False)


async def require_service_identity(request: Request) -> None:
    """验证调用方服务身份；开发环境允许显式不配置 Token。"""
    settings = request.app.state.container.settings
    credentials: HTTPAuthorizationCredentials | None = await _bearer(request)
    expected = settings.service_bearer_token
    if not expected and settings.app_env != "production":
        return
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少服务 Bearer Token",
        )
    if not hmac.compare_digest(credentials.credentials, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="服务 Bearer Token 无效",
        )
