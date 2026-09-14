# 第三方许可证

## Noto Sans SC

- 来源：Google Fonts `ofl/notosanssc/NotoSansSC[wght].ttf`
- 运行文件：`assets/fonts/NotoSansSC-Regular.ttf`（由官方可变字体固定为 400 字重）
- SHA-256：`7cf5bd68acf6e5fc6c45d7ba7ce27891976b505fefb95512a23bea5596c295c0`
- 许可证：SIL Open Font License 1.1，完整文本见 `assets/fonts/OFL-NotoSansSC.txt`。

## 主要运行与构建依赖

| 组件 | 锁定版本 | 许可证标识 |
|---|---:|---|
| FastAPI | 0.128.8 | MIT |
| Uvicorn | 0.39.0 | BSD-3-Clause |
| ReportLab | 4.4.9 | BSD |
| qrcode | 8.2 | BSD |
| pypdfium2 / PDFium | 4.30.0 | Apache-2.0 OR BSD-3-Clause；PDFium 另含其第三方许可 |
| pypdf | 6.10.0 | BSD-3-Clause |
| NumPy | 2.0.2（Python 3.9） | BSD-3-Clause；二进制发行物含其声明的兼容依赖 |
| OpenCV headless | 4.12.0.88 | Apache-2.0 |
| python-multipart | 0.0.20 | Apache-2.0 |
| Pillow | 11.3.0 | MIT-CMU |
| PyInstaller | 6.16.0 | GPL-2.0-or-later with bootloader exception |
| Playwright（仅开发/截图验证） | 1.63.0 | Apache-2.0 |

实际许可证全文以各锁定发行包随附的 `LICENSE`、`COPYING`、包元数据及 pypdfium2/PDFium 第三方声明为准。本表用于记录本次构建采用的版本和许可标识，不替代原文。
