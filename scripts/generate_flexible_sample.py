from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from scoreflow.omr.template import generate_template

sizes = [5, 7, 4, 8, 6, 5, 7, 7]
students = []
number = 1
for group, size in enumerate(sizes, start=1):
    for member in range(size):
        students.append({
            "student_id": f"flex-student-{number:02d}", "student_number": number,
            "name": f"演示学生{number}", "group_number": group, "is_leader": member == 0,
        })
        number += 1

out = ROOT / "var" / "phase1"
manifest = generate_template(
    out / "ScoreFlow-八组不等人数样表.pdf",
    out / "ScoreFlow-八组不等人数样表.manifest.json",
    project_id="flex-project-v1", period_id="flex-period-v1", sheet_id="flex-sheet-00000001",
    class_name="二四一五班", period_name="第3—4周", students=students,
)
print(f"八组样表已生成：{len(students)} 人，分组人数 {sizes}")
print(out / "ScoreFlow-八组不等人数样表.pdf")
