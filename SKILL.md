---
name: benchmark-case-video-editor
description: Build or run a benchmark-customer video editing workflow from fragmented or repeated A-roll takes and related B-roll footage. Transcribes and deduplicates actual speech into V1, reduces it by complete semantic blocks to a 5–6.5 minute V2, then produces an A-roll rough cut, B-roll plan, evidence-checked information graphics, review report, and editable Jianying draft. A supplied V0 script is optional metadata and is not used as edit copy.
---

# Benchmark Case Video Editor

Turn a folder containing `A-roll/` and `B-roll/` into a reviewable edit plan while preserving original media. The spoken A-roll—not a pre-interview or original V0 script—is the source of truth.

## Operating rules

- Treat source media as read-only. Write every generated file under a separate work directory.
- Use proxy frames and extracted audio for analysis; reference the original paths and timecodes in final plans.
- Do not send full videos or every frame to a language model. Use transcripts, scene metadata, and representative thumbnails.
- Never hide low-confidence matches. Put them in the review report and keep them out of automatic final export unless the user approves them.
- A generated Jianying draft is an unofficial interchange artifact. Keep the JSON edit plan as the source of truth and verify the draft in Jianying before delivery.

## Expected input

Accept either explicit paths or discover these items in the supplied project folder:

- one or more videos under `A-roll/`;
- zero or more videos under `B-roll/`.
- optional `.txt`, `.md`, or `.docx` background script. Record it in the inventory but never use its wording to build V1.
- for multiple people or multiple files per person, a small `speaker_map.json` that maps source filenames to names and lists position priority. This avoids adding heavyweight face or voice recognition.
- optional `transcript_corrections.json` for project names and obvious speech-recognition homophones. Corrections may fix spelling but must not rewrite meaning or borrow sentences from V0.
- for series branding, mark the source folder name with `AI智能设计平台` or `生产对接`, and put one customer logo image in the folder root or a `logo/` subfolder. If the current folder is not yet marked, supply `--case-type ai` or `--case-type production` explicitly. More than one plausible image requires `--customer-logo` to avoid using the wrong brand.

If the folder structure differs, map the files without renaming or moving the originals.

## Workflow

1. Run `scripts/pipeline.py inspect` to create `media_inventory.json` and confirm duration, resolution, frame rate, codecs, and missing inputs. A V0 script is not required. For an AI-design-platform case, `inspect` also appends the bundled screen recordings under `assets/common-broll/ai-design-platform`; production-connection cases never receive them. Folder markers take priority, with an `AI标杆` V0 title accepted only as a fallback classification hint.
2. Run `scripts/pipeline.py thumbnails` to create one representative thumbnail for every case-specific and eligible common B-roll clip and `broll_index.json`. Common clips arrive with reviewed descriptions/tags and `origin: skill_common_ai`; case clips still require storyboard review and labels. Use `contact-sheets` when an agent or reviewer needs to inspect many clips efficiently.
3. Run `scripts/pipeline.py analyze-broll` before matching. It samples multiple frames, measures camera jitter and image quality, creates five-frame storyboards, and records stable source windows in `broll_analysis.json`. Do not choose a fixed center crop when this analysis is available.
4. Run `scripts/pipeline.py transcribe` on A-roll. Prefer local `faster-whisper`; use word timestamps and Chinese language hints unless the user requests a cloud transcription service. The command resumes from completed files in `transcript.json` after interruption.
5. Run `build-v1`. It groups clips by the speaker map, removes directing chatter and repeated takes, applies only approved term corrections, and writes the immutable `script_v1.json`/`.md`. Every V1 sentence must have `origin: transcript` plus its original source path and timecodes. Read [references/aroll-matching.md](references/aroll-matching.md) before changing this stage.
6. Run `prepare-v1-correction`, read the worksheet, and create `v1_correction_plan.json`. Correct homophones, proper nouns, obvious missing words, and ASR-created sentence breaks from the actual speech plus neighbouring transcript context. Do not polish the customer's speaking style or import wording from V0. Use `evidence: context` only for small, high-confidence changes; use `audio_relisten` or `second_asr` for larger repairs or any numerical change. Apply it with `correct-v1`. The raw V1 and all timecodes remain unchanged; the result is `script_v1_corrected.json`. If `v1_correction_report.md` lists unresolved audio review, stop before V2. Read [references/v1-correction.md](references/v1-correction.md) for the plan schema and safety rules.
7. Run `shorten-script` once to score the corrected V1 material, then read `script_v1_corrected.md` and create `v2_editorial_plan.json` from the selected complete ideas. Each block must contain one person's consecutive transcript order numbers; it may jump only over a directing/noise chunk. Judge the block as a listener: its first line must introduce the subject, and its final line must finish the claim, example, or result. Do not trust transcript chunk punctuation as a sentence boundary. Detect the highest-position speaker from spoken titles such as chairman, president, vice president, or general manager, using `speaker_priority` only as the tie-breaker. That main speaker must have the longest total duration. Excluding the main speaker, the longest person's duration divided by the shortest person's duration must be no more than 1.35. Prefer removing repeated lower-position commentary; retain complete unique examples and reliable business data. Career-tenure numbers, ordinals such as “第二天”, counters such as “一套”, and badly transcribed percentages are not automatically business KPIs. Re-run `shorten-script --max-minutes 6.5`; it must reject a plan that violates the 1500–1800 character range, 5:00–6:24 source-duration budget, semantic-edge checks, or the 35% balance rule. Read [references/aroll-matching.md](references/aroll-matching.md) for the plan schema.
8. Run `align`. For transcript-derived V2, copy each sentence's retained source timecodes directly into `roughcut_plan.json`; do not fuzzy-match it a second time. Only use the legacy global content matcher when no direct transcript anchors exist.
9. Review raw V1, corrected V1, V2, the correction/deduplication reports, and any uncertain lines. Correct transcription terms or speaker mapping in their small JSON configuration files, then regenerate; never edit source media.
10. Run `preview` only after the rough-cut plan exists. The preview is disposable; the timecoded JSON plan remains authoritative.
11. Describe and tag B-roll from multi-frame storyboards, merge the labels with `apply-labels`, and run `match-broll --max-source-uses 2`. Matching must operate on short clauses and combine semantic and quality scores. In AI-design-platform cases, treat each bundled recording's filename/manifest description as its approved content label and give a semantically relevant recording higher priority than customer-specific footage. The final compose gate must contain 3–5 distinct bundled recordings (target 4), each used at most once. If scoring alone yields fewer than 3, replace the best matching customer B-roll windows with unused recordings based on filename tags. Customer-specific source files retain the configurable hard cap, normally two uses. Once all relevant alternatives reach their limit, leave the passage on A-roll; never exceed a source-specific cap to chase coverage. For an already assembled timeline, run `inject-common-broll` rather than merely changing scoring. Review `broll_review.md`; read [references/broll-matching.md](references/broll-matching.md) before changing this stage.
12. Run `compose` to create `edit_plan.json`. Inspect `b_roll_metrics`: `source_use_limit_respected` must be true and `maximum_observed_source_uses` must be at most 2. The default coverage target remains 50%, but it may be missed when the available non-repeating footage is insufficient. A-roll flashes of 1.5 seconds or less between adjacent B-roll shots are bridged automatically. When source windows have safe head/tail room, extend them; otherwise reposition the unchanged stable windows. Do not solve coverage by reusing a third time or by choosing semantically unrelated footage. Read [references/outputs.md](references/outputs.md) before changing output formats.
13. Run `scripts/chart_pipeline.py propose --motion` after `edit_plan.json` is finalized when animated charts are requested (omit `--motion` for static output). Read [references/graphics.md](references/graphics.md). Review candidates against current subtitles, retain 5–10 useful facts or concrete explanations, and write accurate titles, labels and `cues` into `chart_plan.json`. The invoking agent performs this semantic review and continues for clear evidence; request user input only for unresolved facts. Preserve units, before/after direction, alternatives, negation and qualifiers such as “左右”. Mark checked charts `approved`, then run `sync` to anchor each value or step to retained A-roll word timestamps. When `style.visual_theme` is `bilingual`, add reviewed English fields without changing the Chinese facts, use distinct ring variants for successive stat cards, and require non-overlapping Chinese/English zones. A readability veil must cover the full 1920×1080 frame; Logo and subtitles remain above it through track order, rather than by cutting holes in the veil. Resolve reported timing failures before `render`. Inspect the contact sheet named in `chart_render_manifest.json`, then `attach`. Animated output creates both `edit_plan_with_charts.json` and `media_inventory_with_charts.json`; use the latter when exporting animated MOV layers. Edit-plan or transcript changes invalidate old timing; plan changes after rendering invalidate old assets. The standalone `all` command prepares proposals for agent review and never treats heuristic selection as approval.
14. If the case needs series branding, run `scripts/branding.py` after chart `attach` and before Jianying export. It creates two transparent, full-length corner layers from the preset KuJiaLe / AI platform marks and the customer's supplied logo. AI-design cases place KuJiaLe + customer at top left and AI platform at top right; production-connection cases place the preset series text at top left and KuJiaLe + customer at top right. KuJiaLe and AI platform lettering are white while the AI icon keeps its color; a white `|` separates equal-height KuJiaLe and customer marks. Do not copy customer logos into the reusable skill. Read [references/graphics.md](references/graphics.md) for visual constraints.
15. For Windows Jianying 11.4.2 or 11.5.0, read [references/jianying-export.md](references/jianying-export.md), run `scripts/export_jianying.py` in `plain` mode first, and only use `encrypted` mode after the structural package passes. The tested `pyJianYingDraft` revision is bundled under `vendor/`; do not depend on another machine-specific checkout unless `--repo` is deliberately provided. Use the DLL folder for the version currently installed, not a stale version directory. Generated caption text must contain no punctuation; replace spoken pauses with ordinary spaces. Always create a new draft name and verify it by opening it in Jianying. On macOS, stop after the cross-platform plans/assets/previews; Mac Jianying draft export is not yet implemented.

### Chart layout and caption revision

For animated chart output, insert `chart_layout.py prepare` → agent visual review of every sampled shot → `chart_layout.py apply` between chart `sync` and `render`. Read the subject-protection schema and layer-order rules in [references/graphics.md](references/graphics.md). Prefer a stable left/right position that avoids people throughout the interval; when neither side fits, center the card over a separate blurred moving-background layer. Before/after comparisons use full-screen presentation. Inspect composites over actual footage before exporting. The default caption size is now 4.0 (20% below the previous 5.0), with black outline; repeated runs must not reduce it again. If the user prohibits UI control, create and structurally verify the new draft without opening Jianying, and state that UI acceptance is pending.

### Final editorial and source-audio gate

After `compose` and before chart planning, read [references/editorial-quality.md](references/editorial-quality.md) and run `editorial_quality.py audit`. Removing a filming cue from text does not remove its sound. Check actual retained source segments, the opening, each new speaker, and phrase tails. Repair reviewed source boundaries on a new base plan, ripple all timelines, then rebuild dependent captions/charts/layout. Never use rewritten subtitles to conceal missing spoken words. `caption_segments` use word/phrase anchors rather than character-count timing; low-confidence phrases need explicit reviewed source anchors, not silent interpolation. The marketing pass verifies speaker identities and the scope of each business metric without inventing titles, results, payment amounts or contacts. Do not generate an opening theme/topic label. V0 remains excluded from edit copy, but a reviewed `姓名（公司职位）` heading may supply label-only identity metadata. Speaker cards use one compact, wide lower-left dark-blue glass panel with a translucent background: large white Chinese name, a thin vertical divider, Chinese role, letter-spaced company, and a small English role. Do not add a speaker number or dot. The five speakers use five controlled blue geometric accents so the system stays consistent without repeating one identical ornament. Confine every decorative shape to a narrow left accent zone; it must never pass beneath or cover the name, role, company, or English text. Width follows content and remains inside the subtitle/logo safe area. Use a fast slide from the left and fade-out, and show the card only on that speaker's clear A-roll. `presentation_notes.json` topic items are ignored. Subtitles use `transform_y=-0.88`; preset logos use a fixed 1.20 enlargement over the 46 px baseline, rounded to 55 px, keeping transparent backgrounds and 24 px corner margins.

## Invocation examples

Use the Python environment prepared for this skill:

```powershell
python scripts/pipeline.py inspect --project "E:\path\to\source" --work "E:\path\to\work"
python scripts/pipeline.py thumbnails --project "E:\path\to\source" --work "E:\path\to\work"
python scripts/pipeline.py analyze-broll --project "E:\path\to\source" --work "E:\path\to\work"
python scripts/pipeline.py transcribe --project "E:\path\to\source" --work "E:\path\to\work" --model small
python scripts/pipeline.py build-v1 --project "E:\path\to\source" --work "E:\path\to\work" --speaker-map "E:\path\to\speaker_map.json" --corrections "E:\path\to\transcript_corrections.json"
python scripts/pipeline.py prepare-v1-correction --project "E:\path\to\source" --work "E:\path\to\work"
python scripts/pipeline.py correct-v1 --project "E:\path\to\source" --work "E:\path\to\work" --review "E:\path\to\work\v1_correction_plan.json"
python scripts/pipeline.py shorten-script --project "E:\path\to\source" --work "E:\path\to\work" --primary-speaker "周总" --max-minutes 6.5
python scripts/pipeline.py align --project "E:\path\to\source" --work "E:\path\to\work"
python scripts/pipeline.py preview --project "E:\path\to\source" --work "E:\path\to\work"
python scripts/pipeline.py match-broll --project "E:\path\to\source" --work "E:\path\to\work" --max-source-uses 2
python scripts/pipeline.py compose --project "E:\path\to\source" --work "E:\path\to\work"
python scripts/chart_pipeline.py propose --work "E:\path\to\work" --count 8 --motion
# AI 对照 V2 审核 chart_plan.json，确认 status 和每项口播 cues 后继续
python scripts/chart_pipeline.py sync --work "E:\path\to\work"
python scripts/chart_pipeline.py render --work "E:\path\to\work"
python scripts/chart_pipeline.py attach --work "E:\path\to\work"
python scripts/branding.py --project "E:\path\to\source_AI智能设计平台" --work "E:\path\to\work"
python scripts/export_jianying.py --edit-plan "E:\path\to\work\edit_plan_with_charts.json" --inventory "E:\path\to\work\media_inventory_with_charts.json" --draft-root "E:\path\to\work\jianying-test\Drafts" --artifacts-dir "E:\path\to\work\jianying-test\artifacts" --draft-name "案例自动剪辑图表测试_v1" --mode plain
```

For a new Windows machine, run `scripts/setup.ps1`; on macOS/Linux run `bash scripts/setup.sh`. Then run `scripts/doctor.py`. The initial transcription model download can be large and requires network access. Windows-only Jianying export additionally requires the current installation folder containing `videoeditor.dll`.

## Completion criteria

An MVP run is complete only when:

- the original files remain unchanged;
- inventory counts match the source folders;
- V1 contains only transcript-origin sentences and every sentence keeps source timecodes;
- corrected V1 preserves every original order/source/timecode, records raw and corrected text for each edit, introduces no context-only numerical facts, and has no unresolved audio-review item before V2;
- V2 contains 1500–1800 visible characters, retains reliable non-duplicate business data in complete context, uses complete reviewed blocks, measures 5–6.5 minutes after cut padding, and has zero unresolved items in `script_v2_coherence_report.md`;
- the highest-position speaker has the longest duration, and among all other speakers `longest / shortest <= 1.35`;
- every V2 sentence has a matched, review, or missing status;
- selected A-roll ranges do not substantially overlap within the same source file; final timeline order follows the script rather than filenames;
- no B-roll source file appears more than twice in the final timeline; a lower coverage ratio is acceptable when necessary to satisfy this rule;
- the review report names all uncertain selections;
- when charts are requested, every chart is approved against the current subtitle and edit-plan hash, the rendered contact sheet has been visually checked, and `chart_metrics.review_required` is false; animated values/steps have verified speech anchors, word times and readability adjustments are reported, and separate PNG/MOV layers have editable timing and position (their internal text is baked into images);
- when series branding is requested, the folder annotation or explicit case-type override is recorded, both prescribed corner placements are present for the full edit duration, and customer logo selection is unambiguous; do not place a customer-specific mark inside the reusable skill;
- generated previews and draft files can be traced back to source file paths and timecodes.
