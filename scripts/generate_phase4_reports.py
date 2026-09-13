from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from scoreflow.db import Store
from scoreflow.reports import generate_period_report


def roster():
    surnames = "赵钱孙李周吴郑"
    return [
        {
            "student_number": number,
            "name": f"{surnames[(number - 1) // 7]}同学{number}",
            "group_number": (number - 1) // 7 + 1,
            "is_leader": number % 7 == 1,
        }
        for number in range(1, 50)
    ]


def add_side(store, project_id, sheet_id, side, serial):
    job_id = store.create_scan_job(project_id)
    digest = f"{serial:064x}"
    asset_id, _ = store.create_scan_asset(project_id, f"demo-{side}.png", f"demo-{side}.png", digest, 1, job_id)
    run_id = store.create_recognition_run(asset_id, 0 if side == "front" else 1)
    observations = []
    for row in range(49):
        if side == "front":
            count = row % 3 + 1
            for slot in range(count):
                observations.append({"slot_id":f"front-r{row:02d}-c00-s{slot}", "classification":"slash_forward", "confidence":.99, "features":{}})
            if row % 4 == 0:
                observations.append({"slot_id":f"front-r{row:02d}-c03-s4", "classification":"slash_back", "confidence":.99, "features":{}})
            observations.append({"slot_id":f"front-r{row:02d}-c01-s4", "classification":"x", "confidence":.99, "features":{}})
        elif row % 5 == 0:
            observations.append({"slot_id":f"back-r{row:02d}-c03-s0", "classification":"slash_forward", "confidence":.99, "features":{}})
    store.save_observations(run_id, observations)
    store.update_recognition_run(run_id, status="ready", sheet_id=sheet_id, side=side)
    store.mark_side_candidate(sheet_id, side)
    store.adopt_run(run_id)
    return run_id


def main():
    work = ROOT / "tmp" / "pdfs" / "phase4-demo"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    store = Store(work / "scoreflow.sqlite3")
    project_id = store.create_project("二四一五班", "2026—2027")
    store.import_students(project_id, roster())
    period_id = store.create_period(project_id, "第3—4周", "2026-09-15", "2026-09-28")
    sheet_id = store.start_period(period_id)
    front_run = add_side(store, project_id, sheet_id, "front", 1)
    add_side(store, project_id, sheet_id, "back", 2)
    store.add_scan_note(front_run, "15：主动搬运班级器材", student_number="15", rule_name="服务＋")
    store.begin_settlement(period_id)
    store.post_sheet(sheet_id, "phase4-demo-post")
    store.close_period(period_id)
    data = store.period_results(period_id)
    output = ROOT / "output" / "pdf"
    output.mkdir(parents=True, exist_ok=True)
    teacher = output / "ScoreFlow-Phase4-教师存档报告.pdf"
    display = output / "ScoreFlow-Phase4-教室展示报告.pdf"
    generate_period_report(teacher, data, "teacher", False)
    generate_period_report(display, data, "display", False)
    print(teacher)
    print(display)


if __name__ == "__main__":
    main()
