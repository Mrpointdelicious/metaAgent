"""
创建日期：2026-08-29
文件功能：对最终文案执行最小安全校验，阻止内部代码和空结论直接输出。
"""

import re


class ClaimValidator:
    """在接入完整事实引用验证器前提供保守输出门。"""

    _internal_code_pattern = re.compile(
        r"\b(?:SOURCE_RECONCILIATION_PENDING|controlled_test_only|unit_status)\b",
        re.IGNORECASE,
    )

    def validate(self, patient_message: str) -> str:
        message = patient_message.strip()
        if not message:
            return "暂时没有查到可用于回答的康复训练信息。"
        return self._internal_code_pattern.sub("相关内部状态", message)
