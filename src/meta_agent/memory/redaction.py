"""
创建日期：2026-10-30
文件功能：清除长期消息中的凭证、手机号和带授权参数的链接。
"""

import json
import re

PRIVATE_KEYS = {
    "password",
    "token",
    "identitycontexttoken",
    "accessgrant",
    "apikey",
    "apitoken",
    "systemcontext",
    "phone",
    "patientphone",
    "doctorphones",
    "displaynameprivate",
    "patientname",
    "loginname",
    "authorization",
}


def redact_text(value: str) -> str:
    value = re.sub(
        r"https?://[^\s<>\]\)]+(?:access_grant|token|password|api_key)=[^\s<>\]\)]*",
        "[授权链接已隐藏]",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "[手机号已隐藏]", value)
    return re.sub(
        r"(?i)(password|identity_context_token|api_key|apikey|apitoken|bearer)\s*[:= ]\s*[^\s,;]+",
        r"\1=[已隐藏]",
        value,
    )


def redact_data(value):
    if isinstance(value, dict):
        return {
            key: redact_data(item)
            for key, item in value.items()
            if re.sub(r"[^a-z]", "", key.casefold()) not in PRIVATE_KEYS
        }
    if isinstance(value, list):
        return [redact_data(item) for item in value]
    return redact_text(value) if isinstance(value, str) else value


def retained_messages(messages):
    result = []
    for message in messages:
        message = message.model_copy(deep=True)
        content = message.content
        if isinstance(content, str):
            try:
                content = json.dumps(redact_data(json.loads(content)), ensure_ascii=False)
            except (TypeError, ValueError):
                content = redact_text(content)
        else:
            content = redact_data(content)
        message.content = content
        if getattr(message, "tool_calls", None):
            message.tool_calls = redact_data(message.tool_calls)
        message.additional_kwargs = redact_data(
            {
                key: value
                for key, value in message.additional_kwargs.items()
                if key not in {"tool_calls", "function_call"}
            }
        )
        message.response_metadata = redact_data(message.response_metadata)
        result.append(message)
    return result
