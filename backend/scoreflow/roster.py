from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass


_DIGIT_TRANSLATION = str.maketrans("０１２３４５６７８９", "0123456789")
_HEADER = re.compile(r"^\s*学号\s*[,，、\t ]+\s*姓名\s*$", re.IGNORECASE)
_ONLY_NUMBER = re.compile(r"^\s*([0-9０-９]+)\s*号?\s*$")
_ROW = re.compile(r"^\s*([0-9０-９]+)\s*号?(?:(?:\s*[,，、\t]\s*)|(?:\s+))(.+?)\s*$")


@dataclass(frozen=True)
class RosterParseResult:
    rows: list[dict[str, object]]
    errors: list[dict[str, object]]
    warnings: list[str]
    skipped_headers: list[int]

    @property
    def valid(self) -> bool:
        return not self.errors


def parse_roster_text(text: str) -> RosterParseResult:
    """Parse teacher-friendly `student number + name` lines without altering names."""
    rows: list[dict[str, object]] = []
    errors: list[dict[str, object]] = []
    skipped_headers: list[int] = []
    number_lines: dict[str, list[int]] = defaultdict(list)
    name_numbers: dict[str, list[str]] = defaultdict(list)

    for line_number, original in enumerate(text.lstrip("\ufeff").splitlines(), start=1):
        line = original.strip()
        if not line:
            continue
        if _HEADER.fullmatch(line):
            skipped_headers.append(line_number)
            continue
        match = _ROW.fullmatch(original)
        if not match:
            message = "缺少姓名" if _ONLY_NUMBER.fullmatch(original) else "请使用“学号 姓名”格式，学号必须位于行首"
            errors.append({"line": line_number, "text": original, "message": message})
            continue
        digits = match.group(1).translate(_DIGIT_TRANSLATION)
        name = match.group(2).strip()
        try:
            number_value = int(digits)
        except ValueError:
            number_value = 0
        if number_value <= 0:
            errors.append({"line": line_number, "text": original, "message": "学号必须是大于0的数字"})
            continue
        if not name:
            errors.append({"line": line_number, "text": original, "message": "缺少姓名"})
            continue
        number = str(number_value)
        number_lines[number].append(line_number)
        name_numbers[name].append(number)
        rows.append({"student_number": number, "name": name, "source_line": line_number})

    for number, lines in number_lines.items():
        if len(lines) > 1:
            errors.append({"line": lines[-1], "text": number, "message": f"学号 {number} 重复，出现在第 {'、'.join(map(str, lines))} 行"})
    warnings = [f"姓名“{name}”出现 {len(numbers)} 次（学号 {'、'.join(numbers)}），允许重名，请核对"
                for name, numbers in name_numbers.items() if len(numbers) > 1]
    if skipped_headers:
        warnings.insert(0, f"已跳过第 {'、'.join(map(str, skipped_headers))} 行明确表头")
    return RosterParseResult(rows=rows, errors=sorted(errors, key=lambda item: int(item["line"])),
                             warnings=warnings, skipped_headers=skipped_headers)
