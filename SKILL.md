---
name: benchmark-case-video-editor
description: Turn fragmented customer A-roll and related B-roll into a checkpointed 5–6.5 minute benchmark-case edit with corrected transcript, balanced speakers, captions, animated charts, branding, and a Windows Jianying draft. Use for running, resuming, or diagnosing this packaged workflow; do not use V0 as edit copy.
---

# Benchmark Case Video Editor

Use the product runner to create a reviewable edit while keeping source media read-only. Actual A-roll speech is the source of truth; V0 supplies identity metadata only.

## Runtime contract

- On a new machine, installation is a separate one-time task. Read [product-runbook.md](references/product-runbook.md) and run `scripts/setup.ps1` before accepting a real case.
- During a case, use only `scripts/run_case.ps1`. Never install packages, download another Python, clone another copy, switch ASR backends, or change the Whisper model to recover from an error.
- `run_state.json` is the durable source of progress. Resume the same work directory and adopt completed stages; do not restart a case because chat context was compacted.
- Each failed stage stops immediately. Retry only after fixing the reported cause, and no more than twice.
- Detailed command output belongs in `work/logs/`, not in the model context.

## Start, resume, and inspect

Start a Windows case:

```powershell
.\scripts\run_case.ps1 -Action start `
  -Project "D:\素材\客户案例_AI智能设计平台" `
  -Work "D:\视频工作\客户案例" `
  -DraftRoot "D:\JianyingPro Drafts" `
  -JianyingInstall "D:\JianyingPro\当前版本" `
  -CaseType ai `
  -DraftName "客户案例_自动剪辑_v1"
```

文件夹类型标记错误、缺失或与用户最新确认不一致时，使用 `-CaseType ai` 或 `-CaseType production` 显式覆盖；显式值优先于文件夹名，并贯穿素材索引、通用录屏和品牌角标。

Inspect or resume:

```powershell
.\scripts\run_case.ps1 -Action status -Work "D:\视频工作\客户案例"
.\scripts\run_case.ps1 -Action resume -Work "D:\视频工作\客户案例"
```

When the runner pauses, read only `NEXT_ACTION.md` and the files named there. Complete that semantic review, then resume. Do not manually replay the preceding commands.

## Editorial invariants

- Preserve original media and keep the work directory outside the project folder.
- Build V1 only from transcript-origin speech with source path and timecodes. Correct ASR errors without importing V0 wording or inventing data.
- Build V2 from complete semantic blocks. Target 1500–1800 Chinese characters and 5:00–6:30. The highest-position speaker must have the longest duration; among other speakers, longest/shortest must be at most 1.35. The first semantic block must be that highest-position speaker's company introduction; do not open with conclusions, feature claims, or another speaker.
- Prefer concrete data, before/after changes, business outcomes and unique examples. Remove repeated lower-position commentary before the main speaker's version.
- Do not hide filming cues or missing speech with rewritten subtitles. Final captions contain no punctuation, use spaces for pauses, sit near the bottom, and have a black outline.
- Customer B-roll may be used at most twice. AI-platform cases use 3–5 distinct bundled screen recordings, each at most once and semantically matched. The default full-video B-roll target is at least 50%; lower coverage is acceptable only when relevant, stable material is genuinely insufficient and the report says so. Reserve source-use capacity across the opening, middle, and closing thirds: every third should contain relevant B-roll, and no third may consume the pool at the expense of a later third. After blurred-chart exclusions, run one redistribution pass over eligible non-blurred gaps before accepting reduced coverage.
- Avoid brief A/B/A flashes; bridge nearby B-roll and use stable interior source windows.
- Every chart fact, unit, direction and qualifier must be supported by retained speech. Protect people in every sampled shot. Prefer a semi-transparent single-side card for lists, flows and single statistics; reserve full-frame veil/blur for before/after comparisons or shots where neither side can avoid people. Treat a sequence of 3–5 short, semantically parallel clauses as `parallelism` only after semantic review confirms a genuine rhetorical series rather than an ordinary list. Its bilingual rows must reveal on the actual spoken phrases, alternate rapid left/right impact entrances, use a full-frame dark transparent veil without blur, and keep all copy above the caption-safe zone. Use A-roll only beneath this impact overlay and remove overlapping B-roll so its source-use allowance remains available elsewhere. A non-comparison blur lasting more than 12 seconds must be split or converted to a reviewed side card. A full-frame blurred chart interval uses A-roll as its blur source and contains no B-roll placement; trim or remove overlapping B-roll before export, recalculate coverage/source-use metrics, and leave the freed source-use quota available elsewhere. Chart selection must cover the opening, middle, and closing thirds when supported facts exist; the review gate must reject a chart set clustered entirely in one half of the video.
- Keep chart text and decorative geometry in separate verified safe zones. No glyph may touch or cross a ring, tick, connector, arrow, or decorative line; if fitting reaches the minimum type size, split the copy, switch layouts, or stop for review instead of allowing overlap.
- Do not generate an opening topic label. Speaker labels contain name, role and company, appear lower-left, slide in quickly and fade out.
- Generate a plain structural draft first, then a new encrypted Jianying draft. Never overwrite an existing draft.

## Review routing

Read only the reference needed at the current runner gate:

- A-roll mapping, V2 blocks and speaker balance: [aroll-matching.md](references/aroll-matching.md)
- Safe V1 correction: [v1-correction.md](references/v1-correction.md)
- B-roll semantics, stability, limits and common recordings: [broll-matching.md](references/broll-matching.md)
- Caption, source-audio and marketing audit: [editorial-quality.md](references/editorial-quality.md)
- Animated chart facts, layout, branding and speaker cards: [graphics.md](references/graphics.md)
- Output schemas and coverage metrics: [outputs.md](references/outputs.md)
- Jianying plain/encrypted export: [jianying-export.md](references/jianying-export.md)
- Installation, resume, retry and reset: [product-runbook.md](references/product-runbook.md)

## Completion

The run is complete only when `run_state.json` reports `complete`, all non-skipped stages are `passed`, the encrypted export report exists, and the user is told the draft name, duration, speaker durations, B-roll coverage/common recordings, charts, automatic repairs, and remaining Jianying review points.

On macOS, use the cross-platform analysis stages only and skip Jianying export; the editable Mac draft adapter is not yet implemented.
