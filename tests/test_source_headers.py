"""
创建日期：2026-08-29
文件功能：检查 Python 文件是否包含合法的标准文件头。
"""

import re
from datetime import datetime
from pathlib import Path

DATE_PATTERN = re.compile(r"创建日期：(\d{4}-\d{2}-\d{2})")


def test_all_python_files_have_required_header() -> None:
    project_root = Path(__file__).resolve().parents[1]

    python_files = [
        project_root / "main.py",
    ]

    python_files.extend((project_root / "src").rglob("*.py"))

    python_files.extend((project_root / "tests").rglob("*.py"))

    missing_header: list[str] = []
    invalid_date: list[str] = []

    for path in python_files:
        first_lines = "\n".join(path.read_text(encoding="utf-8").splitlines()[:5])

        date_match = DATE_PATTERN.search(first_lines)

        has_function = "文件功能：" in first_lines

        if not date_match or not has_function:
            missing_header.append(str(path.relative_to(project_root)))
            continue

        try:
            datetime.strptime(
                date_match.group(1),
                "%Y-%m-%d",
            )
        except ValueError:
            invalid_date.append(str(path.relative_to(project_root)))

    assert not missing_header, f"以下 Python 文件缺少标准文件头：{missing_header}"

    assert not invalid_date, f"以下 Python 文件创建日期格式非法：{invalid_date}"
