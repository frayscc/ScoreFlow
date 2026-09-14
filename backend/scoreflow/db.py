from __future__ import annotations

import csv
import io
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Iterable, Iterator, Optional

from .scoring import aggregate_observations, summarize_results


SCHEMA_VERSION = 10

MIGRATION_1 = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS schema_version(version INTEGER NOT NULL);
INSERT INTO schema_version(version) SELECT 0 WHERE NOT EXISTS (SELECT 1 FROM schema_version);

CREATE TABLE projects(
  id TEXT PRIMARY KEY, class_name TEXT NOT NULL, school_year TEXT NOT NULL,
  group_count INTEGER NOT NULL DEFAULT 7 CHECK(group_count BETWEEN 1 AND 20),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE students(
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
  student_number TEXT NOT NULL, name TEXT NOT NULL, group_number INTEGER NOT NULL CHECK(group_number BETWEEN 1 AND 20),
  is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)), sort_order INTEGER NOT NULL,
  is_unassigned INTEGER NOT NULL DEFAULT 0 CHECK(is_unassigned IN (0,1)),
  UNIQUE(project_id, student_number)
);
CREATE TABLE class_groups(
  project_id TEXT NOT NULL REFERENCES projects(id), group_number INTEGER NOT NULL CHECK(group_number BETWEEN 1 AND 20),
  leader_student_id TEXT REFERENCES students(id), PRIMARY KEY(project_id, group_number)
);
CREATE TABLE rules(
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), name TEXT NOT NULL,
  side TEXT NOT NULL CHECK(side IN ('front','back')), unit_score INTEGER NOT NULL,
  sort_order INTEGER NOT NULL, is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
  CHECK((side='front' AND unit_score>0) OR (side='back' AND unit_score<0)),
  UNIQUE(project_id, side, sort_order)
);
CREATE TABLE periods(
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), name TEXT NOT NULL,
  start_date TEXT NOT NULL, expected_end_date TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft'
    CHECK(status IN ('draft','active','settling','closed')),
  base_score INTEGER NOT NULL DEFAULT 100, started_at TEXT, closed_at TEXT, result_version INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE period_grouping_revisions(
  period_id TEXT PRIMARY KEY REFERENCES periods(id), revision INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE period_draft_members(
  period_id TEXT NOT NULL REFERENCES periods(id), student_id TEXT NOT NULL REFERENCES students(id),
  group_number INTEGER CHECK(group_number BETWEEN 1 AND 20), sort_order INTEGER NOT NULL,
  PRIMARY KEY(period_id,student_id), UNIQUE(period_id,sort_order)
);
CREATE TABLE period_draft_groups(
  period_id TEXT NOT NULL REFERENCES periods(id), group_number INTEGER NOT NULL CHECK(group_number BETWEEN 1 AND 20),
  leader_student_id TEXT REFERENCES students(id), PRIMARY KEY(period_id,group_number)
);
CREATE TABLE period_students(
  period_id TEXT NOT NULL REFERENCES periods(id), student_id TEXT NOT NULL REFERENCES students(id),
  student_number TEXT NOT NULL, name TEXT NOT NULL, group_number INTEGER NOT NULL, row_index INTEGER NOT NULL,
  PRIMARY KEY(period_id, student_id), UNIQUE(period_id, student_number), UNIQUE(period_id, row_index)
);
CREATE TABLE period_groups(
  period_id TEXT NOT NULL REFERENCES periods(id), group_number INTEGER NOT NULL,
  leader_student_id TEXT NOT NULL, PRIMARY KEY(period_id, group_number),
  FOREIGN KEY(period_id, leader_student_id) REFERENCES period_students(period_id, student_id)
);
CREATE TABLE period_rules(
  period_id TEXT NOT NULL REFERENCES periods(id), rule_id TEXT NOT NULL REFERENCES rules(id),
  name TEXT NOT NULL, side TEXT NOT NULL, unit_score INTEGER NOT NULL, sort_order INTEGER NOT NULL,
  PRIMARY KEY(period_id, rule_id), UNIQUE(period_id, side, sort_order)
);
CREATE TABLE paper_sheets(
  id TEXT PRIMARY KEY, period_id TEXT NOT NULL REFERENCES periods(id), sheet_number INTEGER NOT NULL,
  template_id TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'issued' CHECK(status IN ('issued','posted','void_unused')),
  issued_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(period_id, sheet_number)
);
CREATE TABLE paper_sides(
  sheet_id TEXT NOT NULL REFERENCES paper_sheets(id), side TEXT NOT NULL CHECK(side IN ('front','back')),
  status TEXT NOT NULL DEFAULT 'missing' CHECK(status IN ('missing','candidate','reviewed','confirmed_blank','posted')),
  PRIMARY KEY(sheet_id, side)
);
CREATE TABLE template_versions(
  id TEXT PRIMARY KEY, template_name TEXT NOT NULL, geometry_json TEXT NOT NULL,
  font_sha256 TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE scan_jobs(
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), status TEXT NOT NULL,
  stage TEXT NOT NULL, total_pages INTEGER NOT NULL DEFAULT 0, completed_pages INTEGER NOT NULL DEFAULT 0,
  cancel_requested INTEGER NOT NULL DEFAULT 0, error TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  scan_asset_id TEXT, is_duplicate INTEGER NOT NULL DEFAULT 0 CHECK(is_duplicate IN (0,1)),
  original_filename TEXT, sha256 TEXT
);
CREATE TABLE scan_assets(
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), original_filename TEXT NOT NULL,
  job_id TEXT REFERENCES scan_jobs(id), stored_path TEXT NOT NULL, sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, delete_requested INTEGER NOT NULL DEFAULT 0 CHECK(delete_requested IN (0,1)),
  UNIQUE(project_id,sha256)
);
CREATE TABLE recognition_runs(
  id TEXT PRIMARY KEY, scan_asset_id TEXT NOT NULL REFERENCES scan_assets(id), page_index INTEGER NOT NULL,
  sheet_id TEXT REFERENCES paper_sheets(id), side TEXT CHECK(side IN ('front','back')), corrected_path TEXT,
  algorithm_version TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('queued','processing','needs_identity','failed','ready','reviewed')),
  adopted INTEGER NOT NULL DEFAULT 0 CHECK(adopted IN (0,1)), quality_json TEXT, error TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, removed_at TEXT, removal_reason TEXT,
  UNIQUE(scan_asset_id,page_index)
);
CREATE TABLE slot_observations(
  run_id TEXT NOT NULL REFERENCES recognition_runs(id), slot_id TEXT NOT NULL, auto_class TEXT NOT NULL,
  confidence REAL NOT NULL, manual_class TEXT, features_json TEXT NOT NULL,
  PRIMARY KEY(run_id,slot_id)
);
CREATE TABLE audit_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL, entity_id TEXT NOT NULL,
  details_json TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX one_adopted_run_per_side ON recognition_runs(sheet_id,side) WHERE adopted=1;
CREATE TABLE scan_notes(
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES recognition_runs(id), student_number TEXT,
  rule_name TEXT, note_text TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE posting_batches(
  id TEXT PRIMARY KEY, sheet_id TEXT NOT NULL REFERENCES paper_sheets(id), idempotency_key TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK(status IN ('posted','reversed')), reverses_batch_id TEXT REFERENCES posting_batches(id),
  front_run_id TEXT REFERENCES recognition_runs(id), back_run_id TEXT REFERENCES recognition_runs(id),
  reason TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX one_active_posting_per_sheet ON posting_batches(sheet_id)
  WHERE status='posted' AND reverses_batch_id IS NULL;
CREATE TABLE ledger_entries(
  id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES posting_batches(id), source_entry_id TEXT REFERENCES ledger_entries(id),
  student_id TEXT NOT NULL REFERENCES students(id), rule_id TEXT NOT NULL REFERENCES rules(id),
  mark_count INTEGER NOT NULL, unit_score INTEGER NOT NULL, amount INTEGER NOT NULL, slot_ids_json TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CHECK(amount=mark_count*unit_score)
);
CREATE TABLE report_exports(
  id TEXT PRIMARY KEY, period_id TEXT NOT NULL REFERENCES periods(id), result_version INTEGER NOT NULL,
  variant TEXT NOT NULL CHECK(variant IN ('teacher','display')), is_draft INTEGER NOT NULL CHECK(is_draft IN (0,1)),
  source_digest TEXT NOT NULL, stored_path TEXT NOT NULL, sha256 TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(period_id,result_version,variant,is_draft,source_digest)
);
UPDATE schema_version SET version=10;
"""

MIGRATION_2 = """
PRAGMA foreign_keys=OFF;
ALTER TABLE projects ADD COLUMN group_count INTEGER NOT NULL DEFAULT 7 CHECK(group_count BETWEEN 1 AND 20);
CREATE TABLE students_v2(
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), student_number TEXT NOT NULL,
  name TEXT NOT NULL, group_number INTEGER NOT NULL CHECK(group_number BETWEEN 1 AND 20),
  is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)), sort_order INTEGER NOT NULL,
  UNIQUE(project_id, student_number)
);
INSERT INTO students_v2 SELECT * FROM students;
DROP TABLE students;
ALTER TABLE students_v2 RENAME TO students;
CREATE TABLE class_groups_v2(
  project_id TEXT NOT NULL REFERENCES projects(id), group_number INTEGER NOT NULL CHECK(group_number BETWEEN 1 AND 20),
  leader_student_id TEXT REFERENCES students(id), PRIMARY KEY(project_id, group_number)
);
INSERT INTO class_groups_v2 SELECT * FROM class_groups;
DROP TABLE class_groups;
ALTER TABLE class_groups_v2 RENAME TO class_groups;
UPDATE schema_version SET version=2;
PRAGMA user_version=2;
PRAGMA foreign_keys=ON;
"""

MIGRATION_3 = """
CREATE TABLE template_versions(
  id TEXT PRIMARY KEY, template_name TEXT NOT NULL, geometry_json TEXT NOT NULL,
  font_sha256 TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
UPDATE schema_version SET version=3;
PRAGMA user_version=3;
"""

MIGRATION_4 = """
CREATE TABLE scan_assets(
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), original_filename TEXT NOT NULL,
  stored_path TEXT NOT NULL, sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(project_id,sha256)
);
CREATE TABLE recognition_runs(
  id TEXT PRIMARY KEY, scan_asset_id TEXT NOT NULL REFERENCES scan_assets(id), page_index INTEGER NOT NULL,
  sheet_id TEXT REFERENCES paper_sheets(id), side TEXT CHECK(side IN ('front','back')), corrected_path TEXT,
  algorithm_version TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('queued','processing','needs_identity','failed','ready','reviewed')),
  adopted INTEGER NOT NULL DEFAULT 0 CHECK(adopted IN (0,1)), quality_json TEXT, error TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(scan_asset_id,page_index)
);
CREATE TABLE slot_observations(
  run_id TEXT NOT NULL REFERENCES recognition_runs(id), slot_id TEXT NOT NULL, auto_class TEXT NOT NULL,
  confidence REAL NOT NULL, manual_class TEXT, features_json TEXT NOT NULL,
  PRIMARY KEY(run_id,slot_id)
);
CREATE TABLE audit_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL, entity_id TEXT NOT NULL,
  details_json TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
UPDATE schema_version SET version=4;
PRAGMA user_version=4;
"""

MIGRATION_5 = """
CREATE TABLE scan_jobs(
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), status TEXT NOT NULL,
  stage TEXT NOT NULL, total_pages INTEGER NOT NULL DEFAULT 0, completed_pages INTEGER NOT NULL DEFAULT 0,
  cancel_requested INTEGER NOT NULL DEFAULT 0, error TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
ALTER TABLE scan_assets ADD COLUMN job_id TEXT REFERENCES scan_jobs(id);
UPDATE schema_version SET version=5;
PRAGMA user_version=5;
"""

MIGRATION_6 = """
CREATE UNIQUE INDEX one_adopted_run_per_side ON recognition_runs(sheet_id,side) WHERE adopted=1;
CREATE TABLE scan_notes(
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES recognition_runs(id), student_number TEXT,
  rule_name TEXT, note_text TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
UPDATE schema_version SET version=6;
PRAGMA user_version=6;
"""

MIGRATION_7 = """
CREATE TABLE posting_batches(
  id TEXT PRIMARY KEY, sheet_id TEXT NOT NULL REFERENCES paper_sheets(id), idempotency_key TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK(status IN ('posted','reversed')), reverses_batch_id TEXT REFERENCES posting_batches(id),
  reason TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX one_active_posting_per_sheet ON posting_batches(sheet_id)
  WHERE status='posted' AND reverses_batch_id IS NULL;
CREATE TABLE ledger_entries(
  id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES posting_batches(id), source_entry_id TEXT REFERENCES ledger_entries(id),
  student_id TEXT NOT NULL REFERENCES students(id), rule_id TEXT NOT NULL REFERENCES rules(id),
  mark_count INTEGER NOT NULL, unit_score INTEGER NOT NULL, amount INTEGER NOT NULL, slot_ids_json TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CHECK(amount=mark_count*unit_score)
);
CREATE TABLE report_exports(
  id TEXT PRIMARY KEY, period_id TEXT NOT NULL REFERENCES periods(id), result_version INTEGER NOT NULL,
  variant TEXT NOT NULL CHECK(variant IN ('teacher','display')), is_draft INTEGER NOT NULL CHECK(is_draft IN (0,1)),
  source_digest TEXT NOT NULL, stored_path TEXT NOT NULL, sha256 TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(period_id,result_version,variant,is_draft,source_digest)
);
UPDATE schema_version SET version=7;
PRAGMA user_version=7;
"""

MIGRATION_8 = """
ALTER TABLE posting_batches ADD COLUMN front_run_id TEXT REFERENCES recognition_runs(id);
ALTER TABLE posting_batches ADD COLUMN back_run_id TEXT REFERENCES recognition_runs(id);
UPDATE schema_version SET version=8;
PRAGMA user_version=8;
"""

MIGRATION_9 = """
ALTER TABLE students ADD COLUMN is_unassigned INTEGER NOT NULL DEFAULT 0 CHECK(is_unassigned IN (0,1));
CREATE TABLE period_grouping_revisions(
  period_id TEXT PRIMARY KEY REFERENCES periods(id), revision INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE period_draft_members(
  period_id TEXT NOT NULL REFERENCES periods(id), student_id TEXT NOT NULL REFERENCES students(id),
  group_number INTEGER CHECK(group_number BETWEEN 1 AND 20), sort_order INTEGER NOT NULL,
  PRIMARY KEY(period_id,student_id), UNIQUE(period_id,sort_order)
);
CREATE TABLE period_draft_groups(
  period_id TEXT NOT NULL REFERENCES periods(id), group_number INTEGER NOT NULL CHECK(group_number BETWEEN 1 AND 20),
  leader_student_id TEXT REFERENCES students(id), PRIMARY KEY(period_id,group_number)
);
INSERT INTO period_grouping_revisions(period_id)
  SELECT id FROM periods WHERE status='draft';
INSERT INTO period_draft_members(period_id,student_id,group_number,sort_order)
  SELECT p.id,s.id,s.group_number,s.sort_order FROM periods p JOIN students s ON s.project_id=p.project_id
  WHERE p.status='draft' AND s.is_active=1;
INSERT INTO period_draft_groups(period_id,group_number,leader_student_id)
  SELECT p.id,cg.group_number,cg.leader_student_id FROM periods p
  JOIN class_groups cg ON cg.project_id=p.project_id WHERE p.status='draft';
UPDATE schema_version SET version=9;
PRAGMA user_version=9;
"""

MIGRATION_10 = """
ALTER TABLE scan_assets ADD COLUMN delete_requested INTEGER NOT NULL DEFAULT 0 CHECK(delete_requested IN (0,1));
ALTER TABLE recognition_runs ADD COLUMN removed_at TEXT;
ALTER TABLE recognition_runs ADD COLUMN removal_reason TEXT;
ALTER TABLE scan_jobs ADD COLUMN scan_asset_id TEXT;
ALTER TABLE scan_jobs ADD COLUMN is_duplicate INTEGER NOT NULL DEFAULT 0 CHECK(is_duplicate IN (0,1));
ALTER TABLE scan_jobs ADD COLUMN original_filename TEXT;
ALTER TABLE scan_jobs ADD COLUMN sha256 TEXT;
UPDATE scan_jobs SET scan_asset_id=(SELECT id FROM scan_assets WHERE scan_assets.job_id=scan_jobs.id);
UPDATE scan_jobs SET original_filename=(SELECT original_filename FROM scan_assets WHERE scan_assets.job_id=scan_jobs.id),
  sha256=(SELECT sha256 FROM scan_assets WHERE scan_assets.job_id=scan_jobs.id);
UPDATE schema_version SET version=10;
PRAGMA user_version=10;
"""


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.migrate()

    def close(self) -> None:
        with self._lock:
            self.connection.close()

    def backup_to(self, path: Path) -> None:
        """Create a transactionally consistent snapshot with SQLite's Backup API."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            target = sqlite3.connect(path)
            try:
                self.connection.backup(target)
            finally:
                target.close()

    def cancel_active_scan_jobs(self) -> None:
        with self._lock, self.connection:
            self.connection.execute(
                "UPDATE scan_jobs SET cancel_requested=1 WHERE status IN ('queued','processing')"
            )

    def migrate(self) -> None:
        with self._lock:
            current = self.connection.execute("PRAGMA user_version").fetchone()[0]
            if current == 0:
                with self.connection:
                    self.connection.executescript(MIGRATION_1)
                    self.connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            elif current == 1:
                self.connection.executescript(MIGRATION_2)
                problems = self.connection.execute("PRAGMA foreign_key_check").fetchall()
                if problems:
                    raise RuntimeError("数据库升级后的外键检查失败")
                current = 2
            if current == 2:
                self.connection.executescript(MIGRATION_3)
                current = 3
            if current == 3:
                self.connection.executescript(MIGRATION_4)
                current = 4
            if current == 4:
                self.connection.executescript(MIGRATION_5)
                current = 5
            if current == 5:
                self.connection.executescript(MIGRATION_6)
                current = 6
            if current == 6:
                self.connection.executescript(MIGRATION_7)
                current = 7
            if current == 7:
                self.connection.executescript(MIGRATION_8)
                current = 8
            if current == 8:
                self.connection.executescript(MIGRATION_9)
                current = 9
            if current == 9:
                self.connection.executescript(MIGRATION_10)
            elif current > SCHEMA_VERSION:
                raise RuntimeError(f"数据库版本 {current} 高于程序支持版本 {SCHEMA_VERSION}")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock, self.connection:
            yield self.connection

    def create_project(self, class_name: str, school_year: str, group_count: int = 7) -> str:
        if not 1 <= group_count <= 20:
            raise ValueError("组数必须在 1—20 之间")
        project_id = str(uuid.uuid4())
        with self._lock, self.connection:
            self.connection.execute("INSERT INTO projects(id,class_name,school_year,group_count) VALUES(?,?,?,?)", (project_id, class_name.strip(), school_year.strip(), group_count))
            self.connection.executemany("INSERT INTO class_groups(project_id,group_number) VALUES(?,?)", [(project_id, number) for number in range(1, group_count + 1)])
            self._insert_default_rules(project_id, self.connection)
        return project_id

    def list_projects(self) -> list[dict[str, object]]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT p.*,count(s.id) AS student_count FROM projects p LEFT JOIN students s ON s.project_id=p.id GROUP BY p.id ORDER BY p.created_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def list_students(self, project_id: str) -> list[dict[str, object]]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT s.id,s.student_number,s.name,CASE WHEN s.is_unassigned=1 THEN NULL ELSE s.group_number END group_number,s.is_active,s.sort_order,"
                "CASE WHEN cg.leader_student_id=s.id THEN 1 ELSE 0 END is_leader "
                "FROM students s LEFT JOIN class_groups cg ON cg.project_id=s.project_id AND cg.group_number=s.group_number "
                "WHERE s.project_id=? ORDER BY s.group_number,s.sort_order",
                (project_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def update_student(self, project_id: str, student_id: str, *, name: str, group_number: int, is_leader: bool) -> None:
        with self._lock, self.connection:
            project = self.connection.execute("SELECT group_count FROM projects WHERE id=?", (project_id,)).fetchone()
            student = self.connection.execute("SELECT * FROM students WHERE id=? AND project_id=?", (student_id, project_id)).fetchone()
            if not project or not student or not name.strip() or not 1 <= group_number <= project["group_count"]:
                raise ValueError("学生信息或组号无效")
            self.connection.execute("UPDATE students SET name=?,group_number=?,is_unassigned=0 WHERE id=?", (name.strip(), group_number, student_id))
            self.connection.execute("UPDATE class_groups SET leader_student_id=NULL WHERE project_id=? AND leader_student_id=?", (project_id, student_id))
            if is_leader:
                self.connection.execute("UPDATE class_groups SET leader_student_id=? WHERE project_id=? AND group_number=?", (student_id, project_id, group_number))

    def list_rules(self, project_id: str) -> list[dict[str, object]]:
        with self._lock:
            return [dict(row) for row in self.connection.execute(
                "SELECT id,name,side,unit_score,sort_order,is_active FROM rules WHERE project_id=? ORDER BY side,sort_order", (project_id,)
            )]

    def replace_rules(self, project_id: str, rules: list[dict[str, object]]) -> None:
        front = [r for r in rules if r["side"] == "front"]
        back = [r for r in rules if r["side"] == "back"]
        if not 1 <= len(front) <= 7 or not 1 <= len(back) <= 6:
            raise ValueError("正面项目须为1—7个，背面项目须为1—6个")
        for rule in rules:
            score = int(rule["unit_score"])
            if not str(rule["name"]).strip() or score == 0 or score % 5 or (rule["side"] == "front") != (score > 0):
                raise ValueError("项目名称、面别或分值无效；分值须以5为步长")
        with self._lock, self.connection:
            self.connection.execute("UPDATE rules SET is_active=0,sort_order=-rowid WHERE project_id=? AND is_active=1", (project_id,))
            self.connection.executemany(
                "INSERT INTO rules(id,project_id,name,side,unit_score,sort_order,is_active) VALUES(?,?,?,?,?,?,1)",
                [(str(uuid.uuid4()), project_id, str(r["name"]).strip(), r["side"], int(r["unit_score"]), i) for side in ("front","back") for i,r in enumerate([x for x in rules if x["side"] == side])],
            )

    def add_default_rules(self, project_id: str) -> None:
        with self._lock, self.connection:
            self._insert_default_rules(project_id, self.connection)

    @staticmethod
    def _insert_default_rules(project_id: str, connection: sqlite3.Connection) -> None:
        front = ["作业＋", "课堂＋", "成绩＋", "服务＋", "值日＋", "活动＋", "其他＋"]
        back = ["作业－", "课堂－", "值日－", "迟到－", "晚归－", "其他－"]
        connection.executemany(
            "INSERT INTO rules(id,project_id,name,side,unit_score,sort_order) VALUES(?,?,?,?,?,?)",
            [(str(uuid.uuid4()), project_id, name, "front", 5, i) for i, name in enumerate(front)]
            + [(str(uuid.uuid4()), project_id, name, "back", -5, i) for i, name in enumerate(back)],
        )

    def import_students(self, project_id: str, rows: Iterable[dict[str, object]]) -> list[str]:
        normalized = []
        seen = set()
        with self._lock:
            project = self.connection.execute("SELECT group_count FROM projects WHERE id=?", (project_id,)).fetchone()
        if not project:
            raise ValueError("项目不存在")
        for index, row in enumerate(rows):
            number = str(row["student_number"]).strip()
            name = str(row["name"]).strip()
            group = int(row["group_number"])
            if not number or not name or not 1 <= group <= project["group_count"]:
                raise ValueError(f"第 {index + 1} 行字段无效")
            if number in seen:
                raise ValueError(f"名单内学号重复：{number}")
            seen.add(number)
            normalized.append((str(uuid.uuid4()), project_id, number, name, group, index, bool(row.get("is_leader", False))))
        ids = []
        try:
            with self._lock, self.connection:
                for student_id, pid, number, name, group, order, _ in normalized:
                    self.connection.execute(
                        "INSERT INTO students(id,project_id,student_number,name,group_number,sort_order) VALUES(?,?,?,?,?,?)",
                        (student_id, pid, number, name, group, order),
                    )
                    ids.append(student_id)
                for student_id, _, _, _, group, _, is_leader in normalized:
                    if is_leader:
                        if self.connection.execute("SELECT leader_student_id FROM class_groups WHERE project_id=? AND group_number=?", (project_id, group)).fetchone()[0]:
                            raise ValueError(f"第 {group} 组有多个组长")
                        self.connection.execute("UPDATE class_groups SET leader_student_id=? WHERE project_id=? AND group_number=?", (student_id, project_id, group))
                self._append_students_to_drafts(project_id, [(row[0], row[4]) for row in normalized], self.connection)
        except sqlite3.IntegrityError as exc:
            raise ValueError("学号重复或名单关联无效，未导入任何学生") from exc
        return ids

    @staticmethod
    def _canonical_number(value: object) -> str:
        text = str(value).strip()
        return str(int(text)) if text.isdigit() and int(text) > 0 else text

    def roster_import_preview(self, project_id: str, rows: list[dict[str, object]]) -> list[dict[str, object]]:
        with self._lock:
            if not self.connection.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone():
                raise ValueError("项目不存在")
            existing_rows = self.connection.execute(
                "SELECT id,student_number,name,group_number,is_unassigned FROM students WHERE project_id=? AND is_active=1",
                (project_id,),
            ).fetchall()
        existing = {self._canonical_number(row["student_number"]): row for row in existing_rows}
        preview = []
        for row in rows:
            number = self._canonical_number(row["student_number"])
            current = existing.get(number)
            status = "new" if not current else ("unchanged" if current["name"] == row["name"] else "name_difference")
            preview.append({**row, "student_number": number, "status": status,
                            "existing_name": current["name"] if current else None,
                            "existing_student_id": current["id"] if current else None,
                            "existing_group_number": None if current and current["is_unassigned"] else (current["group_number"] if current else None)})
        return preview

    def import_roster_students(self, project_id: str, rows: list[dict[str, object]]) -> dict[str, int]:
        preview = self.roster_import_preview(project_id, rows)
        new_rows = [row for row in preview if row["status"] == "new"]
        with self._lock, self.connection:
            start = self.connection.execute("SELECT COALESCE(MAX(sort_order),-1)+1 FROM students WHERE project_id=?", (project_id,)).fetchone()[0]
            inserted = []
            for index, row in enumerate(new_rows):
                student_id = str(uuid.uuid4())
                self.connection.execute(
                    "INSERT INTO students(id,project_id,student_number,name,group_number,sort_order,is_unassigned) VALUES(?,?,?,?,1,?,1)",
                    (student_id, project_id, row["student_number"], str(row["name"]).strip(), start + index),
                )
                inserted.append((student_id, None))
            self._append_students_to_drafts(project_id, inserted, self.connection)
        return {"inserted": len(new_rows), "unchanged": sum(row["status"] == "unchanged" for row in preview),
                "name_differences": sum(row["status"] == "name_difference" for row in preview)}

    @staticmethod
    def _append_students_to_drafts(project_id: str, students: list[tuple[str, Optional[int]]], connection: sqlite3.Connection) -> None:
        drafts = connection.execute("SELECT id FROM periods WHERE project_id=? AND status='draft'", (project_id,)).fetchall()
        for draft in drafts:
            next_order = connection.execute(
                "SELECT COALESCE(MAX(sort_order),-1)+1 FROM period_draft_members WHERE period_id=?", (draft["id"],)
            ).fetchone()[0]
            for offset, (student_id, group_number) in enumerate(students):
                connection.execute(
                    "INSERT OR IGNORE INTO period_draft_members(period_id,student_id,group_number,sort_order) VALUES(?,?,?,?)",
                    (draft["id"], student_id, group_number, next_order + offset),
                )
            if students:
                connection.execute(
                    "UPDATE period_grouping_revisions SET revision=revision+1,updated_at=CURRENT_TIMESTAMP WHERE period_id=?",
                    (draft["id"],),
                )

    def create_period(self, project_id: str, name: str, start_date: str, expected_end_date: str) -> str:
        period_id = str(uuid.uuid4())
        with self._lock, self.connection:
            self.connection.execute("INSERT INTO periods(id,project_id,name,start_date,expected_end_date) VALUES(?,?,?,?,?)", (period_id, project_id, name, start_date, expected_end_date))
            self.connection.execute("INSERT INTO period_grouping_revisions(period_id) VALUES(?)", (period_id,))
            self.connection.execute(
                "INSERT INTO period_draft_members(period_id,student_id,group_number,sort_order) "
                "SELECT ?,id,CASE WHEN is_unassigned=1 THEN NULL ELSE group_number END,sort_order FROM students "
                "WHERE project_id=? AND is_active=1 ORDER BY sort_order", (period_id, project_id)
            )
            self.connection.execute(
                "INSERT INTO period_draft_groups(period_id,group_number,leader_student_id) "
                "SELECT ?,cg.group_number,CASE WHEN s.is_unassigned=0 THEN cg.leader_student_id ELSE NULL END "
                "FROM class_groups cg LEFT JOIN students s ON s.id=cg.leader_student_id WHERE cg.project_id=?",
                (period_id, project_id),
            )
        return period_id

    def grouping_draft(self, period_id: str) -> dict[str, object]:
        with self._lock:
            period = self.connection.execute(
                "SELECT p.id,p.status,p.project_id,pr.group_count FROM periods p JOIN projects pr ON pr.id=p.project_id WHERE p.id=?",
                (period_id,),
            ).fetchone()
            if not period:
                raise ValueError("周期不存在")
            if period["status"] == "draft":
                revision = self.connection.execute("SELECT revision FROM period_grouping_revisions WHERE period_id=?", (period_id,)).fetchone()
                members = [dict(row) for row in self.connection.execute(
                    "SELECT pdm.student_id,s.student_number,s.name,pdm.group_number,pdm.sort_order "
                    "FROM period_draft_members pdm JOIN students s ON s.id=pdm.student_id WHERE pdm.period_id=? ORDER BY pdm.sort_order",
                    (period_id,),
                )]
                leaders = {str(row["group_number"]): row["leader_student_id"] for row in self.connection.execute(
                    "SELECT group_number,leader_student_id FROM period_draft_groups WHERE period_id=? ORDER BY group_number", (period_id,)
                )}
                editable = True
            else:
                revision = None
                members = [dict(row) for row in self.connection.execute(
                    "SELECT ps.student_id,ps.student_number,ps.name,ps.group_number,ps.row_index sort_order "
                    "FROM period_students ps WHERE ps.period_id=? ORDER BY ps.row_index", (period_id,)
                )]
                leaders = {str(row["group_number"]): row["leader_student_id"] for row in self.connection.execute(
                    "SELECT group_number,leader_student_id FROM period_groups WHERE period_id=? ORDER BY group_number", (period_id,)
                )}
                editable = False
            return {"period_id": period_id, "editable": editable, "revision": revision["revision"] if revision else None,
                    "group_count": period["group_count"], "members": members, "leaders": leaders}

    def save_grouping_draft(self, period_id: str, revision: int, members: list[dict[str, object]], leaders: dict[str, Optional[str]]) -> int:
        with self._lock, self.connection:
            period = self.connection.execute(
                "SELECT p.project_id,pr.group_count FROM periods p JOIN projects pr ON pr.id=p.project_id WHERE p.id=? AND p.status='draft'",
                (period_id,),
            ).fetchone()
            if not period:
                raise ValueError("只有草稿周期可以调整分组")
            current = self.connection.execute("SELECT revision FROM period_grouping_revisions WHERE period_id=?", (period_id,)).fetchone()
            if not current or current["revision"] != revision:
                raise ValueError("分组草稿已在别处更新，请刷新后重试")
            expected = {row["id"] for row in self.connection.execute(
                "SELECT id FROM students WHERE project_id=? AND is_active=1", (period["project_id"],)
            )}
            received = [str(row.get("student_id", "")) for row in members]
            if len(received) != len(set(received)) or set(received) != expected:
                raise ValueError("分组草稿必须且只能包含当前全部学生")
            normalized = []
            group_by_student: dict[str, Optional[int]] = {}
            for order, row in enumerate(members):
                group = row.get("group_number")
                group = None if group is None else int(group)
                if group is not None and not 1 <= group <= period["group_count"]:
                    raise ValueError("草稿中存在无效组号")
                student_id = str(row["student_id"])
                group_by_student[student_id] = group
                normalized.append((period_id, student_id, group, order))
            normalized_leaders: list[tuple[str, int, Optional[str]]] = []
            for group in range(1, period["group_count"] + 1):
                leader = leaders.get(str(group))
                if leader is not None and group_by_student.get(str(leader)) != group:
                    raise ValueError(f"第{group}组组长必须来自本组成员")
                normalized_leaders.append((period_id, group, str(leader) if leader else None))
            self.connection.execute("DELETE FROM period_draft_groups WHERE period_id=?", (period_id,))
            self.connection.execute("DELETE FROM period_draft_members WHERE period_id=?", (period_id,))
            self.connection.executemany(
                "INSERT INTO period_draft_members(period_id,student_id,group_number,sort_order) VALUES(?,?,?,?)", normalized
            )
            self.connection.executemany(
                "INSERT INTO period_draft_groups(period_id,group_number,leader_student_id) VALUES(?,?,?)", normalized_leaders
            )
            new_revision = revision + 1
            self.connection.execute(
                "UPDATE period_grouping_revisions SET revision=?,updated_at=CURRENT_TIMESTAMP WHERE period_id=?", (new_revision, period_id)
            )
            self.connection.execute(
                "INSERT INTO audit_events(action,entity_id,details_json) VALUES('save_grouping_draft',?,?)",
                (period_id, json.dumps({"revision": new_revision}, ensure_ascii=False)),
            )
            return new_revision

    def update_draft_period(self, period_id: str, name: str, start_date: str, expected_end_date: str) -> None:
        with self._lock, self.connection:
            changed = self.connection.execute(
                "UPDATE periods SET name=?,start_date=?,expected_end_date=? WHERE id=? AND status='draft'",
                (name.strip(), start_date, expected_end_date, period_id),
            ).rowcount
            if changed != 1:
                raise ValueError("只有草稿周期可以修改")

    def list_periods(self, project_id: str) -> list[dict[str, object]]:
        with self._lock:
            return [dict(row) for row in self.connection.execute(
                "SELECT id,name,start_date,expected_end_date,status,base_score,started_at,closed_at,result_version "
                "FROM periods WHERE project_id=? ORDER BY start_date DESC",
                (project_id,),
            ).fetchall()]

    def paper_generation_data(self, period_id: str, sheet_id: str) -> dict[str, object]:
        with self._lock:
            period = self.connection.execute(
                "SELECT p.*,pr.class_name FROM periods p JOIN projects pr ON pr.id=p.project_id WHERE p.id=?", (period_id,)
            ).fetchone()
            sheet = self.connection.execute("SELECT * FROM paper_sheets WHERE id=? AND period_id=?", (sheet_id, period_id)).fetchone()
            if not period or not sheet:
                raise ValueError("周期或纸表不存在")
            leaders = {row["group_number"]: row["leader_student_id"] for row in self.connection.execute("SELECT * FROM period_groups WHERE period_id=?", (period_id,))}
            students = [dict(row) for row in self.connection.execute(
                "SELECT student_id,student_number,name,group_number,row_index FROM period_students WHERE period_id=? ORDER BY row_index", (period_id,)
            )]
            for student in students:
                student["is_leader"] = leaders.get(student["group_number"]) == student["student_id"]
            rules = [dict(row) for row in self.connection.execute("SELECT * FROM period_rules WHERE period_id=? ORDER BY side,sort_order", (period_id,))]
            return {"period": dict(period), "sheet": dict(sheet), "students": students, "rules": rules}

    def rollback_failed_start(self, period_id: str) -> None:
        """Restore a just-started period when initial paper generation failed."""
        with self._lock, self.connection:
            sheets = self.connection.execute("SELECT id FROM paper_sheets WHERE period_id=?", (period_id,)).fetchall()
            for sheet in sheets:
                self.connection.execute("DELETE FROM paper_sides WHERE sheet_id=?", (sheet["id"],))
            self.connection.execute("DELETE FROM paper_sheets WHERE period_id=?", (period_id,))
            self.connection.execute("DELETE FROM period_groups WHERE period_id=?", (period_id,))
            self.connection.execute("DELETE FROM period_rules WHERE period_id=?", (period_id,))
            self.connection.execute("DELETE FROM period_students WHERE period_id=?", (period_id,))
            self.connection.execute("UPDATE periods SET status='draft',started_at=NULL WHERE id=?", (period_id,))

    def list_sheets(self, period_id: str) -> list[dict[str, object]]:
        with self._lock:
            return [dict(row) for row in self.connection.execute(
                "SELECT ps.id,ps.sheet_number,ps.status,ps.issued_at,"
                "MAX(CASE WHEN side='front' THEN pside.status END) front_status,"
                "MAX(CASE WHEN side='back' THEN pside.status END) back_status "
                "FROM paper_sheets ps JOIN paper_sides pside ON pside.sheet_id=ps.id "
                "WHERE ps.period_id=? GROUP BY ps.id ORDER BY ps.sheet_number", (period_id,)
            )]

    def paper_download_info(self, sheet_id: str) -> dict[str, object]:
        with self._lock:
            row = self.connection.execute(
                "SELECT ps.period_id,ps.sheet_number,p.project_id FROM paper_sheets ps "
                "JOIN periods p ON p.id=ps.period_id WHERE ps.id=?", (sheet_id,)
            ).fetchone()
            if not row:
                raise ValueError("纸表不存在")
            return dict(row)

    def resolve_sheet_reference(self, project_id: str, reference: str) -> str:
        with self._lock:
            rows = self.connection.execute(
                "SELECT ps.id FROM paper_sheets ps JOIN periods p ON p.id=ps.period_id WHERE p.project_id=? AND (ps.id=? OR ps.id LIKE ?)",
                (project_id, reference, f"%{reference}"),
            ).fetchall()
            if len(rows) != 1: raise ValueError("短编号不存在或不唯一")
            return rows[0]["id"]

    def remove_failed_sheet(self, sheet_id: str) -> None:
        with self._lock, self.connection:
            self.connection.execute("DELETE FROM paper_sides WHERE sheet_id=?", (sheet_id,))
            self.connection.execute("DELETE FROM paper_sheets WHERE id=? AND status='issued'", (sheet_id,))

    def void_unused_sheet(self, sheet_id: str) -> None:
        with self._lock, self.connection:
            row = self.connection.execute(
                "SELECT ps.status,MAX(CASE WHEN pside.status!='missing' THEN 1 ELSE 0 END) touched "
                "FROM paper_sheets ps JOIN paper_sides pside ON pside.sheet_id=ps.id WHERE ps.id=? GROUP BY ps.id", (sheet_id,)
            ).fetchone()
            if not row or row["status"] != "issued" or row["touched"]:
                raise ValueError("只有两面均未处理的已签发纸表可以标记为未使用")
            self.connection.execute("UPDATE paper_sheets SET status='void_unused' WHERE id=?", (sheet_id,))

    def record_template_version(self, sheet_id: str, manifest: dict[str, object], font_sha256: str) -> None:
        version_id = str(manifest["layout_hash"])
        geometry = json.dumps({"page": manifest["page"], "geometry": manifest["geometry"], "row_count": manifest["row_count"]}, ensure_ascii=False, sort_keys=True)
        with self._lock, self.connection:
            self.connection.execute("INSERT OR IGNORE INTO template_versions(id,template_name,geometry_json,font_sha256) VALUES(?,?,?,?)", (version_id, manifest["template_id"], geometry, font_sha256))
            self.connection.execute("UPDATE paper_sheets SET template_id=? WHERE id=?", (version_id, sheet_id))

    def create_scan_job(self, project_id: str, filename: Optional[str] = None) -> str:
        job_id = str(uuid.uuid4())
        with self._lock, self.connection:
            self.connection.execute("INSERT INTO scan_jobs(id,project_id,status,stage,original_filename) VALUES(?,?,'queued','等待处理',?)",
                                    (job_id, project_id, filename))
        return job_id

    def recover_interrupted_jobs(self) -> None:
        with self._lock, self.connection:
            self.connection.execute(
                "UPDATE scan_jobs SET status='failed',stage='上次运行中断',error='任务未完成，可重新导入原文件' "
                "WHERE status IN ('queued','processing')"
            )

    def update_scan_job(self, job_id: str, *, status: str, stage: str, total_pages: Optional[int] = None,
                        completed_pages: Optional[int] = None, error: Optional[str] = None) -> None:
        with self._lock, self.connection:
            self.connection.execute(
                "UPDATE scan_jobs SET status=?,stage=?,total_pages=COALESCE(?,total_pages),completed_pages=COALESCE(?,completed_pages),error=? WHERE id=?",
                (status, stage, total_pages, completed_pages, error, job_id),
            )

    def get_scan_job(self, job_id: str) -> dict[str, object]:
        with self._lock:
            row = self.connection.execute("SELECT * FROM scan_jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise ValueError("扫描任务不存在")
            return dict(row)

    def cancel_scan_job(self, job_id: str) -> None:
        with self._lock, self.connection:
            changed = self.connection.execute("UPDATE scan_jobs SET cancel_requested=1 WHERE id=? AND status IN ('queued','processing')", (job_id,)).rowcount
            if not changed:
                raise ValueError("任务已结束，无法取消")

    def remove_scan_job_record(self, job_id: str) -> None:
        with self._lock, self.connection:
            row = self.connection.execute("SELECT status,scan_asset_id,is_duplicate FROM scan_jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise ValueError("导入记录不存在")
            if row["status"] in {"queued", "processing"}:
                raise ValueError("请先取消正在运行的任务")
            if row["scan_asset_id"] and not row["is_duplicate"]:
                raise ValueError("该记录仍有导入文件，请使用删除本次导入")
            self.connection.execute("DELETE FROM scan_jobs WHERE id=?", (job_id,))

    def scan_asset_delete_requested(self, asset_id: str) -> bool:
        with self._lock:
            row = self.connection.execute("SELECT delete_requested FROM scan_assets WHERE id=?", (asset_id,)).fetchone()
            return not row or bool(row["delete_requested"])

    def create_scan_asset(self, project_id: str, filename: str, stored_path: str, sha256: str, size_bytes: int, job_id: Optional[str] = None) -> tuple[str, bool]:
        with self._lock, self.connection:
            existing = self.connection.execute("SELECT id,delete_requested FROM scan_assets WHERE project_id=? AND sha256=?", (project_id, sha256)).fetchone()
            if existing:
                if existing["delete_requested"]:
                    raise ValueError("相同文件正在删除，请稍后重试")
                if job_id:
                    self.connection.execute("UPDATE scan_jobs SET scan_asset_id=?,is_duplicate=1,original_filename=?,sha256=? WHERE id=?",
                                            (existing["id"], filename, sha256, job_id))
                return existing["id"], True
            asset_id = str(uuid.uuid4())
            self.connection.execute("INSERT INTO scan_assets(id,project_id,original_filename,job_id,stored_path,sha256,size_bytes) VALUES(?,?,?,?,?,?,?)", (asset_id, project_id, filename, job_id, stored_path, sha256, size_bytes))
            if job_id:
                self.connection.execute("UPDATE scan_jobs SET scan_asset_id=?,original_filename=?,sha256=? WHERE id=?",
                                        (asset_id, filename, sha256, job_id))
            return asset_id, False

    def create_recognition_run(self, asset_id: str, page_index: int) -> str:
        run_id = str(uuid.uuid4())
        with self._lock, self.connection:
            self.connection.execute("INSERT INTO recognition_runs(id,scan_asset_id,page_index,algorithm_version,status) VALUES(?,?,?,?,?)", (run_id, asset_id, page_index, "prototype-2", "queued"))
        return run_id

    def remove_unprocessed_asset(self, asset_id: str) -> None:
        with self._lock, self.connection:
            if not self.connection.execute("SELECT 1 FROM recognition_runs WHERE scan_asset_id=?", (asset_id,)).fetchone():
                self.connection.execute("DELETE FROM scan_assets WHERE id=?", (asset_id,))

    def update_recognition_run(self, run_id: str, *, status: str, sheet_id: Optional[str] = None, side: Optional[str] = None,
                               corrected_path: Optional[str] = None, quality: Optional[dict] = None, error: Optional[str] = None) -> None:
        with self._lock, self.connection:
            self.connection.execute(
                "UPDATE recognition_runs SET status=?,sheet_id=COALESCE(?,sheet_id),side=COALESCE(?,side),"
                "corrected_path=COALESCE(?,corrected_path),quality_json=?,error=? WHERE id=?",
                (status, sheet_id, side, corrected_path, json.dumps(quality, ensure_ascii=False) if quality is not None else None, error, run_id),
            )

    def save_observations(self, run_id: str, observations: list[dict[str, object]]) -> None:
        with self._lock, self.connection:
            self.connection.execute("DELETE FROM slot_observations WHERE run_id=?", (run_id,))
            self.connection.executemany(
                "INSERT INTO slot_observations(run_id,slot_id,auto_class,confidence,features_json) VALUES(?,?,?,?,?)",
                [(run_id, o["slot_id"], o["classification"], o["confidence"], json.dumps(o["features"], sort_keys=True)) for o in observations],
            )

    def list_recognition_runs(self, project_id: str) -> list[dict[str, object]]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT rr.*,sa.original_filename,sa.sha256 FROM recognition_runs rr JOIN scan_assets sa ON sa.id=rr.scan_asset_id "
                "WHERE sa.project_id=? AND rr.removed_at IS NULL AND sa.delete_requested=0 "
                "ORDER BY rr.created_at DESC,rr.page_index", (project_id,)
            ).fetchall()
            return [dict(row) for row in rows]

    def list_scan_imports(self, project_id: str, include_removed: bool = False) -> list[dict[str, object]]:
        with self._lock:
            assets = [dict(row) for row in self.connection.execute(
                "SELECT COALESCE(sa.id,sj.id) id,sa.id asset_id,sj.project_id,sa.job_id,sa.stored_path,sa.size_bytes,COALESCE(sa.delete_requested,0) delete_requested,"
                "COALESCE(sj.original_filename,sa.original_filename) original_filename,"
                "COALESCE(sj.sha256,sa.sha256) sha256,sj.id import_job_id,sj.status job_status,sj.stage,sj.total_pages,sj.completed_pages,"
                "sj.error job_error,sj.is_duplicate,sj.created_at import_created_at "
                "FROM scan_jobs sj LEFT JOIN scan_assets sa ON sa.id=sj.scan_asset_id WHERE sj.project_id=? "
                "ORDER BY sj.created_at DESC,sj.id", (project_id,)
            )]
            result = []
            for asset in assets:
                clauses = "" if include_removed else " AND rr.removed_at IS NULL"
                pages = [] if asset["is_duplicate"] or not asset["asset_id"] else [dict(row) for row in self.connection.execute(
                    "SELECT rr.*,ps.sheet_number,p.name period_name,p.status period_status,"
                    "EXISTS(SELECT 1 FROM posting_batches pb WHERE (pb.front_run_id=rr.id OR pb.back_run_id=rr.id) "
                    "AND pb.status='posted' AND pb.reverses_batch_id IS NULL) is_posted,"
                    "(SELECT COUNT(*) FROM slot_observations so WHERE so.run_id=rr.id "
                    "AND COALESCE(so.manual_class,so.auto_class) LIKE 'slash_%') effective_marks "
                    "FROM recognition_runs rr LEFT JOIN paper_sheets ps ON ps.id=rr.sheet_id "
                    "LEFT JOIN periods p ON p.id=ps.period_id WHERE rr.scan_asset_id=?" + clauses +
                    " ORDER BY rr.page_index,rr.created_at", (asset["asset_id"],)
                )]
                for page in pages:
                    quality = json.loads(page["quality_json"]) if page.get("quality_json") else {}
                    page["quality"] = quality
                    page["predicted_delta"] = quality.get("predicted_delta", 0)
                    page.pop("quality_json", None)
                if pages or asset["is_duplicate"] or asset["job_status"] in {"queued", "processing", "failed"} or (asset["job_status"] == "cancelled" and asset["asset_id"]):
                    asset["created_at"] = asset["import_created_at"]
                    asset["pages"] = pages
                    result.append(asset)
            return result

    def scan_removal_preview(self, run_ids: list[str]) -> dict[str, object]:
        unique = list(dict.fromkeys(run_ids))
        if not unique:
            raise ValueError("请选择要移除的页面")
        placeholders = ",".join("?" for _ in unique)
        with self._lock:
            rows = self.connection.execute(
                f"SELECT rr.id,rr.sheet_id,rr.side,rr.adopted,p.status period_status,ps.sheet_number,"
                f"EXISTS(SELECT 1 FROM posting_batches pb WHERE (pb.front_run_id=rr.id OR pb.back_run_id=rr.id) "
                f"AND pb.status='posted' AND pb.reverses_batch_id IS NULL) is_posted "
                f"FROM recognition_runs rr LEFT JOIN paper_sheets ps ON ps.id=rr.sheet_id "
                f"LEFT JOIN periods p ON p.id=ps.period_id WHERE rr.id IN ({placeholders}) AND rr.removed_at IS NULL",
                unique,
            ).fetchall()
            if len(rows) != len(unique):
                raise ValueError("部分识别页不存在或已移除")
            if any(row["period_status"] == "closed" for row in rows):
                raise ValueError("已关闭周期为只读；请先通过维护入口重新打开")
            posted_sheets = {row["sheet_id"] for row in rows if row["is_posted"]}
            affected_people = 0
            score_delta = 0
            for sheet_id in posted_sheets:
                batch = self.connection.execute(
                    "SELECT id FROM posting_batches WHERE sheet_id=? AND status='posted' AND reverses_batch_id IS NULL", (sheet_id,)
                ).fetchone()
                if batch:
                    summary = self.connection.execute(
                        "SELECT COUNT(DISTINCT student_id),COALESCE(SUM(amount),0) FROM ledger_entries WHERE batch_id=?", (batch["id"],)
                    ).fetchone()
                    affected_people += int(summary[0]); score_delta += int(summary[1])
            return {"page_count":len(rows), "requires_reversal":len(posted_sheets),
                    "affected_people":affected_people, "score_delta":score_delta}

    def cancel_run_adoption(self, run_id: str) -> None:
        with self._lock, self.connection:
            row = self.connection.execute(
                "SELECT rr.*,p.status period_status FROM recognition_runs rr LEFT JOIN paper_sheets ps ON ps.id=rr.sheet_id "
                "LEFT JOIN periods p ON p.id=ps.period_id WHERE rr.id=? AND rr.removed_at IS NULL", (run_id,)
            ).fetchone()
            if not row or not row["adopted"]:
                raise ValueError("该页面没有已采用的确认")
            if row["period_status"] == "closed":
                raise ValueError("已关闭周期为只读；请先通过维护入口重新打开")
            posted = self.connection.execute(
                "SELECT 1 FROM posting_batches WHERE (front_run_id=? OR back_run_id=?) AND status='posted' AND reverses_batch_id IS NULL",
                (run_id, run_id),
            ).fetchone()
            if posted:
                raise ValueError("该页面已入账，必须先撤销整表入账")
            self.connection.execute("UPDATE recognition_runs SET adopted=0,status='ready' WHERE id=?", (run_id,))
            self.connection.execute("UPDATE paper_sides SET status='candidate' WHERE sheet_id=? AND side=?", (row["sheet_id"], row["side"]))
            self.connection.execute("INSERT INTO audit_events(action,entity_id,details_json) VALUES('cancel_run_adoption',?,'{}')", (run_id,))

    def prepare_recognition_retry(self, run_id: str) -> dict[str, object]:
        with self._lock, self.connection:
            row = self.connection.execute(
                "SELECT rr.*,sa.job_id,sa.stored_path,sa.delete_requested,p.status period_status "
                "FROM recognition_runs rr JOIN scan_assets sa ON sa.id=rr.scan_asset_id "
                "LEFT JOIN paper_sheets ps ON ps.id=rr.sheet_id LEFT JOIN periods p ON p.id=ps.period_id "
                "WHERE rr.id=? AND rr.removed_at IS NULL", (run_id,)
            ).fetchone()
            if not row:
                raise ValueError("识别页不存在或已移除")
            if row["delete_requested"]:
                raise ValueError("该导入正在删除")
            if row["adopted"]:
                raise ValueError("请先取消该面确认再重新识别")
            if row["period_status"] == "closed":
                raise ValueError("已关闭周期为只读；请先通过维护入口重新打开")
            if row["status"] in {"queued", "processing"}:
                raise ValueError("该页面正在识别")
            self.connection.execute("DELETE FROM slot_observations WHERE run_id=?", (run_id,))
            self.connection.execute(
                "UPDATE recognition_runs SET status='queued',adopted=0,quality_json=NULL,error=NULL,corrected_path=NULL WHERE id=?", (run_id,)
            )
            self.connection.execute(
                "UPDATE scan_jobs SET status='queued',stage='等待重新识别',cancel_requested=0,error=NULL WHERE id=?", (row["job_id"],)
            )
            self.connection.execute("INSERT INTO audit_events(action,entity_id,details_json) VALUES('retry_recognition_run',?,'{}')", (run_id,))
            return {"run_id":run_id, "job_id":row["job_id"], "asset_id":row["scan_asset_id"],
                    "stored_path":row["stored_path"], "page_index":row["page_index"], "old_corrected_path":row["corrected_path"]}

    def remove_recognition_runs(self, run_ids: list[str], reason: str, *, reverse_posted: bool = False,
                                idempotency_prefix: Optional[str] = None) -> dict[str, object]:
        if not reason.strip():
            raise ValueError("移除记录必须填写原因")
        preview = self.scan_removal_preview(run_ids)
        if preview["requires_reversal"] and not reverse_posted:
            raise ValueError(
                f"选中页面涉及 {preview['requires_reversal']} 张已入账纸表，"
                f"必须撤销整表入账（影响 {preview['affected_people']} 人，分值 {preview['score_delta']:+d}）"
            )
        unique = list(dict.fromkeys(run_ids))
        paths: set[str] = set()
        reversed_sheets: list[str] = []
        hard_deleted = soft_removed = 0
        with self._lock, self.connection:
            rows = [self.connection.execute("SELECT * FROM recognition_runs WHERE id=?", (run_id,)).fetchone() for run_id in unique]
            posted_sheets = {
                row["sheet_id"] for row in rows if row and self.connection.execute(
                    "SELECT 1 FROM posting_batches WHERE (front_run_id=? OR back_run_id=?) "
                    "AND status='posted' AND reverses_batch_id IS NULL", (row["id"], row["id"])
                ).fetchone()
            }
            for sheet_id in sorted(posted_sheets):
                key = f"{idempotency_prefix or uuid.uuid4()}:{sheet_id}"
                self.reverse_posting(sheet_id, reason, key, _in_transaction=True)
                reversed_sheets.append(sheet_id)
            for row in rows:
                if not row:
                    raise ValueError("识别页不存在")
                run_id, sheet_id, side = row["id"], row["sheet_id"], row["side"]
                referenced = self.connection.execute(
                    "SELECT 1 FROM posting_batches WHERE front_run_id=? OR back_run_id=?", (run_id, run_id)
                ).fetchone()
                was_adopted = bool(row["adopted"])
                if referenced:
                    self.connection.execute(
                        "UPDATE recognition_runs SET adopted=0,removed_at=CURRENT_TIMESTAMP,removal_reason=? WHERE id=?",
                        (reason.strip(), run_id),
                    )
                    soft_removed += 1
                else:
                    if row["corrected_path"]:
                        paths.add(row["corrected_path"])
                    self.connection.execute("DELETE FROM scan_notes WHERE run_id=?", (run_id,))
                    self.connection.execute("DELETE FROM slot_observations WHERE run_id=?", (run_id,))
                    self.connection.execute("DELETE FROM recognition_runs WHERE id=?", (run_id,))
                    hard_deleted += 1
                if sheet_id and side:
                    remaining = self.connection.execute(
                        "SELECT adopted FROM recognition_runs WHERE sheet_id=? AND side=? AND removed_at IS NULL", (sheet_id, side)
                    ).fetchall()
                    if was_adopted or not remaining:
                        self.connection.execute("UPDATE paper_sides SET status='missing' WHERE sheet_id=? AND side=?", (sheet_id, side))
                    elif not any(item["adopted"] for item in remaining):
                        self.connection.execute("UPDATE paper_sides SET status='candidate' WHERE sheet_id=? AND side=?", (sheet_id, side))
                self.connection.execute(
                    "INSERT INTO audit_events(action,entity_id,details_json) VALUES('remove_recognition_run',?,?)",
                    (run_id, json.dumps({"reason":reason.strip(), "history_retained":bool(referenced)}, ensure_ascii=False)),
                )
            asset_ids = {row["scan_asset_id"] for row in rows if row}
            for asset_id in asset_ids:
                if not self.connection.execute("SELECT 1 FROM recognition_runs WHERE scan_asset_id=?", (asset_id,)).fetchone():
                    asset = self.connection.execute("SELECT stored_path FROM scan_assets WHERE id=?", (asset_id,)).fetchone()
                    if asset:
                        paths.add(asset["stored_path"])
                    self.connection.execute("DELETE FROM scan_assets WHERE id=?", (asset_id,))
        return {**preview, "hard_deleted":hard_deleted, "history_retained":soft_removed,
                "reversed_sheets":reversed_sheets, "paths_to_delete":sorted(paths)}

    def request_scan_asset_deletion(self, asset_id: str, reason: str) -> dict[str, object]:
        if not reason.strip():
            raise ValueError("删除导入必须填写原因")
        with self._lock, self.connection:
            asset = self.connection.execute(
                "SELECT sa.*,sj.status job_status FROM scan_assets sa LEFT JOIN scan_jobs sj ON sj.id=sa.job_id WHERE sa.id=?", (asset_id,)
            ).fetchone()
            if not asset:
                raise ValueError("导入文件不存在")
            posted = self.connection.execute(
                "SELECT 1 FROM recognition_runs rr JOIN posting_batches pb ON pb.front_run_id=rr.id OR pb.back_run_id=rr.id "
                "WHERE rr.scan_asset_id=? AND pb.status='posted' AND pb.reverses_batch_id IS NULL", (asset_id,)
            ).fetchone()
            if posted:
                raise ValueError("该导入含已入账页面，请选择页面并使用“撤销入账并移除”")
            if asset["job_status"] in {"queued", "processing"}:
                self.connection.execute("UPDATE scan_assets SET delete_requested=1 WHERE id=?", (asset_id,))
                self.connection.execute("UPDATE scan_jobs SET cancel_requested=1,stage='正在取消并删除' WHERE id=?", (asset["job_id"],))
                self.connection.execute("INSERT INTO audit_events(action,entity_id,details_json) VALUES('request_delete_scan_asset',?,?)",
                                        (asset_id, json.dumps({"reason":reason.strip()}, ensure_ascii=False)))
                return {"status":"pending", "paths_to_delete":[]}
            run_ids = [row[0] for row in self.connection.execute(
                "SELECT id FROM recognition_runs WHERE scan_asset_id=? AND removed_at IS NULL", (asset_id,)
            )]
        if run_ids:
            return {"status":"deleted", **self.remove_recognition_runs(run_ids, reason)}
        with self._lock, self.connection:
            asset = self.connection.execute("SELECT stored_path FROM scan_assets WHERE id=?", (asset_id,)).fetchone()
            if asset:
                self.connection.execute("DELETE FROM scan_assets WHERE id=?", (asset_id,))
        return {"status":"deleted", "paths_to_delete":[asset["stored_path"]] if asset else []}

    def finalize_requested_asset_deletion(self, asset_id: str) -> dict[str, object]:
        with self._lock:
            asset = self.connection.execute("SELECT stored_path,delete_requested FROM scan_assets WHERE id=?", (asset_id,)).fetchone()
            if not asset or not asset["delete_requested"]:
                return {"paths_to_delete":[]}
            run_ids = [row[0] for row in self.connection.execute(
                "SELECT id FROM recognition_runs WHERE scan_asset_id=? AND removed_at IS NULL", (asset_id,)
            )]
        if run_ids:
            result = self.remove_recognition_runs(run_ids, "识别中取消并删除")
            return result
        with self._lock, self.connection:
            self.connection.execute("DELETE FROM scan_assets WHERE id=?", (asset_id,))
        return {"paths_to_delete":[asset["stored_path"]]}

    def get_run(self, run_id: str, include_observations: bool = False) -> dict[str, object]:
        with self._lock:
            row = self.connection.execute("SELECT rr.*,sa.project_id,sa.original_filename,sa.stored_path FROM recognition_runs rr JOIN scan_assets sa ON sa.id=rr.scan_asset_id WHERE rr.id=?", (run_id,)).fetchone()
            if not row:
                raise ValueError("识别任务不存在")
            result = dict(row)
            if include_observations:
                observations = [dict(item) for item in self.connection.execute(
                    "SELECT slot_id,auto_class,confidence,manual_class,features_json FROM slot_observations WHERE run_id=? "
                    "ORDER BY CASE WHEN COALESCE(manual_class,auto_class)='review' THEN 0 ELSE 1 END,confidence", (run_id,)
                )]
                result["observations"] = observations
            return result

    def set_manual_observations(self, run_id: str, decisions: dict[str, str]) -> None:
        allowed = {"blank","slash_forward","slash_back","x"}
        if not decisions or any(value not in allowed for value in decisions.values()):
            raise ValueError("人工判定无效")
        with self._lock, self.connection:
            period = self.connection.execute(
                "SELECT p.status FROM recognition_runs rr LEFT JOIN paper_sheets ps ON ps.id=rr.sheet_id "
                "LEFT JOIN periods p ON p.id=ps.period_id WHERE rr.id=?", (run_id,)
            ).fetchone()
            if period and period["status"] == "closed":
                raise ValueError("已关闭周期为只读；请先通过维护入口重新打开")
            for slot_id, value in decisions.items():
                changed = self.connection.execute("UPDATE slot_observations SET manual_class=? WHERE run_id=? AND slot_id=?", (value, run_id, slot_id)).rowcount
                if changed != 1:
                    raise ValueError(f"槽位不存在：{slot_id}")
            self.connection.execute("INSERT INTO audit_events(action,entity_id,details_json) VALUES('manual_review',?,?)", (run_id, json.dumps(decisions, ensure_ascii=False)))

    def adopt_run(self, run_id: str) -> None:
        with self._lock, self.connection:
            run = self.connection.execute("SELECT * FROM recognition_runs WHERE id=?", (run_id,)).fetchone()
            if not run or run["status"] != "ready" or not run["sheet_id"] or not run["side"]:
                raise ValueError("识别版本尚不能采用")
            unresolved = self.connection.execute("SELECT count(*) FROM slot_observations WHERE run_id=? AND COALESCE(manual_class,auto_class)='review'", (run_id,)).fetchone()[0]
            if unresolved:
                raise ValueError(f"仍有 {unresolved} 个槽位待复核")
            side = self.connection.execute("SELECT status FROM paper_sides WHERE sheet_id=? AND side=?", (run["sheet_id"], run["side"])).fetchone()
            if not side or side["status"] == "posted":
                raise ValueError("纸表面不存在或已经入账")
            if side["status"] == "confirmed_blank":
                nonblank = self.connection.execute("SELECT count(*) FROM slot_observations WHERE run_id=? AND COALESCE(manual_class,auto_class)!='blank'", (run_id,)).fetchone()[0]
                if nonblank:
                    raise ValueError("该面此前已确认空白；请先撤回空白确认")
            self.connection.execute("UPDATE recognition_runs SET adopted=0 WHERE sheet_id=? AND side=?", (run["sheet_id"], run["side"]))
            self.connection.execute("UPDATE recognition_runs SET adopted=1,status='reviewed' WHERE id=?", (run_id,))
            self.connection.execute("UPDATE paper_sides SET status='reviewed' WHERE sheet_id=? AND side=?", (run["sheet_id"], run["side"]))
            self.connection.execute("INSERT INTO audit_events(action,entity_id,details_json) VALUES('adopt_run',?,'{}')", (run_id,))

    def confirm_blank_side(self, sheet_id: str, side: str) -> None:
        if side not in {"front","back"}:
            raise ValueError("面别无效")
        with self._lock, self.connection:
            current = self.connection.execute("SELECT status FROM paper_sides WHERE sheet_id=? AND side=?", (sheet_id, side)).fetchone()
            if not current or current["status"] == "posted":
                raise ValueError("纸表面不存在或已经入账")
            self.connection.execute("UPDATE paper_sides SET status='confirmed_blank' WHERE sheet_id=? AND side=?", (sheet_id, side))
            self.connection.execute("INSERT INTO audit_events(action,entity_id,details_json) VALUES('confirm_blank',?,?)", (sheet_id, json.dumps({"side":side})))

    def mark_side_candidate(self, sheet_id: str, side: str) -> str:
        with self._lock, self.connection:
            row = self.connection.execute("SELECT status FROM paper_sides WHERE sheet_id=? AND side=?", (sheet_id, side)).fetchone()
            if not row: raise ValueError("纸表面不存在")
            if row["status"] == "posted": raise ValueError("该纸表面已经入账")
            if row["status"] != "confirmed_blank":
                self.connection.execute("UPDATE paper_sides SET status='candidate' WHERE sheet_id=? AND side=?", (sheet_id, side))
            return row["status"]

    def withdraw_blank_side(self, sheet_id: str, side: str, reason: str) -> None:
        if not reason.strip():
            raise ValueError("必须填写撤回原因")
        with self._lock, self.connection:
            changed = self.connection.execute("UPDATE paper_sides SET status='missing' WHERE sheet_id=? AND side=? AND status='confirmed_blank'", (sheet_id, side)).rowcount
            if changed != 1:
                raise ValueError("该面没有空白确认")
            self.connection.execute("INSERT INTO audit_events(action,entity_id,details_json) VALUES('withdraw_blank',?,?)", (sheet_id, json.dumps({"side":side,"reason":reason}, ensure_ascii=False)))

    def add_scan_note(self, run_id: str, note_text: str, student_number: Optional[str] = None, rule_name: Optional[str] = None) -> str:
        if not note_text.strip(): raise ValueError("备注内容不能为空")
        note_id = str(uuid.uuid4())
        with self._lock, self.connection:
            run = self.connection.execute(
                "SELECT p.status period_status FROM recognition_runs rr LEFT JOIN paper_sheets ps ON ps.id=rr.sheet_id "
                "LEFT JOIN periods p ON p.id=ps.period_id WHERE rr.id=?", (run_id,)
            ).fetchone()
            if not run: raise ValueError("识别任务不存在")
            if run["period_status"] == "closed": raise ValueError("已关闭周期为只读；请先通过维护入口重新打开")
            self.connection.execute("INSERT INTO scan_notes(id,run_id,student_number,rule_name,note_text) VALUES(?,?,?,?,?)", (note_id, run_id, student_number, rule_name, note_text.strip()))
            self.connection.execute("INSERT INTO audit_events(action,entity_id,details_json) VALUES('add_scan_note',?,?)", (run_id, json.dumps({"note_id":note_id}, ensure_ascii=False)))
        return note_id

    def list_scan_notes(self, run_id: str) -> list[dict[str, object]]:
        with self._lock:
            return [dict(row) for row in self.connection.execute("SELECT * FROM scan_notes WHERE run_id=? ORDER BY created_at,id", (run_id,))]

    def begin_settlement(self, period_id: str) -> None:
        with self._lock, self.connection:
            changed = self.connection.execute(
                "UPDATE periods SET status='settling' WHERE id=? AND status='active'", (period_id,)
            ).rowcount
            if changed != 1:
                raise ValueError("只有进行中的周期可以进入待结算")
            self.connection.execute(
                "INSERT INTO audit_events(action,entity_id,details_json) VALUES('begin_settlement',?,'{}')",
                (period_id,),
            )

    def post_sheet(self, sheet_id: str, idempotency_key: str) -> dict[str, object]:
        if not idempotency_key.strip():
            raise ValueError("缺少幂等键")
        with self._lock, self.connection:
            existing = self.connection.execute(
                "SELECT * FROM posting_batches WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
            if existing:
                if existing["sheet_id"] != sheet_id or existing["reverses_batch_id"]:
                    raise ValueError("幂等键已用于其他操作")
                return dict(existing)
            sheet = self.connection.execute(
                "SELECT ps.*,p.status period_status FROM paper_sheets ps JOIN periods p ON p.id=ps.period_id WHERE ps.id=?",
                (sheet_id,),
            ).fetchone()
            if not sheet or sheet["period_status"] != "settling":
                raise ValueError("纸表只能在待结算周期中入账")
            active = self.connection.execute(
                "SELECT * FROM posting_batches WHERE sheet_id=? AND status='posted' AND reverses_batch_id IS NULL",
                (sheet_id,),
            ).fetchone()
            if active:
                return dict(active)
            if sheet["status"] != "issued":
                raise ValueError("纸表不是可入账状态")
            sides = self.connection.execute(
                "SELECT side,status FROM paper_sides WHERE sheet_id=? ORDER BY side", (sheet_id,)
            ).fetchall()
            if len(sides) != 2 or any(row["status"] not in {"reviewed", "confirmed_blank"} for row in sides):
                raise ValueError("正反面均复核或确认空白后才能整表入账")
            period_students = [dict(row) for row in self.connection.execute(
                "SELECT * FROM period_students WHERE period_id=? ORDER BY row_index", (sheet["period_id"],)
            )]
            period_rules = [dict(row) for row in self.connection.execute(
                "SELECT * FROM period_rules WHERE period_id=? ORDER BY side,sort_order", (sheet["period_id"],)
            )]
            observations_by_side: dict[str, list[dict[str, object]]] = {}
            run_ids: dict[str, Optional[str]] = {"front": None, "back": None}
            for side in sides:
                if side["status"] == "confirmed_blank":
                    observations_by_side[side["side"]] = []
                    continue
                run = self.connection.execute(
                    "SELECT id FROM recognition_runs WHERE sheet_id=? AND side=? AND adopted=1 AND status='reviewed'",
                    (sheet_id, side["side"]),
                ).fetchone()
                if not run:
                    raise ValueError(f"{side['side']}面缺少已采用的复核版本")
                run_ids[side["side"]] = run["id"]
                observations_by_side[side["side"]] = [dict(row) for row in self.connection.execute(
                    "SELECT slot_id,auto_class,manual_class FROM slot_observations WHERE run_id=? ORDER BY slot_id",
                    (run["id"],),
                )]
            entries = aggregate_observations(period_students, period_rules, observations_by_side)
            batch_id = str(uuid.uuid4())
            self.connection.execute(
                "INSERT INTO posting_batches(id,sheet_id,idempotency_key,status,front_run_id,back_run_id) VALUES(?,?,?,'posted',?,?)",
                (batch_id, sheet_id, idempotency_key, run_ids["front"], run_ids["back"]),
            )
            self.connection.executemany(
                "INSERT INTO ledger_entries(id,batch_id,student_id,rule_id,mark_count,unit_score,amount,slot_ids_json) "
                "VALUES(?,?,?,?,?,?,?,?)",
                [
                    (
                        str(uuid.uuid4()), batch_id, entry["student_id"], entry["rule_id"], entry["mark_count"],
                        entry["unit_score"], entry["amount"], json.dumps(entry["slot_ids"], ensure_ascii=False),
                    )
                    for entry in entries
                ],
            )
            self.connection.execute("UPDATE paper_sheets SET status='posted' WHERE id=?", (sheet_id,))
            self.connection.execute("UPDATE paper_sides SET status='posted' WHERE sheet_id=?", (sheet_id,))
            self.connection.execute(
                "INSERT INTO audit_events(action,entity_id,details_json) VALUES('post_sheet',?,?)",
                (batch_id, json.dumps({"sheet_id": sheet_id, "entry_count": len(entries)}, ensure_ascii=False)),
            )
            return {"id": batch_id, "sheet_id": sheet_id, "status": "posted", "entry_count": len(entries)}

    def reverse_posting(self, sheet_id: str, reason: str, idempotency_key: str,
                        _in_transaction: bool = False) -> dict[str, object]:
        if not reason.strip():
            raise ValueError("撤销入账必须填写原因")
        if not idempotency_key.strip():
            raise ValueError("缺少幂等键")
        transaction = nullcontext() if _in_transaction else self.connection
        with self._lock, transaction:
            existing = self.connection.execute(
                "SELECT * FROM posting_batches WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
            if existing:
                if existing["sheet_id"] != sheet_id or not existing["reverses_batch_id"]:
                    raise ValueError("幂等键已用于其他操作")
                return dict(existing)
            sheet = self.connection.execute(
                "SELECT ps.*,p.status period_status FROM paper_sheets ps JOIN periods p ON p.id=ps.period_id WHERE ps.id=?",
                (sheet_id,),
            ).fetchone()
            if not sheet or sheet["period_status"] != "settling":
                raise ValueError("只有重新打开或待结算的周期可以撤销入账")
            original = self.connection.execute(
                "SELECT * FROM posting_batches WHERE sheet_id=? AND status='posted' AND reverses_batch_id IS NULL",
                (sheet_id,),
            ).fetchone()
            if not original:
                raise ValueError("该纸表没有可撤销的入账")
            reversal_id = str(uuid.uuid4())
            self.connection.execute("UPDATE posting_batches SET status='reversed' WHERE id=?", (original["id"],))
            self.connection.execute(
                "INSERT INTO posting_batches(id,sheet_id,idempotency_key,status,reverses_batch_id,front_run_id,back_run_id,reason) "
                "VALUES(?,?,?,'posted',?,?,?,?)",
                (reversal_id, sheet_id, idempotency_key, original["id"], original["front_run_id"], original["back_run_id"], reason.strip()),
            )
            original_entries = self.connection.execute(
                "SELECT * FROM ledger_entries WHERE batch_id=? ORDER BY id", (original["id"],)
            ).fetchall()
            self.connection.executemany(
                "INSERT INTO ledger_entries(id,batch_id,source_entry_id,student_id,rule_id,mark_count,unit_score,amount,slot_ids_json) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                [
                    (
                        str(uuid.uuid4()), reversal_id, entry["id"], entry["student_id"], entry["rule_id"],
                        -entry["mark_count"], entry["unit_score"], -entry["amount"], entry["slot_ids_json"],
                    )
                    for entry in original_entries
                ],
            )
            self.connection.execute("UPDATE paper_sheets SET status='issued' WHERE id=?", (sheet_id,))
            for side in ("front", "back"):
                adopted = self.connection.execute(
                    "SELECT 1 FROM recognition_runs WHERE sheet_id=? AND side=? AND adopted=1", (sheet_id, side)
                ).fetchone()
                self.connection.execute(
                    "UPDATE paper_sides SET status=? WHERE sheet_id=? AND side=?",
                    ("reviewed" if adopted else "confirmed_blank", sheet_id, side),
                )
            self.connection.execute(
                "INSERT INTO audit_events(action,entity_id,details_json) VALUES('reverse_posting',?,?)",
                (reversal_id, json.dumps({"reverses": original["id"], "reason": reason.strip()}, ensure_ascii=False)),
            )
            return {"id": reversal_id, "sheet_id": sheet_id, "status": "posted", "reverses_batch_id": original["id"]}

    def period_results(self, period_id: str) -> dict[str, object]:
        with self._lock:
            period = self.connection.execute(
                "SELECT p.*,pr.class_name,pr.school_year FROM periods p JOIN projects pr ON pr.id=p.project_id WHERE p.id=?",
                (period_id,),
            ).fetchone()
            if not period:
                raise ValueError("周期不存在")
            students = [dict(row) for row in self.connection.execute(
                "SELECT * FROM period_students WHERE period_id=? ORDER BY row_index", (period_id,)
            )]
            rules = [dict(row) for row in self.connection.execute(
                "SELECT * FROM period_rules WHERE period_id=? ORDER BY CASE side WHEN 'front' THEN 0 ELSE 1 END,sort_order", (period_id,)
            )]
            effective = [dict(row) for row in self.connection.execute(
                "SELECT le.student_id,le.rule_id,SUM(le.mark_count) mark_count,MAX(le.unit_score) unit_score,SUM(le.amount) amount "
                "FROM ledger_entries le JOIN posting_batches pb ON pb.id=le.batch_id "
                "JOIN paper_sheets ps ON ps.id=pb.sheet_id WHERE ps.period_id=? "
                "GROUP BY le.student_id,le.rule_id HAVING SUM(le.mark_count)!=0 OR SUM(le.amount)!=0",
                (period_id,),
            )]
            result = summarize_results(dict(period), students, rules, effective)
            result["sheets"] = [dict(row) for row in self.connection.execute(
                "SELECT ps.id,ps.sheet_number,ps.status,pb.id batch_id,pb.front_run_id,pb.back_run_id,pb.created_at posted_at "
                "FROM paper_sheets ps LEFT JOIN posting_batches pb ON pb.sheet_id=ps.id AND pb.status='posted' "
                "AND pb.reverses_batch_id IS NULL WHERE ps.period_id=? ORDER BY ps.sheet_number",
                (period_id,),
            )]
            result["notes"] = [dict(row) for row in self.connection.execute(
                "SELECT sn.student_number,sn.rule_name,sn.note_text,sn.created_at,ps.sheet_number "
                "FROM scan_notes sn JOIN recognition_runs rr ON rr.id=sn.run_id "
                "JOIN paper_sheets ps ON ps.id=rr.sheet_id WHERE ps.period_id=? AND rr.adopted=1 "
                "ORDER BY ps.sheet_number,sn.created_at,sn.id",
                (period_id,),
            )]
            result["posting_history"] = [dict(row) for row in self.connection.execute(
                "SELECT pb.id,pb.sheet_id,ps.sheet_number,pb.status,pb.reverses_batch_id,pb.front_run_id,pb.back_run_id,pb.reason,pb.created_at "
                "FROM posting_batches pb JOIN paper_sheets ps ON ps.id=pb.sheet_id WHERE ps.period_id=? ORDER BY pb.created_at,pb.id",
                (period_id,),
            )]
            ledger = [dict(row) for row in self.connection.execute(
                "SELECT le.id,le.batch_id,le.source_entry_id,pb.sheet_id,ps.sheet_number,pts.student_number,pts.name,"
                "pr.name rule_name,pr.side,le.mark_count,le.unit_score,le.amount,le.slot_ids_json,"
                "CASE WHEN pr.side='front' THEN pb.front_run_id ELSE pb.back_run_id END run_id "
                "FROM ledger_entries le JOIN posting_batches pb ON pb.id=le.batch_id "
                "JOIN paper_sheets ps ON ps.id=pb.sheet_id JOIN period_students pts ON pts.period_id=ps.period_id AND pts.student_id=le.student_id "
                "JOIN period_rules pr ON pr.period_id=ps.period_id AND pr.rule_id=le.rule_id WHERE ps.period_id=? "
                "ORDER BY ps.sheet_number,pts.row_index,pr.side,pr.sort_order,le.created_at,le.id",
                (period_id,),
            )]
            for entry in ledger:
                entry["slot_ids"] = json.loads(entry.pop("slot_ids_json"))
            result["ledger"] = ledger
            return result

    def close_period(self, period_id: str) -> int:
        with self._lock, self.connection:
            period = self.connection.execute("SELECT status,result_version FROM periods WHERE id=?", (period_id,)).fetchone()
            if not period or period["status"] != "settling":
                raise ValueError("只有待结算周期可以关闭")
            incomplete = self.connection.execute(
                "SELECT count(*) FROM paper_sheets WHERE period_id=? AND status NOT IN ('posted','void_unused')",
                (period_id,),
            ).fetchone()[0]
            if incomplete:
                raise ValueError(f"仍有 {incomplete} 张纸表未入账或未标记作废")
            version = int(period["result_version"]) + 1
            self.connection.execute(
                "UPDATE periods SET status='closed',closed_at=CURRENT_TIMESTAMP,result_version=? WHERE id=?",
                (version, period_id),
            )
            self.connection.execute(
                "INSERT INTO audit_events(action,entity_id,details_json) VALUES('close_period',?,?)",
                (period_id, json.dumps({"result_version": version})),
            )
            return version

    def reopen_period(self, period_id: str, reason: str) -> None:
        if not reason.strip():
            raise ValueError("重新打开必须填写原因")
        with self._lock, self.connection:
            changed = self.connection.execute(
                "UPDATE periods SET status='settling',closed_at=NULL WHERE id=? AND status='closed'", (period_id,)
            ).rowcount
            if changed != 1:
                raise ValueError("只有已关闭周期可以重新打开")
            self.connection.execute(
                "INSERT INTO audit_events(action,entity_id,details_json) VALUES('reopen_period',?,?)",
                (period_id, json.dumps({"reason": reason.strip()}, ensure_ascii=False)),
            )

    def find_report_export(self, period_id: str, result_version: int, variant: str, is_draft: bool, source_digest: str) -> Optional[dict[str, object]]:
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM report_exports WHERE period_id=? AND result_version=? AND variant=? AND is_draft=? AND source_digest=?",
                (period_id, result_version, variant, int(is_draft), source_digest),
            ).fetchone()
            return dict(row) if row else None

    def register_report_export(self, period_id: str, result_version: int, variant: str, is_draft: bool,
                               source_digest: str, stored_path: str, sha256: str) -> str:
        export_id = str(uuid.uuid4())
        with self._lock, self.connection:
            self.connection.execute(
                "INSERT INTO report_exports(id,period_id,result_version,variant,is_draft,source_digest,stored_path,sha256) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (export_id, period_id, result_version, variant, int(is_draft), source_digest, stored_path, sha256),
            )
        return export_id

    def get_report_export(self, export_id: str) -> dict[str, object]:
        with self._lock:
            row = self.connection.execute("SELECT * FROM report_exports WHERE id=?", (export_id,)).fetchone()
            if not row:
                raise ValueError("报告不存在")
            return dict(row)

    def start_period(self, period_id: str, template_id: str = "a4-49-v1") -> str:
        with self._lock, self.connection:
            period = self.connection.execute("SELECT * FROM periods WHERE id=?", (period_id,)).fetchone()
            if not period or period["status"] != "draft":
                raise ValueError("只有草稿周期可以开始")
            project = self.connection.execute("SELECT group_count FROM projects WHERE id=?", (period["project_id"],)).fetchone()
            students = self.connection.execute(
                "SELECT s.*,pdm.group_number draft_group,pdm.sort_order draft_order FROM period_draft_members pdm "
                "JOIN students s ON s.id=pdm.student_id WHERE pdm.period_id=? AND s.is_active=1 "
                "ORDER BY pdm.group_number,pdm.sort_order,s.student_number", (period_id,)
            ).fetchall()
            if not students:
                raise ValueError("名单为空")
            if len(students) > 49:
                raise ValueError("当前 A4 纸表模板最多容纳49人，请调整名单或模板")
            if any(student["draft_group"] is None for student in students):
                raise ValueError("仍有学生未分组，不能开始周期")
            rules = self.connection.execute("SELECT * FROM rules WHERE project_id=? AND is_active=1 ORDER BY side,sort_order", (period["project_id"],)).fetchall()
            if sum(rule["side"] == "front" for rule in rules) > 7 or sum(rule["side"] == "back" for rule in rules) > 6:
                raise ValueError("启用项目超出纸表容量")
            leaders = self.connection.execute("SELECT * FROM period_draft_groups WHERE period_id=? ORDER BY group_number", (period_id,)).fetchall()
            member_ids = {student["id"]: student["draft_group"] for student in students}
            if any(not group["leader_student_id"] or member_ids.get(group["leader_student_id"]) != group["group_number"] for group in leaders):
                raise ValueError("每组必须指定一名本组组长")
            if len(leaders) != project["group_count"]:
                raise ValueError("分组草稿缺少小组记录")
            counts = {group: sum(student["draft_group"] == group for student in students) for group in range(1, project["group_count"] + 1)}
            if any(count == 0 for count in counts.values()):
                raise ValueError("每组至少需要一名成员")
            if project["group_count"] == 7 and len(students) == 49 and any(count != 7 for count in counts.values()):
                raise ValueError("当前49人、7组班级必须每组恰好7人")
            self.connection.executemany(
                "INSERT INTO period_students(period_id,student_id,student_number,name,group_number,row_index) VALUES(?,?,?,?,?,?)",
                [(period_id, s["id"], s["student_number"], s["name"], s["draft_group"], i) for i, s in enumerate(students)],
            )
            self.connection.executemany("INSERT INTO period_groups(period_id,group_number,leader_student_id) VALUES(?,?,?)", [(period_id, g["group_number"], g["leader_student_id"]) for g in leaders])
            self.connection.executemany(
                "INSERT INTO period_rules(period_id,rule_id,name,side,unit_score,sort_order) VALUES(?,?,?,?,?,?)",
                [(period_id, r["id"], r["name"], r["side"], r["unit_score"], r["sort_order"]) for r in rules],
            )
            self.connection.executemany(
                "UPDATE students SET group_number=?,is_unassigned=0 WHERE id=?",
                [(student["draft_group"], student["id"]) for student in students],
            )
            self.connection.execute("UPDATE class_groups SET leader_student_id=NULL WHERE project_id=?", (period["project_id"],))
            self.connection.executemany(
                "UPDATE class_groups SET leader_student_id=? WHERE project_id=? AND group_number=?",
                [(group["leader_student_id"], period["project_id"], group["group_number"]) for group in leaders],
            )
            self.connection.execute("UPDATE periods SET status='active',started_at=CURRENT_TIMESTAMP WHERE id=?", (period_id,))
            return self.issue_sheet(period_id, template_id, connection=self.connection)

    def issue_sheet(self, period_id: str, template_id: str = "a4-49-v1", *, connection=None) -> str:
        db = connection or self.connection
        period = db.execute("SELECT status FROM periods WHERE id=?", (period_id,)).fetchone()
        if not period or period["status"] != "active":
            raise ValueError("只能为进行中周期签发纸表")
        number = db.execute("SELECT COALESCE(MAX(sheet_number),0)+1 FROM paper_sheets WHERE period_id=?", (period_id,)).fetchone()[0]
        sheet_id = str(uuid.uuid4())
        def write():
            db.execute("INSERT INTO paper_sheets(id,period_id,sheet_number,template_id) VALUES(?,?,?,?)", (sheet_id, period_id, number, template_id))
            db.executemany("INSERT INTO paper_sides(sheet_id,side) VALUES(?,?)", [(sheet_id, "front"), (sheet_id, "back")])
        if connection is None:
            with self._lock, db:
                write()
        else:
            write()
        return sheet_id


def parse_student_csv(text: str) -> list[dict[str, object]]:
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    required = {"学号", "姓名", "组号", "是否组长"}
    if not reader.fieldnames or not required.issubset(reader.fieldnames):
        raise ValueError("CSV 表头必须包含：学号、姓名、组号、是否组长")
    return [{
        "student_number": row["学号"], "name": row["姓名"], "group_number": row["组号"],
        "is_leader": row["是否组长"].strip().lower() in {"1", "是", "true", "y", "yes"},
    } for row in reader]
