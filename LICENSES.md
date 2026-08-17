# Third-party licenses and clean-room boundary

Creative Claw 本身按 [MIT License](LICENSE) 发布。

本仓库是独立的 clean-room 实现，没有复制或导入 Vela 或其他 GPL 项目的源码、测试、资源、提示词或文档。Vela 仅作为公开产品行为参考，例如本地知识存储、文档导入、混合检索、引用和智能体工具。Creative Claw 不依赖 GPL-3.0 运行库。

直接运行时依赖及其上游许可证：

| Dependency | License |
| --- | --- |
| Flask | BSD-3-Clause |
| python-docx | MIT |
| python-pptx | MIT |
| openpyxl | MIT |
| pypdf | BSD-3-Clause |
| Requests | Apache-2.0 |
| SQLite（Python 标准库接口） | Public Domain |

发布正式版本前应锁定完整依赖树、检查传递依赖许可证，并生成 SBOM/第三方通知。该列表是工程边界说明，不构成法律意见。
