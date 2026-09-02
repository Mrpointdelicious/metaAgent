"""
创建日期：2026-08-29
文件功能：确保所有预创建 Python 文件都包含创建日期和文件功能说明。
"""

from pathlib import Path


def test_all_python_files_have_required_header() -> None:
    project_root = Path(__file__).resolve().parents[1]
    python_files = [project_root / "main.py"]
    python_files.extend((project_root / "src").rglob("*.py"))
    python_files.extend((project_root / "tests").rglob("*.py"))

    missing: list[str] = []
    for path in python_files:
        first_lines = "\n".join(path.read_text(encoding="utf-8").splitlines()[:5])
        if "创建日期：2026-08-29" not in first_lines or "文件功能：" not in first_lines:
            missing.append(str(path.relative_to(project_root)))

    assert not missing, f"以下 Python 文件缺少标准文件头：{missing}"
