# 产品运行手册

本手册只处理安装、启动、断点恢复和故障停止。剪辑判断规则由其他参考文档定义。

## 一次性安装（Windows）

产品环境必须使用独立的64位 Python 3.11或3.12。不要使用Windows Store占位程序、Codex缓存Python或从另一台电脑复制的`.venv`。

```powershell
cd "$env:USERPROFILE\.codex\skills\benchmark-case-video-editor"
.\scripts\setup.ps1 -JianyingInstall "D:\JianyingPro\当前版本"
```

`-JianyingInstall`必须指向包含`videoeditor.dll`的实际版本目录。开始菜单中的`Programs\剪映专业版`只是快捷方式位置，不能用于草稿加密。

安装脚本会：

1. 创建本Skill专用`.venv`；
2. 安装`requirements-lock.txt`中固定的依赖组合；
3. 将Whisper模型下载到用户级固定缓存；
4. 验证CPU int8转写、FFmpeg、通用素材和剪映DLL；
5. 所有必需检查通过后，才写入不随Git发布的`.runtime.json`和环境报告，并保存已验证的剪映版本目录。

受管控电脑可以用`-CacheRoot "D:\允许写入的缓存目录"`指定模型缓存。仅做开发自检时可用`-SkipModelDownload`，正式交付电脑不应跳过模型下载。

## 启动新案例

```powershell
.\scripts\run_case.ps1 -Action start `
  -Project "D:\素材\客户案例_AI智能设计平台" `
  -Work "D:\视频工作\客户案例" `
  -DraftRoot "D:\JianyingPro Drafts" `
  -JianyingInstall "D:\JianyingPro\当前版本" `
  -CaseType ai `
  -DraftName "客户案例_自动剪辑_v1"
```

如果素材文件夹名写错、未标类型，或用户后来更正类型，必须用 `-CaseType ai` / `production` 显式指定。该值优先于文件夹名，并同时控制通用B-roll与角标，不能只手工替换Logo。

运行器只使用`.venv`中的Python。详细输出写入`工作目录/logs/`，聊天中只返回阶段结果或失败日志尾部。

## 断点状态

`工作目录/run_state.json`是唯一的阶段状态源。运行器会采用已存在且通过完整性检查的转写、V1/V2、时间线和审核结果。不要删除工作目录后重新开始。

查看状态：

```powershell
.\scripts\run_case.ps1 -Action status -Work "D:\视频工作\客户案例"
```

继续：

```powershell
.\scripts\run_case.ps1 -Action resume -Work "D:\视频工作\客户案例"
```

如果某阶段失败，修复明确原因后只允许显式重试：

```powershell
.\scripts\run_case.ps1 -Action resume -Work "D:\视频工作\客户案例" -RetryFailed
```

同一阶段最多尝试两次。达到上限后必须人工查看日志；禁止自动换模型、重装Python或重新克隆仓库。

## 语义审核关口

运行器在以下位置安全暂停，并将唯一下一步写入`NEXT_ACTION.md`：

- `speaker_review`：根据实际转写与身份资料生成`speaker_map.json`；
- `v1_review`：审核转写修正，生成`v1_correction_plan.json`；
- `v2_review`：审核完整语义段，生成`v2_editorial_plan.json`；
- `broll_review`：查看联系表，生成`broll_labels.json`；
- `chart_review`：核对图表事实与口播锚点；
- `chart_layout_review`：逐帧标记人物和重点对象保护框；
- `speaker_label_review`：只生成姓名、职位、公司标签，不生成主题标签。

Codex只处理当前关口所需的小文件。完成后调用`resume`，不要重新阅读全部历史、重新执行已通过阶段或重新生成上游文件。

## 有意修改上游结果

如果人工修改了一个已经通过的上游结果，先重置该阶段及之后的状态；命令不会删除任何产物：

```powershell
.\scripts\run_case.ps1 -Action reset -Work "D:\视频工作\客户案例" -FromStage v2_review
```

随后再执行`resume`。这可以防止旧字幕、图表或草稿继续引用已变化的时间线。

## 环境与案例故障的边界

- 环境安装、依赖下载和模型预加载只能在`setup.ps1`中发生一次；安装失败时不得留下“可运行”标记，也不得改用便携Python继续案例。
- 正式案例运行中不得执行`pip install`、下载Python、切换ASR后端或更换Whisper模型。
- `doctor.py --strict`失败时，不得进入素材处理。
- 源素材只读；工作目录不得位于素材目录内部。
- 明文结构草稿成功后才允许生成新的加密草稿；绝不覆盖旧草稿。
