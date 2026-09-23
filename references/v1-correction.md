# V1 语句修正

## 为什么放在 V1 与 V2 之间

口音、语速和环境声会让 ASR 把同音词识别错，或在一句话中间人为断开。若直接用这份文字打分，V2 可能错误删除有价值内容，B-roll 也会匹配到错误画面。

修正阶段只提高“文字对真实口播的还原度”，不负责精简内容。原始 `script_v1.json` 永远保留；修正结果写入 `script_v1_corrected.json`，句号、人物、素材路径和起止时间均不能改变。

## 操作顺序

1. 运行 `prepare-v1-correction`，生成 `v1_correction_worksheet.json` 和 `.md`。
2. 逐句查看原文、前文、后文、人物和时间码。普通话不标准时，不能只看当前一句猜测。
3. 能从上下文明确判断的小错误可直接修正；不确定的句子列入 `needs_audio_review`。
4. 涉及数字、金额、面积、时间、比例、公司名或产品名时，优先复听对应时间段或进行更强模型的二次转写。
5. 写出 `v1_correction_plan.json`，再运行 `correct-v1`。
6. 查看 `v1_correction_report.md`。只要“仍需复听”不是 0，就不能生成 V2。

## 校正计划格式

```json
{
  "schema_version": 1,
  "edits": [
    {
      "order": 12,
      "original_text": "可以提高我们的精灵率。",
      "corrected_text": "可以提高我们的进店率。",
      "evidence": "context",
      "confidence": 0.96,
      "change_types": ["homophone"],
      "reason": "装修销售语境与相邻句均在谈客户进店"
    }
  ],
  "needs_audio_review": [
    {
      "order": 29,
      "reason": "口音较重，仅凭文字无法确认后半句"
    }
  ]
}
```

`evidence` 只能使用：

- `context`：从前后文即可高把握确认，只适合小范围同音字、专有名词和标点修复；
- `audio_relisten`：已复听原素材对应时间段；
- `second_asr`：已用更强模型对该时间段二次转写。

## 安全边界

- 不借用 V0 补写客户没有说出的句子。
- 不把口语改写成广告文案，不改变人物语气和观点。
- `context` 校正不得新增数字事实；数字变化必须来自复听或二次转写。
- 上下文校正与原文的字符相似度默认不得低于 0.55；复听或二次转写不得低于 0.25。超过范围时程序拒绝应用。
- 不确定就保留原文并复听，不能为了让句子“更好看”而猜。
- 修正只改变 `text`，不得改变 `order`、`speaker`、`source`、`source_in` 或 `source_out`。

