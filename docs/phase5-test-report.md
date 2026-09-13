# Phase 5 验证报告

日期：2026-09-13

## 结论

Phase 5 的 scorepack、恢复防护、版本迁移、安全退出和 macOS Apple Silicon 便携包已经交付并通过当前环境验证。Windows x64 构建脚本已经交付，但没有 Windows 构建环境，所以 Windows 成品明确标为“未构建、未验证”。

## 项目包实现

- 导出由 ScoreFlow 系统界面完成，不依赖 Codex。每次用 SQLite Backup API 取得一致快照，再裁剪为单个项目。
- `.scorepack` 是 UTF-8 相对路径的未加密 ZIP。`manifest.json` 记录项目 ID、格式/数据库/应用版本、UTC 时间，以及所有载荷的大小和 SHA-256。
- 包含目标项目数据库、配置、全部已签发纸表 PDF 与版式 manifest、被数据库引用的原始扫描、校正图及报告。
- 输出先写临时文件，成功后原子更名。证据和已生成纸表均按不可变文件处理。

## 恢复安全与一致性

- 提取前拒绝绝对/上跳/反斜杠路径、重复路径、符号链接、超过 10,000 文件、单文件超过 512MB、解压总量超过 2GB、项目包超过 1GB及异常压缩比。
- 修改当前数据库之前，逐文件复核大小与 SHA-256，并检查格式版本、SQLite `integrity_check`、外键、单一项目身份和全部引用文件。
- 恢复保留项目、周期、纸表、识别版本、入账批次和账本业务 ID。原始与反向流水的自关联在延迟外键事务中完整恢复。
- 恢复写入一个新项目；同项目 ID 已存在时拒绝覆盖。失败会回滚数据库、清理本次新文件，并把原包和 JSON 原因留在 `imports/scorepacks/failed/`。
- 回归验证恢复后的已入账纸表再次点击不会产生第二次入账。

## 自动验证

- `pytest -q`：48 项通过。
- 新增 scorepack 用例覆盖：中文及空格路径、单项目裁剪、数据库/纸表/扫描/校正/报告恢复、入账与反向流水身份、恢复后防重复入账、篡改哈希拒绝、路径穿越拒绝、同项目防覆盖、失败原因留存。
- 前端 TypeScript/Vite 生产构建通过；`npm audit --audit-level=high` 为 0 个已知漏洞。
- 打包预检确认 OpenCV 4.12.0、NumPy 2.0.2、PDFium 4.30.0、ReportLab 4.4.9 可导入。

## 平台包状态

| 平台 | 状态 | 产物/入口 | 实测范围 |
|---|---|---|---|
| macOS Apple Silicon arm64 | 已构建、已验证 | `dist/ScoreFlow.app`、`dist/ScoreFlow-macOS-arm64.zip` | Mach-O arm64；清洁环境不依赖 Python/Node；仅回环动态端口；中文空格数据目录；离线建班、名单、周期、纸表 PDF、scorepack、安全退出 |
| Windows x64 | 脚本已完成，成品未验证 | `scripts/build_windows.ps1` | 当前无 Windows 环境，不能声称已交付成品 |

macOS ZIP 大小约 80MB，SHA-256：`37ea62b90a36654ed0f782e6d8e90fa7e7495ce250bb89caa9b173e658563f52`。内部主可执行文件经 `file` 确认为 Mach-O 64-bit arm64。

本次 macOS 包在 macOS 26.6.2 arm64 构建，使用 ad-hoc 签名，没有 Apple Developer ID 签名或公证。正式面向其他教师分发前仍应完成签名、公证和目标系统回归，不建议关闭系统整体安全保护。

## 升级与数据目录

源码模式仍使用仓库 `var/`。打包后的 macOS 应用使用 `~/Library/Application Support/ScoreFlow/`；Windows 使用 `%LOCALAPPDATA%\ScoreFlow\`。程序资源与用户数据库分离，替换 `.app` 或 Windows 程序目录不会覆盖业务数据。SQLite schema 继续使用版本迁移，恢复的旧格式数据库会先迁移再导入。
