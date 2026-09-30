# 模型队列自动轮换离线验收记录

日期：2026-09-30。对应规范：`2026-09-29-model-pool-auto-rotation-spec.md` v1.2.0。

## 实施结果

- 普通查找、Finder 全阶段和共用目录评估器接入同一轮换实现；运行对象独立于配置字典。
- 严格加载模型队列，拒绝新旧路径冲突及不支持参数；保留旧单模型模式。
- 精确识别百炼 `AllocationQuota.FreeTierOnly`，普通 403、欠费和未知额度码不触发轮换。非零用量冲突先记账后停止。
- 输入超过当前模型上限时跳过，重试切换到新模型时重置；任务请求与用量累计、预算不重置。
- 每次物理请求保存开始事实、预算、实际模型与有效参数指纹，随后保存 usage 和拒绝证据；拒绝不伪造零 usage。保留未知结果保护。
- 耗尽状态使用线程锁、跨进程文件锁和原子替换；覆盖配置删除清理、迟到响应、损坏状态及锁超时。等待预算后重新检查模型是否仍可用。
- 全池耗尽保留候选与阶段检查点；追加模型后普通查找和 Finder 可续跑；反思恢复采用最后一次物理响应，避免误用先前的拒绝响应。
- 续跑候选主键与队列成员分离，实际模型指纹附加到事实记录。队列模式暂不启用没有明确期望模型身份的跨任务规范化缓存；同任务已完成成果继续保留。
- 展示和检查工具不创建状态/锁文件；切换工具拒绝覆写队列；真实测试工具要求显式指定队列中的单一模型。
- 更新运行说明、架构约定、Markdown/JSON 报告及前端停止提示。

## 验证结果

以下为删除前的历史执行记录。按用户要求，本次新增的 `tests/test_model_pool.py` 和 `tools/run_offline_tests.py` 已删除；下表涉及这两个文件的命令不再可用，测试数量也不代表当前保留的测试集合。

所有模型接口、搜索和抓取均使用模拟响应，状态与配置写入临时目录。离线运行器禁止 socket 连接；没有调用真实模型或修改真实模型队列。

| 命令 | 结果 |
| --- | --- |
| `.\.venv\Scripts\python.exe tools/run_offline_tests.py tests.test_model_pool` | 36 项通过 |
| `.\.venv\Scripts\python.exe tools/run_offline_tests.py tests.test_model_pool tests.test_failure_policy tests.test_evaluate tests.test_switch_model` | 62 项通过 |
| `.\.venv\Scripts\python.exe tools/run_offline_tests.py` | 708 项：707 项通过，1 项原有失败 |
| `npm test` | 41 项通过 |
| `git diff --check` | 通过；仅提示 Windows 的 LF/CRLF 转换 |

新增测试覆盖配置和指纹、错误白名单、全池耗尽、输入跳过、重试重置、两进程合并、线程迟到响应、清理竞争、存储故障、预算、两入口恢复、反思恢复、工具只读保护和共用评估器记账。

## 原有失败

`tests.test_catalog_quality.QualityTest.test_invalid_dependency_blocking_type_is_processing_failure`：测试期望字符串 `blocking="false"` 被拒绝；已有解析器将该字符串转换为布尔值，因此测试失败。

已通过 `git show HEAD:src/catalog/evaluation.py` 加载修改前模块，在内存中重跑同一测试，得到相同失败。本次未改变该既有解析行为，也未修改该测试以掩盖问题。

## 运行边界

本次为离线实现验收，不构成真实服务商接口联调结果。需由用户在百炼侧开启“用完即停”，并在真实配置中提供可用模型列表。状态不自动恢复额度，更换账户不会自动清空已有耗尽记录。
