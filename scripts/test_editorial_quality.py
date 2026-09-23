import unittest
from pathlib import Path
from editorial_quality import (trim_opening,revise_source_edges,source_chatter_issues,
    build_caption_segments,first_visible_aroll,speaker_label_keys,render_speaker_label)
from pipeline import (is_directing_chatter,transcript_sentences_for_sources,
    close_residual_broll_flashes,detect_project_case_type,load_common_ai_broll,
    broll_source_use_limit,broll_origin_priority_bonus,ensure_common_ai_broll_quota)
from export_jianying import split_caption_text,caption_plan_signature


class EditorialQualityTests(unittest.TestCase):
    def test_preparation_not_business_speech(self):
        self.assertTrue(is_directing_chatter('嗯 好 我开始了'))
        self.assertFalse(is_directing_chatter('我开始使用AI以后效率提高了'))

    def test_removing_chatter_also_separates_audio_ranges(self):
        transcript={'files':[{'source':'a','segments':[
            {'start':0,'end':2,'text':'我们先了解客户需求','words':[]},
            {'start':2,'end':3,'text':'等一下','words':[]},
            {'start':4,'end':6,'text':'然后提供不同方案','words':[]}]}]}
        sentences=transcript_sentences_for_sources(transcript,{'a'},target_chars=100)
        self.assertEqual([(s['source_in'],s['source_out']) for s in sentences],[(0,2),(4,6)])

    def plan(self):
        return {'timeline':{'duration_seconds':10,'fps':25},'tracks':{
            'a_roll':[dict(section=1,source='a',speaker='甲',source_in=0,source_out=5,timeline_in=0,timeline_out=5),
                      dict(section=2,source='b',speaker='乙',source_in=10,source_out=15,timeline_in=5,timeline_out=10)],
            'b_roll':[dict(source='c',source_in=20,source_out=23,timeline_in=4,timeline_out=7)],
            'subtitles':[dict(section=1,speaker='甲',text='大家好',timeline_in=0,timeline_out=5),
                         dict(section=2,speaker='乙',text='客户非常认可',timeline_in=5,timeline_out=10)]}}

    def test_opening_ripple_keeps_sources_and_sync(self):
        before=self.plan()
        after,delta=trim_opening(before,2.64,25)
        self.assertEqual(delta,2.64)
        self.assertEqual(after['tracks']['a_roll'][0]['source_in'],2.64)
        self.assertEqual(after['tracks']['b_roll'][0]['timeline_in'],1.36)
        self.assertEqual(after['tracks']['b_roll'][0]['source_in'],20)
        self.assertEqual(before['timeline']['duration_seconds'],10)

    def test_edge_extension_keeps_business_duration(self):
        before=self.plan()
        after=revise_source_edges(before,{2:{'source_in':9}})
        self.assertEqual(after['timeline']['duration_seconds'],11)
        self.assertEqual(after['tracks']['a_roll'][1]['source_in'],9)
        self.assertEqual(after['tracks']['subtitles'][1]['timeline_out'],11)

    def test_no_one_character_caption_fragment(self):
        text='甲'*19
        parts=split_caption_text(text,18)
        self.assertEqual(''.join(parts),text)
        self.assertTrue(all(3<=len(p)<=18 for p in parts))

    def test_caption_uses_spoken_onset_not_paragraph_start(self):
        plan=self.plan()
        transcript={'files':[{'source':'a','segments':[{'text':'大家好','start':2,'end':3,
          'words':[{'word':'大家好','start':2,'end':3}]}]},
         {'source':'b','segments':[{'text':'客户非常认可','start':11,'end':13,
          'words':[{'word':'客户非常认可','start':11,'end':13}]}]}]}
        captions,issues=build_caption_segments(plan,transcript)
        self.assertFalse(issues)
        self.assertAlmostEqual(captions[0]['timeline_in'],1.96)
        self.assertAlmostEqual(captions[1]['timeline_in'],5.96)

    def test_signature_invalidates_stale_captions(self):
        plan=self.plan()
        signature=caption_plan_signature(plan)
        plan['tracks']['subtitles'][0]['text']='其他内容'
        self.assertNotEqual(signature,caption_plan_signature(plan))

    def test_bridge_does_not_reopen_next_join(self):
        clips=[dict(timeline_in=a,timeline_out=b,source_in=1,source_out=1+b-a)
               for a,b in [(0,4),(4.5,8),(8,11),(15,18)]]
        self.assertEqual(close_residual_broll_flashes(clips),1)
        self.assertEqual(clips[1]['timeline_in'],4)
        self.assertEqual(clips[2]['timeline_in'],7.5)
        self.assertEqual(clips[2]['timeline_out'],10.5)
        self.assertEqual(clips[2]['source_in'],1)

    def test_speaker_card_waits_for_uncovered_person(self):
        plan=self.plan()
        plan['tracks']['b_roll']=[dict(source='c',source_in=0,source_out=1,timeline_in=0,timeline_out=1)]
        self.assertEqual(first_visible_aroll(plan,'甲',excluded_intervals=[(0.5,2)]),(2,5))
        self.assertIsNone(first_visible_aroll(plan,'甲',excluded_intervals=[(0.5,3)]))

    def test_ai_case_detection_and_common_library(self):
        self.assertEqual(detect_project_case_type(Path('客户_AI智能设计平台')), 'ai')
        self.assertEqual(detect_project_case_type(Path('客户_生产对接')), 'production')
        clips=load_common_ai_broll()
        self.assertEqual(len(clips),7)
        self.assertTrue(all(item['path'].is_file() and item['tags'] for item in clips))
        self.assertTrue(all(broll_source_use_limit(item,2)==1 for item in clips))
        self.assertEqual(broll_source_use_limit({'origin':'project'},2),2)
        self.assertGreater(
            broll_origin_priority_bonus({'origin':'skill_common_ai'}),
            broll_origin_priority_bonus({'origin':'project'}))

    def test_speaker_label_slides_in_and_only_fades_out(self):
        keys=speaker_label_keys(3.8)
        x=[item for item in keys if item['property']=='position_x']
        alpha=[item for item in keys if item['property']=='alpha']
        self.assertLess(x[0]['value'],0)
        self.assertEqual(x[-1]['value'],0)
        self.assertEqual(alpha[0]['value'],1)
        self.assertEqual(alpha[-1]['value'],0)
        self.assertFalse(any(item['property']=='position_y' for item in keys))

    def test_speaker_label_renders_lower_left(self):
        image=render_speaker_label(
            {'name':'周余强','role':'副总裁','organization':'九鼎装饰','label_index':1}, {}, 1920, 1080)
        alpha=image.getchannel('A')
        box=alpha.getbbox()
        self.assertIsNotNone(box)
        self.assertLess(box[0],100)
        self.assertGreater(box[1],650)
        self.assertLess(box[3],980)
        self.assertLess(box[1],800)
        self.assertLess(box[2]-box[0],900)
        # Background glass remains translucent; text can still be fully opaque.
        alphas=[value for value in alpha.crop(box).getdata() if value]
        self.assertTrue(any(90 <= value <= 190 for value in alphas))

    def test_speaker_label_uses_distinct_geometric_variants(self):
        first=render_speaker_label(
            {'name':'周余强','role':'副总裁','organization':'九鼎装饰','label_index':1}, {}, 1920, 1080)
        second=render_speaker_label(
            {'name':'包姐','role':'销售','organization':'九鼎装饰','label_index':2}, {}, 1920, 1080)
        self.assertNotEqual(first.crop((64,770,210,960)).tobytes(),
                            second.crop((64,770,210,960)).tobytes())

    def test_speaker_label_keeps_company_text_clear_and_has_no_index(self):
        image=render_speaker_label(
            {'name':'周余强','role':'副总裁','organization':'九鼎装饰','label_index':1}, {}, 1920, 1080)
        # The right edge used to contain an index/dot. It now contains only the
        # glass panel above the English role line.
        sample=image.crop((630,790,700,840))
        colors={pixel for pixel in sample.getdata()}
        self.assertLess(len(colors),8)

    def test_common_ai_quota_injects_four_unique_recordings(self):
        subtitles=[]
        broll=[]
        for index,text in enumerate(['导入户型图','选择设计风格','局部调整方案','生成全屋效果图','展示普通办公环境'],1):
            subtitles.append({'section':index,'text':text})
            broll.append({'section':index,'source':f'project_{index}.mp4','source_in':0,'source_out':2.5,
                          'timeline_in':index*3.0,'timeline_out':index*3.0+2.5,'origin':'project'})
        common=[]
        specs=[('导入.mp4',['floor_plan']),('风格.mp4',['ai_rendering']),
               ('调整.mp4',['designer_ai_rendering']),('全屋.mp4',['rendering_display'])]
        for name,tags in specs:
            common.append({'path':name,'duration_seconds':3.2,'origin':'skill_common_ai',
                           'description':name,'tags':tags})
        plan={'tracks':{'b_roll':broll,'subtitles':subtitles}}
        report=ensure_common_ai_broll_quota(plan,{'case_type':'ai','b_roll':common},3,5,4)
        selected=[x for x in plan['tracks']['b_roll'] if x.get('origin')=='skill_common_ai']
        self.assertEqual(report['used'],4)
        self.assertEqual(len(selected),4)
        self.assertEqual(len({x['source'] for x in selected}),4)
        self.assertTrue(all(x['max_source_uses']==1 for x in selected))


if __name__=='__main__':
    unittest.main()
