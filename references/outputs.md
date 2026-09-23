# 输出与可编辑性

## 核心文件

`roughcut_plan.json` 是 A-roll 选择结果的事实来源。任何剪映草稿、预览视频或其他时间线格式都应能够追溯到其中的源文件、入点和出点。

`broll_index.json` 保存 B-roll 的代表画面、描述和标签。不要把缩略图路径当作最终视频源路径。

`chart_plan.json` 保存审核后的图表文字、数据、模板和时间位置。`edit_plan_with_charts.json` 是在原剪辑计划上增加图表轨的派生文件；不要为了加入图表而覆盖 `edit_plan.json`。

## 推荐输出顺序

1. `media_inventory.json`
2. `transcript.json`
3. `roughcut_plan.json` 与 `review_report.md`
4. `broll_index.json`
5. `edit_plan.json`：包含 A-roll 主轨、B-roll 覆盖轨、字幕和音频处理建议
6. `chart_candidates.json`、`chart_plan.json` 与 `chart_contact_sheet.jpg`
7. `edit_plan_with_charts.json`
8. `preview.mp4`
9. 剪映草稿目录或其他 NLE 时间线

## 剪映草稿注意事项

剪映草稿格式不是稳定的公开交换标准，不同桌面版可能改变字段。导出适配器应：

- 把素材路径、素材时长和时间基统一放入一层转换模块；
- 保存剪映版本号和适配器版本；
- 不覆盖用户已有草稿；
- 打开剪映后检查素材是否离线、时间线是否错位、帧率是否一致；
- 失败时仍交付 JSON 计划、CSV 和预览视频。
