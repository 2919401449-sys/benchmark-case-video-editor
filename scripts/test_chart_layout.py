import unittest
from PIL import Image
import chart_layout as layout
import chart_motion as motion
from export_jianying import allocate_graphic_lanes, graphic_z_group


class ChartLayoutTests(unittest.TestCase):
    def review(self, *boxes):
        return {'samples': [{'protected_boxes': list(boxes)}]}

    def test_subject_right_chooses_left_without_intersection(self):
        box = [710,175,1610,1080]
        result = layout.choose_layout({'template':'flow'}, (100,75,954,758), self.review(box))
        self.assertEqual(result['mode'],'left')
        self.assertFalse(result['blur'])
        self.assertFalse(layout.intersects(result['box'],box))

    def test_subject_left_chooses_right(self):
        result = layout.choose_layout({'template':'list'}, (100,75,954,610), self.review([0,50,850,1080]))
        self.assertEqual(result['mode'],'right')

    def test_checks_all_shots_not_only_opening_frame(self):
        result = layout.choose_layout({'template':'stat'}, (100,100,934,889),
            {'samples':[{'protected_boxes':[[900,100,1920,1080]]},
                        {'protected_boxes':[[0,100,900,1080]]}]})
        self.assertEqual(result['mode'],'center')
        self.assertTrue(result['blur'])

    def test_comparison_always_fullscreen(self):
        self.assertEqual(layout.choose_layout({'template':'comparison'},None,self.review())['mode'],'fullscreen')

    def test_visible_shots_use_broll_over_aroll(self):
        plan = {'tracks':{'a_roll':[dict(source='a',source_in=20,source_out=30,timeline_in=0,timeline_out=10)],
                          'b_roll':[dict(source='b',source_in=40,source_out=43,timeline_in=3,timeline_out=6)]}}
        spans = layout.visible_spans(plan,1,8)
        self.assertEqual([s['source'] for s in spans],['a','b','a'])
        self.assertEqual([s['source_in'] for s in spans],[21,40,26])
        self.assertEqual(sum(s['end']-s['start'] for s in spans),7)

    def test_transform_preserves_transparency(self):
        image = Image.new('RGBA',(1920,1080))
        image.paste((255,255,255,255),(100,100,200,200))
        changed = layout.transform_layer(image,(100,100,200,200),{'mode':'left','box':[32,110,82,160]})
        self.assertEqual(changed.getbbox(),(32,110,82,160))

    def test_fullscreen_preserves_keys_and_safe_area(self):
        chart = {'template':'comparison','title':'方案周期','columns':['使用前','使用后'],
                 'rows':[{'label':'所需时间','before':'7天','after':'5天'}],'layout':{'mode':'fullscreen'}}
        base,layers = motion.layers_for_chart(chart,{'visual_theme':'editorial'})
        self.assertEqual([k for k,_ in layers],['row_1_before','row_1_after'])
        self.assertLess(base.getpixel((960,1000))[3],150)
        self.assertGreater(base.getpixel((300,600))[3],240)
        for _,im in layers:
            bbox = im.getbbox()
            self.assertGreater(bbox[1],100)
            self.assertLess(bbox[3],935)

    def test_z_groups_cannot_mix_even_when_nonoverlapping(self):
        def clip(template, start, end, **extra):
            return dict(template=template,timeline_in=start,timeline_out=end,**extra)
        clips = [clip('series_branding',0,100),clip('stat',10,15,layer_key='base'),
                 clip('background_blur',10,15),clip('stat',11,15,z_group=20),
                 clip('stat',12,15,z_group=30),clip('series_branding',0,100)]
        lanes = allocate_graphic_lanes(clips)
        self.assertEqual([graphic_z_group(l[0]) for l in lanes],[0,10,20,30,100,100])
        self.assertEqual(sum(map(len,lanes)),len(clips))


if __name__ == '__main__':
    unittest.main()
