# Skills 导航目录 清理清单（可执行）

依据：`docs/Skills导航目录二开需求.md` §8.2–§8.8、§7.1、§7.4；配套 `docs/Skills导航目录实施文档.md`。
状态：**波次 1 已执行**（2026-09-20）；波次 0 部分完成，波次 2、3 未执行。

## 执行记录

| 时间 | 波次 | 结果 |
|---|---|---|
| 2026-09-20 | 0（部分） | 记录 10 个第三方许可证类型（§1.1）；按 §1.3 建议放弃 `SKILL_SOURCES.json` 线索提取 |
| 2026-09-20 | 1 | 删除 24 个路径、932 个跟踪文件；工作区 349.8 MB → 94.7 MB |
| 挂起 | 1 | `vercel.json`、`.vercelignore` **未删除**，前置条件未满足（§2.2） |
| 未开始 | 2、3 | 旧文档、旧运行链路与旧数据 |

已确认的不可逆项：`projects/assistant/assets/logo/` 下 13 个被忽略文件共 137.2 MB（§1.2）已按决定删除，无法从 git 恢复。

---

## 0. 波次划分

| 波次 | 内容 | 可执行时机 | 依据 |
|---|---|---|---|
| 0 | 抢救：许可证、未跟踪素材、可复用代码、线索 | 任何删除之前 | §8.8 步骤 2 |
| 1 | 旧产品、无关同步与旧展示 | 立即可执行（`vercel.json` 除外） | §8.2、§8.3 |
| 2 | 旧文档 | 新 README 与操作文档就绪后 | §8.5 |
| 3 | 旧运行链路、旧数据、旧工具 | 新索引/页面/任务接通后 | §8.4 |

---

## 1. 波次 0：删除前必须处理

### 1.1 第三方许可证与署名（10 个文件）

需求 §8.7 要求复用代码时保留许可证、版权与署名。这些文件都位于将被整体删除的 `skills/` 内。**删除前已逐一核对许可类型并记录如下**（2026-09-20）：

| 路径 | 许可 | 署名 |
|---|---|---|
| `skills/antinet-doc-parse/LICENSE` | Apache-2.0 | — |
| `skills/antinet-four-color-cards/LICENSE` | Apache-2.0 | — |
| `skills/antinet-provenance/LICENSE` | Apache-2.0 | — |
| `skills/antinet-security-scan/LICENSE` | Apache-2.0 | — |
| `skills/legal-assistant-skills-main/contract-review/LICENSE.txt` | Apache-2.0 | — |
| `skills/legal-assistant-skills-main/law-to-markdown/LICENSE.txt` | Apache-2.0 | — |
| `skills/obsidian-skills-integrated/LICENSE` | MIT | Copyright (c) 2026 Steph Ango (@kepano) |
| `skills/qiaomu-x-article-publisher/LICENSE` | MIT | Copyright (c) 2026 Qiaomu |
| `skills/qiaomu-x-article-publisher/qiaomu-x-article-publisher-github/LICENSE` | MIT | Copyright (c) 2026 Qiaomu |
| `skills/content-creation-publisher/baoyu-post-to-wechat/scripts/md/LICENSE` | WTFPL | 改编自 doocs/md（https://github.com/doocs/md） |

**结论**：新流程不计划复用上述任何技能的代码，因此不迁移许可证文件；来源与许可记录保留在本表中。若将来复用其中任何代码，须先按其许可要求恢复署名。

### 1.2 ⚠️ 13 个被忽略文件，137.2 MB，git 无法恢复

`projects/.gitignore` 第 3 行 `*.mp4` 及 `*.zip` 规则使下列文件**从未进入 git 历史**。删除 `projects/` 即永久丢失：

| 大小 | 文件 |
|---|---|
| 30.77 MB | `projects/assistant/assets/logo/锦衣卫多智能体知识系统路演.mp4` |
| 28.99 MB | `projects/assistant/assets/logo/锦衣卫多智能体知识系统路演视频.mp4` |
| 12.11 MB | `projects/assistant/assets/logo/1月27日/1月27日.mp4` |
| 11.89 MB | `projects/assistant/assets/logo/锦衣卫多智能体知识系统路演视频 (1).zip` |
| 11.89 MB | `projects/assistant/assets/logo/锦衣卫多智能体知识系统路演视频.zip` |
| 11.51 MB | `projects/assistant/assets/logo/锦衣卫多智能体知识系统路演.zip` |
| 7.64 MB | `projects/assistant/assets/logo/jimeng-2026-01-27-7344-…mp4` |
| 5.37 MB | `projects/assistant/assets/logo/科技徽章.livp.mp4` |
| 5.05 MB | `projects/assistant/assets/logo/锦衣卫协作图谱.livp.mp4` |
| 4.21 MB | `projects/assistant/assets/logo/logo1.mp4` |
| 2.90 MB | `projects/assistant/assets/logo/antinetbot.mp4` |
| 2.86 MB | `projects/assistant/assets/logo/锦衣卫系统.livp.mp4` |
| 2.00 MB | `projects/assistant/assets/logo/logo5.mp4` |

**动作**：删除前必须先行决定——搬出仓库保留，还是确认可弃。这是本次清理中唯一无法用 git 回滚的一批文件。

### 1.3 `SKILL_SOURCES.json`（691 KB）的线索提取

文件内 `dsh` 出现 7197 次，旧索引很可能以被排除对象为主体。**建议直接放弃提取**，改用 §4.3 的初始来源表作种子；若坚持提取，只保留人工确认的非 DSH、非本仓库条目，不继承任何"可信/推荐"状态（§4.2）。

### 1.4 可复用代码

| 文件 | 可复用部分 | 处理 |
|---|---|---|
| `scripts/sync_skills.py` | 采集与来源配置结构 | 迁移后重写为新入口，或新模块就绪后移除（§8.6） |
| `tools/skill_validator.py`、`tools/skill_validator/` | Markdown/元数据解析逻辑 | 迁移必要部分后删除旧入口（§8.4） |
| `main.py`、`crawler.py`、`data_manager.py`、`config.py` | 抓取、重试、存储 | 迁移有用逻辑后删除（§8.4） |

### 1.5 README 的许可证表述

`README.md` 徽章为 CC-BY-4.0（L7），正文"许可证"节为 MIT（L339），且**根目录没有任何 LICENSE/COPYING/NOTICE 文件**。这是 LICENSE 决策（见 §9）的输入，需在删除任何东西前定案。

---

## 2. 波次 1：立即删除

### 2.1 §8.2 与目标无关的旧产品

| 路径 | 规模 | 备注 |
|---|---|---|
| `skills/` | 565 文件 / 4.40 MB | 71 个目录中仅 17 个含 `SKILL.md`；先做 §1.1 |
| `projects/` | 218 文件 / 236.02 MB | **先做 §1.2**；含 `projects/.gitignore` |
| `antinet-agentteams/` | 134 文件 / 13.78 MB | |
| `finance-skills/` | 7 文件 | |
| `templates/` | 1 文件 | `soul-injection.md` |
| `SKILL.md`（根） | 6,820 B | 小跃虚拟伴侣入口 |
| `scripts/xiaoyue-companion.sh`、`xiaoyue-chat.ps1`、`xiaoyue-chat.js`、`xiaoyi-chat.ps1` | 4 文件 | |
| `images/gzh-qr-topgo.jpg`、`images/wechat-qr.jpg` | 2 文件 | 如需署名改用文本 |
| `package-lock.json` | 109 B | 空锁文件 |

### 2.2 §8.3 无关同步与旧展示

| 路径 | 备注 |
|---|---|
| `.github/workflows/sync_clawhub.yml` | 每天同步 ClawHub 的任务 |
| `scripts/sync_clawhub.py` | |
| `SKILL_SOURCES.json` | 完成 §1.3 后 |
| `public/chat-demo.html`、`public/projects.html`、`public/local-skills.html` | |
| `public/index-en.html`、`public/skills-en.html`、`README_EN.md` | 第一版只做中文 |
| `public/llms.txt`、`public/llms-full.txt`、`scripts/gen_llms_full.py` | |
| `vercel.json`、`.vercelignore` | ⚠️ **带前置条件，见下** |

**`vercel.json` 删除前置条件**（§7.1）：`vercel.json` 不是备用配置——它 `outputDirectory: "public"`，是当前 `skill.miyucaicai.cn` 线上站点的部署配置。必须在用户仓库核实没有需要沿用的 Vercel 自动部署后，才可删除。需求 §8.3 的"删除备用部署配置"表述与 §7.1 冲突，**以 §7.1 为准**。

---

## 3. 波次 2：新 README 与操作文档就绪后删除（§8.5）

| 分组 | 文件 |
|---|---|
| 根目录（5） | `API_INTEGRATION.md`、`DEMO-SUCCESS.md`、`DEPLOYMENT_CHECKLIST.md`、`DEPLOYMENT_GUIDE.md`、`DEPLOYMENT.md` |
| 根目录（7） | `FINAL-SUMMARY.md`、`PROJECT_SUMMARY.md`、`QUICKSTART.md`、`README-FINAL.md`、`SUMMARY.md`、`USAGE-GUIDE.md`、`XIAOYI-README.md` |
| 根目录（2） | `技能清单.md`、`金融接口技能汇总.md` |
| `docs/`（5） | `D盘tool目录整理完成报告.md`、`整理完成总结.md`、`技能管理数据库.md`、`技能清理与迁移指南.md`、`技能数量差异分析报告.md` |
| `docs/`（3） | `best-practices.md`、`quickstart.md`、`specification.md`（可用内容先整合） |

**保留**：`docs/Skills导航目录二开需求.md`、`docs/Skills导航目录实施文档.md`、本清单。不使用通配符批量删除 Markdown。

---

## 4. 波次 3：新流程接通后删除（§8.4）

| 分组 | 文件 |
|---|---|
| 旧爬虫与调度 | `main.py`、`crawler.py`、`scheduler.py`、`data_manager.py`、`config.py` |
| 旧 API | `api_client.py`、`api_server_example.py` |
| Windows 入口 | `run_once.bat`、`start_daemon.bat`、`setup_scheduled_task.ps1` |
| 旧工具 | `tools/skill_validator.py`、`tools/skill_validator/`、`tools/README.md` |
| 旧数据 | `data/skills.json`、`data/local_skills.json`、`data/last_update.txt` |
| 旧展示副本 | `public/data/skills.json`、`public/data/local_skills.json`、`public/data/last_update.txt` |

已核实：`data/` 与 `public/data/` 三份文件 MD5 逐字节相同，合并零风险。新结构下 `data/` 为唯一维护来源，`public/data/` 由程序生成。

---

## 5. 改而不删（§8.6）——含需删除的具体内容

### 5.1 `public/index.html`（重点，需求 §8 未点出）

该页除了是导航页，还是 **TOPGO 站群矩阵推广页**。下列内容必须删除或重写：

| 行 | 内容 | 处理 |
|---|---|---|
| L520–556 | `<!-- Ecosystem -->` 整块："🌐 TOPGO 站群矩阵 / 14 站点在线"，14 个 `*.miyucaicai.cn` 外链 | **整块删除** |
| L558–572 | `<!-- Contact -->` 整块："📬 加入社群"，含 3 个站群外链 + 微信二维码（`img` 指向 `raw.githubusercontent.com/anbeime/skill/…/wechat-qr.jpg`） | **整块删除** |
| L575–580 | `<footer>`："© 2026 TOPGO 站群矩阵 · skill.miyucaicai.cn · Powered by anbeime" | 重写；如需署名用文本形式 |
| L578 | "数据每 24h 自动更新" | 改为每周，与新周期一致 |
| L381 | `<a href="projects.html">项目案例</a>` | 删除（页面将不存在） |
| L384、L444 | "知易体验 / 立即体验" → `ai123.miyucaicai.cn` | 删除 |
| L503 | `<a href="skills-en.html">English</a>` | 删除（英文版首版不做） |

### 5.2 `public/skills.html`

合并进主导航后删除。合并前先清理它自己的失效引用：L255 `skills-en.html`、L509 `local-skills.html`。

### 5.3 `public/robots.txt`、`public/sitemap.xml`

`robots.txt` 第 1 行与 L91、`sitemap.xml` 全部 **8 条 URL** 都指向 `skill.miyucaicai.cn`，其中 6 条指向将被删除的页面（`skills-en`、`local-skills`、`projects`、`index-en`、`llms`、`llms-full`）。按用户 Pages 的 `/skill/` 子路径重新生成，不保留任何旧地址。26 个 AI 爬虫白名单本身可沿用。

### 5.4 其余保留并改写

`README.md`（人工维护，不再承载自动生成的全量列表与 ClawHub 信息）、`CONTRIBUTING.md`、`.github/ISSUE_TEMPLATE/submit-skill.yml`（分类值改为 `config/taxonomy.json` 的十个分类）、`.github/workflows/sync-skills.yml`（顶层入口）、`deploy-pages.yml`（改为 `workflow_call`）、`scripts/sync_skills.py`、`public/styles.css`、`requirements.txt`（收敛为 `requests` + `PyYAML`）、`.gitignore`（真正忽略 `.env` 与本地配置，而不是注释掉）、`.nojekyll`。

---

## 6. 需求 §8 未覆盖、本清单补充的删除项

1. `public/index.html` 的 TOPGO 站群矩阵与加入社群整块（§5.1）——§8 只提到 README 里的推广区块。
2. `public/skills.html` 指向待删页面的两处引用（§5.2）。
3. `public/sitemap.xml` 的 8 条旧地址全部需要重写，不只是"移除旧地址"。
4. `projects/.gitignore` 随目录删除，但它造成的 13 个未跟踪文件是独立的抢救项（§1.2）。

---

## 7. 不删（§8.7）

`.git/` 与已有历史；新建的需求、实施、清理、运行文档；新流程复用代码所需的许可证与署名；用户本地未跟踪的 IDE 配置与工作资料（如 `.idea/`）；仓库外资料（例如 `C:\Users\29311\.gitignore_global`）。不重写历史，不使用通配符删除全部 Markdown。

---

## 8. 执行后预期与验证

| 项 | 现在 | 清理后 |
|---|---|---|
| 工作区体积 | ~256 MB | 约 0.5 MB 级（src/config/data/public/docs） |
| git 跟踪文件 | 996 | 约 40–60 |
| `.git` 体积 | 93.9 MB | **仍为 93.9 MB**（历史保留） |

验证（§8.8 步骤 5、6）：无旧入口；无活跃 DSH 条目；无失效导航；额度上限、失败保留旧数据、候选/推荐状态与发布链路均可验证。

---

## 9. 执行前需决策的三件事

| # | 事项 | 影响 |
|---|---|---|
| 1 | §1.2 那 137.2 MB 素材：搬出保留还是放弃 | **唯一不可回滚项**，删了就没了 |
| 2 | 根目录是否新增 LICENSE 文件、选哪一种许可 | 阻塞波次 2 的文档重写；公开仓库无 LICENSE 默认保留所有权利 |
| 3 | 是否接受 `.git` 永久保留 93.9 MB 历史 | §8.7 要求不重写历史，则新克隆仍要下 ~94 MB 视频。若要小仓库，需要另起干净仓库或 orphan 分支，与 §8.7 冲突，需你定 |

需求文档 §10.2 仍把"目标仓库与发布地址"列为待定，但 §2/§7.1 已定案为 `justdoit712/skill` 与其 GitHub Pages，本清单按已定案执行。
