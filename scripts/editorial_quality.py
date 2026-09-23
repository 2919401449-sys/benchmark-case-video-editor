"""Evidence-based final checks: source chatter, captions and factual signposts.

This is not a speaker-identification or automatic copywriting model. Facts and
names must come from reviewed source speech/project metadata.
"""
from __future__ import annotations

import argparse
import math
import re
from difflib import SequenceMatcher
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFont

import chart_pipeline as cp
from chart_motion import canonical, timed_characters, fade_keys
from export_jianying import clean_caption_text, split_caption_text, caption_plan_signature
from pipeline import is_directing_chatter,timeline_coverage_metrics


def _text_width(draw, text, font):
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0]


def _centered_text_y(draw, text, font, top, height):
    box=draw.textbbox((0,0),text,font=font)
    return top+(height-(box[3]-box[1]))/2-box[1]


def speaker_label_keys(duration):
    """Fast horizontal entrance, followed by a clean opacity-only exit."""
    slide = min(0.24, duration / 3)
    fade = min(0.38, duration / 4)
    return [
        {'property':'position_x','time':0,'value':-0.46},
        {'property':'position_x','time':slide,'value':0},
        {'property':'alpha','time':0,'value':1},
        {'property':'alpha','time':max(slide, duration-fade),'value':1},
        {'property':'alpha','time':duration,'value':0},
    ]


def speaker_identities_from_v0(work):
    """Read verified name/role metadata only; never use V0 as spoken copy."""
    inventory_path = work/'media_inventory.json'
    if not inventory_path.exists():
        return {}
    script_value = cp.load_json(inventory_path).get('script')
    if not script_value:
        return {}
    script = Path(script_value)
    if not script.is_file() or script.suffix.lower() not in {'.txt','.md'}:
        return {}
    text = script.read_text(encoding='utf-8-sig')
    identities = {}
    for raw_line in text.splitlines():
        line = raw_line.strip().replace('**','')
        match = re.match(r'^([\u4e00-\u9fffA-Za-z·]{2,8})\s*[（(]\s*([^）)]+?)\s*[）)]',line)
        if match:
            identities[match.group(1)] = match.group(2).strip()
    return identities


def resolved_speaker_fields(item, identities):
    name = str(item.get('name') or item.get('title') or '').strip()
    organization = str(item.get('organization') or '').strip()
    role = str(item.get('role') or '').strip()
    subtitle = str(item.get('subtitle') or '').strip()
    if not organization and subtitle:
        organization = subtitle.split()[0]
    verified = identities.get(name,'')
    if verified and not role:
        if organization and verified.startswith(organization):
            role = verified[len(organization):].strip()
        else:
            role = verified
    return name, role, organization


def render_speaker_label(item, identities, width, height):
    name, role, organization = resolved_speaker_fields(item, identities)
    if not name or not role or not organization:
        raise ValueError(f'人物标签缺少已核实的姓名、职位或公司：{item}')
    image = Image.new('RGBA',(width,height),(0,0,0,0))
    measuring = ImageDraw.Draw(image)
    name_font = ImageFont.truetype(str(cp._font_path(True)), 82)
    role_font = ImageFont.truetype(str(cp._font_path(False)), 42)
    org_font = ImageFont.truetype(str(cp._font_path(False)), 29)
    english_font = ImageFont.truetype(str(cp._font_path(False)), 19)
    role_english = {
        '副总裁':'VICE PRESIDENT',
        '董事长':'CHAIRMAN',
        '总裁':'PRESIDENT',
        '总经理':'GENERAL MANAGER',
        '销售':'SALES',
        '设计师':'DESIGNER',
    }.get(role, role.upper() if role.isascii() else '')
    label_index=max(1,int(item.get('label_index') or 1))
    name_width=_text_width(measuring,name,name_font)
    role_width=_text_width(measuring,role,role_font)
    # Keep the card compact but leave enough room for the approved name/divider/role hierarchy.
    panel_width=min(840,max(650,name_width+role_width+250))
    panel_height=196
    x1=64
    panel_y=max(80,height-panel_height-118)

    card=Image.new('RGBA',(panel_width,panel_height),(0,0,0,0))
    layer=Image.new('RGBA',card.size,(0,0,0,0))
    layer_draw=ImageDraw.Draw(layer)
    layer_draw.rectangle((0,0,panel_width,panel_height),fill=(5,22,43,154))

    # Five controlled geometric variants: same system, different speaker accents.
    variant=(label_index-1)%5
    if variant in {0,3}:
        radius=150 if variant==0 else 126
        cy=panel_height//2 if variant==0 else panel_height+8
        for inset in range(0,42,3):
            alpha=max(18,112-inset*2)
            layer_draw.ellipse((-radius+inset,cy-radius+inset,radius-inset,cy+radius-inset),
                               fill=(35,125,255,alpha),outline=(111,191,255,min(175,alpha+55)),width=2)
    elif variant in {1,2}:
        wedge=132 if variant==1 else 106
        layer_draw.polygon([(0,0),(wedge,0),(35,panel_height),(0,panel_height)],
                           fill=(38,124,255,132))
        layer_draw.polygon([(wedge,0),(wedge+68,0),(103,panel_height),(35,panel_height)],
                           fill=(64,142,241,38))
    else:
        layer_draw.pieslice((-170,-164,220,226),0,90,fill=(42,125,255,130))
        layer_draw.pieslice((-96,-110,242,228),0,90,fill=(12,42,81,126))
        layer_draw.polygon([(panel_width-205,panel_height),(panel_width,panel_height-125),
                            (panel_width,panel_height)],fill=(33,92,178,30))

    # Decoration is confined to a narrow left-hand accent zone. Repainting the
    # content area prevents any arc/wedge from reducing name or company contrast.
    accent_safe_width=104
    layer_draw.rectangle((accent_safe_width,0,panel_width,panel_height),fill=(5,22,43,154))

    mask=Image.new('L',card.size,0)
    ImageDraw.Draw(mask).rounded_rectangle((0,0,panel_width-1,panel_height-1),radius=24,fill=255)
    layer.putalpha(ImageChops.multiply(layer.getchannel('A'),mask))
    card.alpha_composite(layer)
    draw=ImageDraw.Draw(card)
    draw.rounded_rectangle((1,1,panel_width-2,panel_height-2),radius=24,
                           outline=(103,164,225,154),width=2)

    name_x=110 if variant in {0,3,4} else 88
    name_y=38
    draw.text((name_x+2,name_y+3),name,font=name_font,fill=(0,0,0,95))
    draw.text((name_x,name_y),name,font=name_font,fill=(255,255,255,247))
    name_right=name_x+name_width
    divider_x=name_right+38
    draw.line((divider_x,57,divider_x,116),fill=(148,194,232,190),width=2)
    role_x=divider_x+34
    role_y=_centered_text_y(draw,role,role_font,48,78)
    draw.text((role_x,role_y),role,font=role_font,fill=(221,237,252,240))

    spaced_org=' '.join(organization)
    draw.text((name_x,137),spaced_org,font=org_font,fill=(169,204,232,222))
    if role_english:
        english_width=_text_width(draw,role_english,english_font)
        draw.text((panel_width-english_width-30,151),role_english,font=english_font,
                  fill=(125,169,207,205))

    image.alpha_composite(card,(x1,panel_y))
    return image


def render_label_previews(work, output_dir):
    config=cp.load_json(work/'presentation_notes.json')
    identities=speaker_identities_from_v0(work)
    output_dir.mkdir(parents=True,exist_ok=True)
    cards=[]
    for index,item in enumerate((x for x in config['cards'] if str(x.get('kind'))=='speaker'),1):
        preview_item={**item,'label_index':index}
        image=render_speaker_label(preview_item,identities,1920,1080)
        name=str(item.get('name') or item.get('title') or index)
        target=output_dir/f'{index:02d}_{name}.png'
        image.save(target)
        cards.append((name,image))
    if not cards:
        raise ValueError('没有可预览的人物标签')

    sheet=Image.new('RGB',(1800,1000),(10,18,31))
    sheet_draw=ImageDraw.Draw(sheet)
    title_font=ImageFont.truetype(str(cp._font_path(True)),42)
    small_font=ImageFont.truetype(str(cp._font_path(False)),24)
    sheet_draw.text((64,34),'九鼎装饰｜人物身份标签视觉提案',font=title_font,fill=(242,248,255))
    sheet_draw.text((66,94),'透明图层 · 左下角 · 快速滑入 / 渐隐离场',font=small_font,fill=(138,172,198))
    positions=[(50,150),(920,150),(50,430),(920,430),(485,710)]
    for (name,image),(cell_x,cell_y) in zip(cards,positions):
        sheet_draw.rounded_rectangle((cell_x,cell_y,cell_x+830,cell_y+240),24,
                                     fill=(20,34,54),outline=(43,67,94),width=2)
        bbox=image.getchannel('A').getbbox()
        cropped=image.crop(bbox)
        cropped.thumbnail((760,200),Image.Resampling.LANCZOS)
        px=cell_x+(830-cropped.width)//2
        py=cell_y+(240-cropped.height)//2
        sheet.paste(cropped,(px,py),cropped)
    contact=output_dir/'speaker_label_contact_sheet.png'
    sheet.save(contact)
    return [path for path in output_dir.glob('*.png')]


def source_chatter_issues(plan, transcript):
    files = {f['source']:f for f in transcript['files']}
    issues = []
    for clip in plan['tracks']['a_roll']:
        for segment in files[clip['source']]['segments']:
            if (segment['end'] > clip['source_in'] and segment['start'] < clip['source_out']
                    and is_directing_chatter(segment['text'])):
                issues.append({'section':clip['section'], 'source':clip['source'],
                               'source_in':max(segment['start'],clip['source_in']),
                               'source_out':min(segment['end'],clip['source_out']),
                               'text':segment['text'], 'reason':'retained_source_contains_preparation_speech'})
    return issues


def trim_opening(plan, source_start, fps):
    """Remove a reviewed leading interval and ripple ALL base tracks together."""
    import copy
    plan = copy.deepcopy(plan)
    if plan['tracks'].get('graphics'):
        raise ValueError('Trim the base plan before charts/branding and resync those derivatives')
    first = plan['tracks']['a_roll'][0]
    delta = round((source_start-first['source_in'])*fps)/fps
    if not 0 < delta < first['timeline_out']-first['timeline_in']:
        raise ValueError('Opening trim must stay within first retained clip')
    for track, clips in plan['tracks'].items():
        kept = []
        for clip in clips:
            if clip['timeline_out'] <= delta:
                continue
            clipped = max(0,delta-clip['timeline_in'])
            if clipped and 'source_in' in clip:
                clip['source_in'] = round(clip['source_in']+clipped,4)
            clip['timeline_in'] = round(max(0,clip['timeline_in']-delta),4)
            clip['timeline_out'] = round(clip['timeline_out']-delta,4)
            kept.append(clip)
        plan['tracks'][track] = kept
    plan['timeline']['duration_seconds'] = round(plan['timeline']['duration_seconds']-delta,4)
    return plan, delta


def revise_source_edges(plan, revisions):
    """Reviewed phrase-boundary extensions, keeping original content anchored."""
    import copy
    result=copy.deepcopy(plan)
    if result['tracks'].get('graphics'):
        raise ValueError('Repair base plan first; rebuild derived graphics')
    old={c['section']:c for c in plan['tracks']['a_roll']}
    cursor=0.0
    for clip in result['tracks']['a_roll']:
        revision=revisions.get(clip['section'],{})
        if revision.get('source_in',clip['source_in'])>clip['source_in'] or revision.get('source_out',clip['source_out'])<clip['source_out']:
            raise ValueError('This repair extends incomplete phrases only; use explicit removal review for cuts')
        clip.update({k:revision[k] for k in ('source_in','source_out') if k in revision})
        if clip['source_out']<=clip['source_in']:
            raise ValueError('Empty source edge revision')
        clip['timeline_in']=round(cursor,4)
        cursor+=clip['source_out']-clip['source_in']
        clip['timeline_out']=round(cursor,4)
    new={c['section']:c for c in result['tracks']['a_roll']}
    for sub in result['tracks']['subtitles']:
        c=new[sub['section']]
        sub['timeline_in'],sub['timeline_out']=c['timeline_in'],c['timeline_out']
    for clip in result['tracks'].get('b_roll',[]):
        t=clip['timeline_in']
        parent=next(c for c in old.values() if c['timeline_in']<=t<c['timeline_out'])
        changed=new[parent['section']]
        start=changed['timeline_in']+(t-parent['timeline_in'])+parent['source_in']-changed['source_in']
        duration=clip['timeline_out']-clip['timeline_in']
        clip['timeline_in']=round(start,4)
        clip['timeline_out']=round(start+duration,4)
    result['timeline']['duration_seconds']=round(cursor,4)
    for source in {c['source'] for c in result['tracks']['a_roll']}:
        ranges=sorted((c['source_in'],c['source_out']) for c in result['tracks']['a_roll'] if c['source']==source)
        if any(a[1]>b[0]+.001 for a,b in zip(ranges,ranges[1:])):
            raise ValueError('Source-edge extension duplicates retained speech')
    return result


def build_caption_segments(plan, transcript, overrides=None):
    files = {f['source']:f for f in transcript['files']}
    clips = {c['section']:c for c in plan['tracks']['a_roll']}
    captions, reviews = [], []
    fps = plan['timeline']['fps']
    for subtitle in plan['tracks']['subtitles']:
        clip = clips[subtitle['section']]
        words = [w for s in files[clip['source']]['segments'] for w in s.get('words',[])
                 if w.get('start') is not None and w.get('end') is not None
                 and w['end'] > clip['source_in'] and w['start'] < clip['source_out']]
        raw, times = timed_characters(words)
        parts = split_caption_text(subtitle['text'],18)
        normalized = [canonical(p) for p in parts]
        target = ''.join(normalized)
        mapping = {}
        for block in SequenceMatcher(None,target,raw,autojunk=False).get_matching_blocks():
            mapping.update({block.a+i:block.b+i for i in range(block.size)})
        cursor = 0
        section_captions = []
        for text, value in zip(parts,normalized):
            matched = [mapping[p] for p in range(cursor,cursor+len(value)) if p in mapping]
            coverage = len(matched)/max(1,len(value))
            override=next((o for o in (overrides or []) if o['section']==subtitle['section'] and o['text']==text),None)
            if override:
                if not (override.get('status')=='approved' and override.get('evidence') and
                        clip['source_in']<=override['source_in']<override['source_out']<=clip['source_out']):
                    raise ValueError('Invalid reviewed caption anchors')
            if not override and (len(matched)<2 or coverage<0.55):
                reviews.append({'section':subtitle['section'],'text':text,'coverage':round(coverage,3),
                                'reason':'insufficient_speech_anchors'})
                cursor += len(value)
                continue
            start,end = ((override['source_in'],override['source_out']) if override else
                         (times[min(matched)][0],times[max(matched)][1]))
            a = max(clip['timeline_in'],clip['timeline_in']+start-clip['source_in']-0.04)
            b = min(clip['timeline_out'],clip['timeline_in']+end-clip['source_in']+0.12)
            a,b = round(a*fps)/fps,round(b*fps)/fps
            a,b = max(a,clip['timeline_in']),min(b,clip['timeline_out'])
            if b-a < 0.4:
                reviews.append({'section':subtitle['section'],'text':text,'coverage':coverage,'reason':'caption_too_short'})
            section_captions.append({'section':subtitle['section'],'speaker':subtitle['speaker'],'text':clean_caption_text(text),
                'timeline_in':round(a,4),'timeline_out':round(b,4),'word_coverage':round(coverage,3),
                'timing_method':'reviewed_asr_phrase' if override else 'retained_asr_words','source':clip['source'],
                'anchor_source_in':round(start,4),'anchor_source_out':round(end,4)})
            cursor += len(value)
        for prev,nxt in zip(section_captions,section_captions[1:]):
            if prev['timeline_out'] > nxt['timeline_in']:
                boundary = round((prev['timeline_out']+nxt['timeline_in'])/2*fps)/fps
                prev['timeline_out'] = boundary
                nxt['timeline_in'] = boundary
        captions.extend(section_captions)
    # Preserve exactly the approved subtitle wording, ignoring display spaces.
    if canonical(''.join(c['text'] for c in captions)) != canonical(''.join(s['text'] for s in plan['tracks']['subtitles'])):
        reviews.append({'reason':'caption_text_incomplete'})
    return captions,reviews


def audit(work):
    plan = cp.load_json(work/'edit_plan.json')
    plan.setdefault('b_roll_metrics',{}).update(timeline_coverage_metrics(
        plan['tracks'].get('b_roll',[]),plan['timeline']['duration_seconds'],1.5))
    transcript = cp.load_json(work/'transcript.json')
    overrides=cp.load_json(work/'caption_anchor_overrides.json')['items'] if (work/'caption_anchor_overrides.json').exists() else []
    captions, reviews = build_caption_segments(plan,transcript,overrides)
    chatter = source_chatter_issues(plan,transcript)
    cp.write_json(work/'editorial_quality_report.json',{
        'source_edit_plan_sha256':cp.file_sha256(work/'edit_plan.json'),
        'chatter_issues':chatter,'caption_review':reviews,'caption_count':len(captions),
        'ready':not chatter and not reviews})
    if chatter or reviews:
        raise ValueError('Final audio/caption checks require review; see editorial_quality_report.json')
    plan['caption_segments'] = captions
    plan['quality_checks'] = {'source_chatter_clear':True,'caption_timing':'retained_asr_words',
                              'caption_source_signature':caption_plan_signature(plan),
                              'transcript_sha256':cp.file_sha256(work/'transcript.json')}
    cp.write_json(work/'edit_plan.json',plan)
    return plan


def first_visible_aroll(plan, speaker, min_seconds=2.8, excluded_intervals=()):
    from chart_layout import visible_spans
    clips = [c for c in plan['tracks']['a_roll'] if c['speaker']==speaker]
    for clip in clips:
        for span in visible_spans(plan,clip['timeline_in'],clip['timeline_out']):
            if span['source'] != clip['source']:
                continue
            windows = [(span['start'], span['end'])]
            for blocked_start, blocked_end in excluded_intervals:
                remaining = []
                for start, end in windows:
                    if blocked_end <= start or blocked_start >= end:
                        remaining.append((start, end))
                    else:
                        if start < blocked_start:
                            remaining.append((start, blocked_start))
                        if blocked_end < end:
                            remaining.append((blocked_end, end))
                windows = remaining
            for start, end in windows:
                if end-start >= min_seconds:
                    return start, min(start+4, end)
    return None


def attach_signposts(work, replace_existing=False):
    """Render reviewed topic/name cards as transparent PNG layers."""
    config = cp.load_json(work/'presentation_notes.json')
    plan = cp.load_json(work/'edit_plan_with_charts.json')
    if config['source_edit_plan_sha256'] != cp.file_sha256(work/'edit_plan.json'):
        raise ValueError('Presentation facts were reviewed for a different edit')
    graphics = plan['tracks']['graphics']
    existing_templates={'editorial_signpost','speaker_label'}
    if any(g.get('template') in existing_templates for g in graphics):
        if not replace_existing:
            raise ValueError('Editorial signposts already attached')
        plan['tracks']['graphics']=[g for g in graphics if g.get('template') not in existing_templates]
        graphics=plan['tracks']['graphics']
    directory = work/'presentation'
    directory.mkdir(exist_ok=True)
    width = int(plan['timeline'].get('width', 1920))
    height = int(plan['timeline'].get('height', 1080))
    bold = ImageFont.truetype(str(cp._font_path(True)), 48)
    regular = ImageFont.truetype(str(cp._font_path(False)), 29)
    identities = speaker_identities_from_v0(work)
    speaker_index=0
    for i,item in enumerate(config['cards']):
        if item['status']!='approved' or not item.get('evidence'):
            raise ValueError('Every signpost requires reviewed evidence')
        start,end = item['timeline_in'],item['timeline_out']
        if not 0 <= start < end <= plan['timeline']['duration_seconds']:
            raise ValueError('Signpost outside timeline')
        kind = str(item.get('kind') or 'topic')
        if kind != 'speaker':
            continue
        if kind == 'speaker':
            speaker_index+=1
            image = render_speaker_label({**item,'label_index':speaker_index},identities,width,height)
        target = directory/f'card_{i+1:02d}.png'
        image.save(target)
        duration=end-start
        template = 'speaker_label'
        graphics.append({'chart_id':f'signpost_{i+1}','template':template,'z_group':40,
                         'layer_key':'speaker_label','source':str(target.resolve()),
                         'timeline_in':start,'timeline_out':end,'status':'approved',
                         'title':item['title'],'keyframes':speaker_label_keys(duration)})
    cp.write_json(work/'edit_plan_with_charts.json',plan)
    return plan


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['audit','signposts','label-previews'])
    parser.add_argument('--work',type=Path,required=True)
    parser.add_argument('--replace-existing',action='store_true',help='replace only existing topic/speaker signposts')
    parser.add_argument('--output-dir',type=Path)
    args=parser.parse_args()
    if args.action=='audit':
        audit(args.work.resolve())
    elif args.action=='signposts':
        attach_signposts(args.work.resolve(),args.replace_existing)
    else:
        render_label_previews(
            args.work.resolve(),
            (args.output_dir or (args.work/'person_label_previews')).resolve(),
        )
