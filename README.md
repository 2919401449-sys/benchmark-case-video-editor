# 标杆案例视频自动剪辑 Skill

把碎片化、重复拍摄的客户A-roll和相关B-roll处理成约5–6.5分钟的标杆案例视频，并在Windows上生成可继续编辑的剪映草稿。

当前产品化版本：`0.2.6`

## 0.2.6新增

- 首次安装只有在依赖、Whisper模型、FFmpeg和剪映DLL全部通过后才写入运行环境标记，失败安装不能误启动正式案例。
- 安装时保存已验证的剪映版本目录；以后启动案例可以复用。开始菜单、桌面快捷方式或不含`videoeditor.dll`的目录会在处理素材前立即停止。
- GitHub发布包自动核对SHA-256、排除本机缓存，并检查单文件大小和总文件数是否适合网页直接上传。

## 0.2.5新增

- 新增 `parallelism` 排比句冲击图表：识别 3–5 个短促、同构、连续口播要点，逐项跟随原词从左右交替快速拍入。
- 排比图采用全屏深色透明蒙版与中英双语信息条，但不模糊人物；该区间只保留 A-roll，自动释放重叠 B-roll 的素材使用次数。
- 普通并列信息仍使用 `list`，只有结构和语义均成立的排比句才使用强动效，避免整片视觉疲劳。

## 0.2.4新增

- 左上酷家乐与客户品牌组放大到 70 像素可见高度，与右侧 AI 平台字标获得更接近的视觉分量。
- 列表、流程和单数字图优先单边展示；非对比图避免长时间全屏模糊。
- 模糊图表排除 B-roll 后增加一次覆盖率回补审核，默认目标仍为 50%。

## 0.2.3新增

- 全画面模糊图表仅使用 A-roll 生成背景，并自动移除或裁切同区间 B-roll，避免无效占用素材复用额度。

## 0.2.2新增

- 新增 `-CaseType ai|production` 显式类型覆盖；当素材文件夹命名错误时，统一修正通用录屏、品牌角标和后续剪辑逻辑。
- B-roll 前中后分布或素材复用上限未通过时停止导出，避免生成后段无素材的草稿。
- 人物标签审核文件与当前剪辑计划哈希不一致时回到审核关口，不在后续渲染阶段报错。

## 0.2.1新增

- 强制最高职位者的公司介绍作为全片第一个语义段，并按审核 block 顺序真实生成时间线。
- B-roll 按前、中、后三段轮转分配素材使用额度，输出各段覆盖率并阻止后半段素材枯竭。
- 图表优先覆盖三个时间段，第一张位于前35%，最后一张位于后35%，避免集中在后半段。

## 0.2.0解决了什么

- 安装与正式剪辑分离，不再边剪视频边修Python环境；
- 固定Whisper/CTranslate2等依赖版本，拒绝Codex缓存Python和Windows Store占位程序；
- 安装时预下载并验证Whisper模型；
- 用`run_state.json`记录每个阶段，可以跨任务、重启和额度窗口续跑；
- 已有完整产物自动接管，不重复转写和分析；
- 每个阶段单独写日志，同一错误最多重试两次；
- V1、V2、B-roll、图表和人物标签保留必要的语义审核关口；
- 自动执行明文结构草稿→加密剪映草稿，避免覆盖旧草稿。

## 平台

| 功能 | Windows | macOS |
|---|---:|---:|
| A-roll转写、V1/V2、B-roll、字幕、图表 | 支持 | 技术预览 |
| 剪映可编辑草稿 | 已验证11.4.2/11.5.0 | 尚未适配 |

## 第一次安装

电脑必须先安装64位 Python 3.11或3.12。不要复制另一台电脑的`.venv`。剪映路径必须是包含`videoeditor.dll`的实际版本目录，不能填写开始菜单`Programs`目录或快捷方式。

```powershell
git clone <repository-url> "$env:USERPROFILE\.codex\skills\benchmark-case-video-editor"
cd "$env:USERPROFILE\.codex\skills\benchmark-case-video-editor"
.\scripts\setup.ps1 -JianyingInstall "D:\JianyingPro\当前版本"
```

安装脚本会创建独立`.venv`、安装锁定依赖、下载`small`模型并运行严格自检。首次安装需要网络和一定时间，但以后每个案例不会重复安装或下载模型。只有自检全部通过才会生成`.runtime.json`；若安装失败，应修复报告中的原因后重新运行安装，不能让Codex改用便携Python或临时环境继续剪辑。

受管控电脑可指定允许写入的缓存位置：

```powershell
.\scripts\setup.ps1 `
  -JianyingInstall "D:\JianyingPro\当前版本" `
  -CacheRoot "D:\BenchmarkEditorCache"
```

## 素材目录

```text
客户案例_AI智能设计平台/
├── A-roll/
│   ├── 口播01.mp4
│   └── 口播02.mp4
├── B-roll/
│   ├── 办公环境.mp4
│   └── 客户沟通.mp4
├── 客户logo.png
├── V0文稿.docx                 可选，只提供姓名/职位/公司等身份信息
├── speaker_map.json            可选；没有时运行器会在转写后暂停生成
└── transcript_corrections.json 可选；专有名词修正表
```

项目名包含`AI智能设计平台`时启用内置AI录屏；包含`生产对接`时使用生产对接品牌规则。源素材始终只读，工作目录必须放在素材目录之外。

## 运行一个案例

```powershell
.\scripts\run_case.ps1 -Action start `
  -Project "D:\素材\客户案例_AI智能设计平台" `
  -Work "D:\视频工作\客户案例" `
  -DraftRoot "D:\JianyingPro Drafts" `
  -JianyingInstall "D:\JianyingPro\当前版本" `
  -CaseType ai `
  -DraftName "客户案例_自动剪辑_v1"
```

程序会执行到下一个必须进行语义判断的关口，并在工作目录生成`NEXT_ACTION.md`。让Codex只完成该文件要求的审核，然后继续：

```powershell
.\scripts\run_case.ps1 -Action resume -Work "D:\视频工作\客户案例"
```

查看当前进度：

```powershell
.\scripts\run_case.ps1 -Action status -Work "D:\视频工作\客户案例"
```

出现错误时，先看工作目录`logs/阶段名.log`。修复原因后显式重试：

```powershell
.\scripts\run_case.ps1 -Action resume -Work "D:\视频工作\客户案例" -RetryFailed
```

程序不会自行换模型、下载另一套Python或重新安装依赖。同一阶段失败两次后会停止。

完整运行和恢复说明见[`references/product-runbook.md`](references/product-runbook.md)。

## 在Codex中怎么说

首次安装完成后，新建任务并发送：

```text
使用 $benchmark-case-video-editor 的产品运行器处理下面的案例。
素材目录：D:\素材\客户案例_AI智能设计平台
工作目录：D:\视频工作\客户案例
剪映草稿目录：D:\JianyingPro Drafts
剪映安装目录：D:\JianyingPro\当前版本
草稿名：客户案例_自动剪辑_v1

请先查看或创建run_state.json，只从当前阶段继续。禁止临时安装依赖、切换Python、切换ASR模型，禁止重新执行已经通过的阶段。运行器暂停时，只读取NEXT_ACTION.md要求的文件，完成当前审核后继续。
```

## 开发验证

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s scripts -p "test_*.py"
.\.venv\Scripts\python.exe scripts\doctor.py --strict `
  --jianying-install "D:\JianyingPro\当前版本" --require-jianying
```

## 发布包

```powershell
.\.venv\Scripts\python.exe .\scripts\package_release.py --output "D:\release"
```

发布包排除`.venv`、模型缓存、运行状态、日志、工作目录和草稿文件，包含依赖锁定文件和固定版本的剪映草稿适配器。

上传或同事解压后可再次验证发布包完整性：

```powershell
python .\scripts\package_release.py --verify .
```

本仓库未附加本项目自身的开源许可证。公开分发前请确认公司通用录屏、Logo和代码的授权范围。
