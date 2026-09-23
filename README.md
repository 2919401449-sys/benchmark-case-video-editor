# 标杆案例视频自动剪辑 Skill

面向“客户对镜口播＋相关B-roll”的标杆案例视频。它从实际A-roll语音建立V1，修正明显转写错误，压缩成约5–6.5分钟的V2，自动生成A-roll粗剪、B-roll覆盖、字幕、动态图表、品牌角标和人物标签，并在Windows上输出可继续编辑的剪映草稿。

当前版本：`0.1.0`

## 平台支持

| 功能 | Windows | macOS |
|---|---:|---:|
| A-roll转写、去重、V1/V2 | 支持 | 支持 |
| B-roll分析与匹配 | 支持 | 支持 |
| 字幕、图表、人物标签、Logo | 支持 | 支持 |
| 预览视频和JSON剪辑计划 | 支持 | 支持 |
| 剪映可编辑草稿 | 已验证11.4.2/11.5.0 | 尚未适配 |

macOS可以完成核心分析和素材生成，再把工作目录交给Windows完成剪映草稿导出。Mac版剪映草稿适配器是后续开发项。

## 安装到Codex

建议使用私有GitHub仓库，因为包内包含公司通用录屏和品牌素材。公开发布前请先确认素材版权。

Windows PowerShell：

```powershell
git clone <repository-url> "$env:USERPROFILE\.codex\skills\benchmark-case-video-editor"
cd "$env:USERPROFILE\.codex\skills\benchmark-case-video-editor"
.\scripts\setup.ps1
.\.venv\Scripts\python.exe .\scripts\doctor.py
```

macOS终端：

```bash
git clone <repository-url> ~/.codex/skills/benchmark-case-video-editor
cd ~/.codex/skills/benchmark-case-video-editor
bash scripts/setup.sh
.venv/bin/python scripts/doctor.py
```

首次语音识别会下载Whisper模型。下载完成后，转写过程可以在本地运行。

## 素材目录

```text
客户案例_AI智能设计平台/
├── A-roll/
│   ├── 人物1_01.mp4
│   └── 人物2_01.mp4
├── B-roll/
│   ├── 办公环境.mp4
│   └── 产品操作.mp4
├── 客户logo.png
├── V0文稿.docx                 可选，仅用于身份等元数据
├── speaker_map.json            多人物项目建议提供
└── transcript_corrections.json 可选，专有名词修正
```

文件夹名称包含`AI智能设计平台`时，会启用包内的酷家乐AI通用录屏；包含`生产对接`时不会启用。源素材始终只读，所有中间结果应写入另外的工作目录。

## 在Codex中使用

重新打开一个Codex任务后，可以直接说：

```text
使用 $benchmark-case-video-editor 处理 D:\素材\客户案例_AI智能设计平台，
工作目录放到 D:\视频工作\客户案例-v1，先检查素材并生成可审核的V1和V2。
```

Skill会根据`SKILL.md`依次调用脚本，并在事实不确定、人物身份无法确认或剪映草稿需要实际打开验收时停止并提示。

## Windows剪映导出

当前导出器使用随包携带的`pyJianYingDraft`，不需要同事另外克隆该项目。加密草稿还需要Windows剪映安装目录中的`videoeditor.dll`。

先生成明文结构测试：

```powershell
.\.venv\Scripts\python.exe .\scripts\export_jianying.py `
  --edit-plan "D:\工作目录\edit_plan_with_charts.json" `
  --inventory "D:\工作目录\media_inventory_with_charts.json" `
  --draft-root "D:\工作目录\plain\Drafts" `
  --artifacts-dir "D:\工作目录\plain\artifacts" `
  --draft-name "客户案例_结构测试" `
  --mode plain
```

结构测试通过后，再使用从未存在过的新草稿名运行`encrypted`模式，并提供当前剪映版本目录。不要覆盖已经确认的草稿。

## 验证与开发

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s scripts -p "test_*.py"
.\.venv\Scripts\python.exe scripts\doctor.py --jianying-install "D:\JianyingPro\当前版本"
```

主要目录：

- `SKILL.md`：Codex工作流程入口。
- `references/`：A-roll、B-roll、图表、字幕和剪映导出的详细规则。
- `scripts/`：确定性处理脚本和自动测试。
- `assets/`：通用AI录屏及系列品牌素材。
- `vendor/`：固定版本的剪映草稿第三方库及其许可证。

## 生成GitHub发布包

```powershell
.\.venv\Scripts\python.exe .\scripts\package_release.py --output "D:\release"
```

命令会生成一个干净目录和同名ZIP，自动排除虚拟环境、缓存、工作目录和草稿文件，并写入SHA-256清单。

本仓库当前用于内部协作，未附加本项目自身的开源许可证。公开发布前应先确定代码许可和素材再分发范围。
