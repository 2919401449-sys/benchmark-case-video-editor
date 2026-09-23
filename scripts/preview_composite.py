"""Local compositing QA, not a substitute for Jianying playback validation."""
import argparse
import math
import subprocess
from pathlib import Path
from PIL import Image,ImageDraw,ImageFont
import chart_pipeline as cp
from chart_layout import frame_at,visible_spans
from export_jianying import graphic_z_group
from pipeline import find_ffmpeg


def preview(work):
    plan=cp.load_json(work/'edit_plan_with_charts.json')
    charts=cp.load_json(work/'chart_plan.json')['charts']
    directory=work/'qa'
    directory.mkdir(exist_ok=True)
    shots=[('opening',1.6)]+[(c['id'],c['timeline_out']-.4) for c in charts]
    for card in cp.load_json(work/'presentation_notes.json')['cards'][2:]:
        shots.append((f"speaker_{len(shots)}",card['timeline_in']+1.5))
    outputs=[]
    for name,time in shots:
        raw=directory/f'{name}_background.png'
        frame_at(visible_spans(plan,time,time+.001)[0],time,raw)
        canvas=Image.open(raw).convert('RGBA')
        for clip in sorted(plan['tracks']['graphics'],key=graphic_z_group):
            if not clip['timeline_in']<=time<clip['timeline_out']:
                continue
            source=Path(clip['source'])
            if source.suffix.lower() not in ('.png','.jpg','.jpeg'):
                sampled=directory/f'{name}_{clip["layer_key"]}.png'
                subprocess.run([find_ffmpeg(),'-v','error','-y','-ss',str(time-clip['timeline_in']+clip.get('source_in',0)),
                                '-i',str(source),'-frames:v','1',str(sampled)],check=True,capture_output=True)
                source=sampled
            overlay=Image.open(source).convert('RGBA')
            keys=sorted((k['time'],k['value']) for k in clip.get('keyframes',[]) if k['property']=='alpha')
            alpha,offset=1.0,time-clip['timeline_in']
            for (a,x),(b,y) in zip(keys,keys[1:]):
                if a<=offset<=b:
                    alpha=x+(y-x)*(offset-a)/(b-a)
                    break
            if alpha<1:
                overlay.putalpha(overlay.getchannel('A').point(lambda a:round(a*alpha)))
            canvas.alpha_composite(overlay)
        font=ImageFont.truetype('C:/Windows/Fonts/msyh.ttc',36)
        for caption in plan.get('caption_segments',[]):
            if caption['timeline_in']<=time<caption['timeline_out']:
                ImageDraw.Draw(canvas).text((960,1015),caption['text'],anchor='mm',font=font,
                                           fill='white',stroke_width=2,stroke_fill='black')
        target=directory/f'{name}_composite.jpg'
        canvas.convert('RGB').save(target,quality=93)
        outputs.append((target,time))
    sheet=Image.new('RGB',(1280,390*math.ceil(len(outputs)/2)),'#182336')
    draw=ImageDraw.Draw(sheet)
    for i,(path,time) in enumerate(outputs):
        x,y=i%2*640,i//2*390
        with Image.open(path) as image:
            sheet.paste(image.resize((640,360)),(x,y))
        draw.text((x+8,y+366),f'{path.stem} {time:.2f}s',fill='white')
    sheet.save(directory/'composite_contact_sheet.jpg',quality=93)
    print(f'{len(outputs)} composites; preview caption font approximate; draft properties checked separately')


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--work',type=Path,required=True)
    preview(parser.parse_args().work.resolve())
