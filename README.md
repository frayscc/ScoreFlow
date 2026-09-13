# ScoreFlow

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

ScoreFlow 是完全本地运行的班级积分纸表与 OMR 工具。开发计划 Phase 0—5 已完成：班级与名单、周期快照、参数化双面纸表、批量扫描导入、页面身份匹配、四态识别、教师复核、可靠入账、周期关闭、报告，以及完整项目包导出/恢复。

## 直接运行（macOS Apple Silicon）

本机已构建并冒烟验证：双击 `dist/ScoreFlow.app`。也可把 `dist/ScoreFlow-macOS-arm64.zip` 复制到另一台 Apple Silicon Mac 后解压运行。该测试包是 ad-hoc 签名，尚未使用 Apple Developer ID 签名或公证；首次从网络取得时，macOS 可能要求在“隐私与安全性”中明确确认打开。不要关闭系统整体安全保护。

最终用户不需要安装 Python 或 Node.js。应用数据保存在 `~/Library/Application Support/ScoreFlow/`，替换应用不会覆盖数据。软件内的“退出 ScoreFlow”会安全停止任务和服务；关闭浏览器标签不会退出后台。

Windows x64 的同源构建脚本为 `scripts/build_windows.ps1`，但当前 Mac 环境不能产出或验证 Windows 成品，因此不宣称 Windows 包已交付。

## 源码运行

本仓库已经安装依赖并构建好前端时，在仓库根目录执行：

```bash
.venv/bin/python -m scoreflow.launcher
```

启动器会只在本机 `127.0.0.1` 上启动服务，并自动用默认浏览器打开 ScoreFlow。数据默认保存在 `var/`。再次执行同一命令会打开正在运行的实例。

现在可以实际操作：建立班级、自定义组数、导入名单、自定义周期周次与积分项目、签发或下载纸表、生成补充表、导入扫描 PDF/图片、查看质量提示、人工确认疑点、选择扫描版本、整表入账、撤销纠错、关闭周期，并导出教师存档版或教室展示版 PDF。

界面中的“完整备份与跨电脑恢复”可直接生成 `.scorepack`。它是未加密 ZIP，包含一致性 SQLite 快照、纸表和版式 manifest、原始扫描、校正证据及报告；恢复前会校验路径、体积、版本、SHA-256、SQLite 完整性和引用文件。恢复始终建立独立项目，遇到同一项目身份会拒绝覆盖。

## 开发运行

要求：Python 3.9+、Node.js 20+。首次安装：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
npm --prefix frontend ci
```

开发模式分别运行（两个终端）：

```bash
.venv/bin/python -m scoreflow.main
npm --prefix frontend run dev
```

构建前端并由同源本地服务提供：

```bash
npm --prefix frontend run build
.venv/bin/python -m scoreflow.main
```

浏览器访问后端终端显示的带本地会话凭据地址。服务只绑定 `127.0.0.1`。通常更推荐使用上面的启动器入口。

```bash
.venv/bin/python -m scoreflow.launcher
```

## Phase 1 样表

```bash
.venv/bin/python scripts/generate_phase1_sample.py
.venv/bin/python scripts/generate_simulated_scan.py --marks-per-side 120
.venv/bin/python scripts/recognize_sample.py var/phase1/simulated-front.png
.venv/bin/pytest
```

输出位于 `var/phase1/`。请按 [打印扫描验收说明](docs/print-scan-validation.md) 完成真实纸样测试；模拟样本不能替代实扫验收。

Phase 3 的实现与验收范围见 [Phase 3 验证报告](docs/phase3-test-report.md)。
Phase 4 的实现与验收范围见 [Phase 4 验证报告](docs/phase4-test-report.md)。
Phase 5 的实现、便携包状态与验收范围见 [Phase 5 验证报告](docs/phase5-test-report.md)。

## 开源许可

ScoreFlow 项目原创代码采用 [MIT License](LICENSE)。字体及运行依赖仍遵循各自许可证，详见 [第三方许可证说明](THIRD_PARTY_LICENSES.md)。当前 V2 修改与跨平台发布仍在进行中，真实 Notability 导出和纸质扫描尚未完成用户验收。
