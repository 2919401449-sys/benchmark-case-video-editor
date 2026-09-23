"""Shot-aware chart placement. Visual review supplies boxes, never identities.

prepare samples every visible shot throughout each chart. The invoking agent
reviews all samples and records protected person/product boxes in canvas pixels.
apply chooses a single stable position for the whole interval or center+blur.
"""
from __future__ import annotations

import argparse
import math
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw

import chart_pipeline as cp
from pipeline import find_ffmpeg


def visible_spans(plan, start, end):
    clips = plan['tracks']
    cuts = {start, end}
    for track in ('a_roll', 'b_roll'):
        for clip in clips.get(track, []):
            cuts.update(t for t in (clip['timeline_in'], clip['timeline_out']) if start < t < end)
    ordered = sorted(cuts)
    result = []
    for lo, hi in zip(ordered, ordered[1:]):
        mid = (lo + hi) / 2
        active = [c for track in ('a_roll', 'b_roll') for c in clips.get(track, [])
                  if c['timeline_in'] <= mid < c['timeline_out']]
        if not active:
            raise ValueError(f'Uncovered timeline at {mid}')
        clip = active[-1]
        rate = (clip['source_out'] - clip['source_in']) / (clip['timeline_out'] - clip['timeline_in'])
        if abs(rate - 1) > 0.01:
            raise ValueError('Layout background currently requires normal-speed footage')
        result.append(dict(source=clip['source'], start=lo, end=hi,
                           source_in=clip['source_in'] + lo - clip['timeline_in']))
    return result


def frame_at(span, time, path):
    subprocess.run([find_ffmpeg(), '-v', 'error', '-y', '-ss', str(span['source_in'] + time - span['start']),
                    '-i', span['source'], '-frames:v', '1', '-vf',
                    'scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2',
                    str(path)], check=True, capture_output=True)


def prepare(work):
    plan = cp.load_json(work / 'edit_plan.json')
    charts = cp.load_json(work / 'chart_plan.json')
    directory = work / 'layout_review'
    directory.mkdir(exist_ok=True)
    review = {'source_edit_plan_sha256': cp.file_sha256(work / 'edit_plan.json'), 'charts': []}
    for chart in charts['charts']:
        samples = []
        for span in visible_spans(plan, chart['timeline_in'], chart['timeline_out']):
            n = max(2, math.ceil((span['end'] - span['start']) / 1.0) + 1)
            for i in range(n):
                at = span['start'] + min(0.02, (span['end']-span['start'])/4) + (
                    span['end'] - span['start'] - min(0.04, (span['end']-span['start'])/2)) * i / (n-1)
                path = directory / f"{chart['id']}_{len(samples):02d}.jpg"
                frame_at(span, at, path)
                samples.append({'time': round(at, 4), 'image': str(path.resolve()),
                                'source': span['source'], 'protected_boxes': None})
        sheet = Image.new('RGB', (1440, 300*math.ceil(len(samples)/3)), '#182336')
        d = ImageDraw.Draw(sheet)
        for i, sample in enumerate(samples):
            x, y = i%3*480, i//3*300
            with Image.open(sample['image']) as frame:
                sheet.paste(frame.resize((480, 270)), (x, y))
            d.text((x+8, y+273), f"{chart['id']}  #{i}  {sample['time']:.2f}s", fill='white')
        sheet.save(directory / f"{chart['id']}_sheet.jpg")
        review['charts'].append({'id': chart['id'], 'timeline_in':chart['timeline_in'],
                                'timeline_out':chart['timeline_out'], 'status': 'needs_review', 'samples': samples})
    cp.write_json(work / 'chart_layout_review.json', review)
    return review


def intersects(a, b, margin=30):
    return a[0] < b[2]+margin and a[2] > b[0]-margin and a[1] < b[3]+margin and a[3] > b[1]-margin


def choose_layout(chart, base_bounds, reviewed):
    if chart['template'] == 'comparison':
        return {'mode': 'fullscreen', 'blur': True, 'reason': '前后对比优先全屏展示'}
    boxes = [box for sample in reviewed['samples'] for box in sample['protected_boxes']]
    width, height = base_bounds[2]-base_bounds[0], base_bounds[3]-base_bounds[1]
    # Fit one consistent card across all shots. Do not shrink until unreadable.
    for scale in (1.0, 0.9, 0.8, 0.75):
        w, h = round(width*scale), round(height*scale)
        for side, x in [('left', 32), ('right', 1920-32-w)]:
            target = [x, 110, x+w, 110+h]
            if target[3] <= 935 and not any(intersects(target, box) for box in boxes):
                return {'mode': side, 'blur': False, 'box': target,
                        'reason': '全部抽样画面避开人物及重点对象 保留30像素缓冲'}
    scale = min(1.15, 1500/width, 800/height)
    w, h = round(width*scale), round(height*scale)
    return {'mode': 'center', 'blur': True,
            'box': [(1920-w)//2, (1080-h)//2, (1920-w)//2+w, (1080-h)//2+h],
            'reason': '左右均无法安全容纳 居中并模糊底层画面'}


def apply_review(work):
    import chart_motion as motion
    plan = cp.load_json(work / 'chart_plan.json')
    review = cp.load_json(work / 'chart_layout_review.json')
    if review['source_edit_plan_sha256'] != cp.file_sha256(work / 'edit_plan.json'):
        raise ValueError('Layout review is stale')
    for chart in plan['charts']:
        item = next(c for c in review['charts'] if c['id'] == chart['id'])
        if item['status'] != 'approved' or not item['samples']:
            raise ValueError('Review all layout samples before rendering')
        if (item['timeline_in'],item['timeline_out']) != (chart['timeline_in'],chart['timeline_out']):
            raise ValueError('Chart timing changed; resample layout')
        for sample in item['samples']:
            boxes = sample.get('protected_boxes')
            if not isinstance(boxes, list):
                raise ValueError('Unreviewed frame; use [] only when no subject needs protection')
            for box in boxes:
                if len(box) != 4 or not (0 <= box[0] < box[2] <= 1920 and 0 <= box[1] < box[3] <= 1080):
                    raise ValueError(f'Invalid protected box: {box}')
        raw = {k:v for k,v in chart.items() if k != 'layout'}
        base, _ = motion.layers_for_chart(raw, plan.get('style', {}))
        chart['layout'] = choose_layout(chart, base.getbbox(), item)
        chart['layout']['review_source'] = str((work/'chart_layout_review.json').resolve())
    plan['layout_policy'] = 'subject_safe'
    plan['layout_review_sha256'] = cp.file_sha256(work/'chart_layout_review.json')
    cp.write_json(work/'chart_plan.json', plan)
    return plan


def transform_layer(image, source_box, layout):
    if not layout or layout['mode'] == 'fullscreen':
        return image
    box = layout['box']
    target = Image.new('RGBA', (1920, 1080))
    artwork = image.crop(source_box).resize((box[2]-box[0], box[3]-box[1]), Image.Resampling.LANCZOS)
    target.alpha_composite(artwork, (box[0], box[1]))
    return target


def render_blur(work, chart, directory, fps):
    plan = cp.load_json(work/'edit_plan.json')
    spans = visible_spans(plan, chart['timeline_in'], chart['timeline_out'])
    parts = []
    # Frame-quantized boundaries guarantee no drift over B-roll cuts.
    total_frames = round((chart['timeline_out']-chart['timeline_in'])*fps)
    for i, span in enumerate(spans):
        lo = round((span['start']-chart['timeline_in'])*fps)
        hi = round((span['end']-chart['timeline_in'])*fps)
        if hi <= lo:
            continue
        part = directory / f"{chart['id']}_blur_part_{i}.mp4"
        subprocess.run([find_ffmpeg(), '-v', 'error', '-y', '-ss', str(span['source_in']), '-i', span['source'],
                        '-an', '-vf', f'scale=960:540:force_original_aspect_ratio=decrease,pad=960:540:(ow-iw)/2:(oh-ih)/2,gblur=sigma=22,scale=1920:1080,fps={fps},setsar=1',
                        '-frames:v', str(hi-lo), '-c:v', 'libx264', '-preset', 'fast', '-crf', '20',
                        '-pix_fmt', 'yuv420p', '-threads', '4', str(part)], check=True, capture_output=True)
        parts.append(part)
    listing = directory / f"{chart['id']}_blur_concat.txt"
    listing.write_text('\n'.join("file '" + str(p.resolve()).replace('\\','/').replace("'", "'\\''") + "'" for p in parts), encoding='utf-8')
    target = directory / f"{chart['id']}_background_blur.mp4"
    subprocess.run([find_ffmpeg(), '-v','error','-y','-f','concat','-safe','0','-i',str(listing),
                    '-c','copy',str(target)], check=True, capture_output=True)
    return {'chart_id': chart['id'], 'layer_key': 'background_blur', 'source': str(target.resolve()),
            'source_in': 0, 'source_out': total_frames/fps, 'timeline_in': chart['timeline_in'],
            'timeline_out': chart['timeline_out'], 'template': 'background_blur', 'z_group': 0,
            'status': 'approved', 'keyframes': [
                {'property':'alpha','time':t,'value':a} for t,a in
                [(0,0),(0.24,1),(chart['timeline_out']-chart['timeline_in']-0.24,1),
                 (chart['timeline_out']-chart['timeline_in'],0)]], 'title': chart['title']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['prepare','apply'])
    parser.add_argument('--work', type=Path, required=True)
    args = parser.parse_args()
    (prepare if args.action == 'prepare' else apply_review)(args.work)
