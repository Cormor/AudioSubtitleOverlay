import unittest
from subtitle_layout import (reading_window_height, paired_window_height, scrollbar_geometry,
                             EntryView, ParagraphLedger, StyleMetrics, build_lines, reading_positions)
from display_module import SubtitleHistory

class FollowupTests(unittest.TestCase):
    def test_one_row_minimum_with_unequal_fonts(self):
        for source, translated in ((24,24),(41.5,26),(20,65)):
            paired=reading_window_height(0,source,translated,True)
            single=reading_window_height(0,source,translated,False)
            self.assertGreaterEqual(paired,98+source+translated)
            self.assertLess(paired,99+source+translated)
            self.assertGreaterEqual(single,68+source)
            self.assertLess(single,69+source)

    def test_collapse_preserves_source_capacity_and_preferred_height(self):
        for preferred in (180,279,500):
            single=reading_window_height(preferred,32,40,False)
            self.assertLess(single,preferred)
            self.assertAlmostEqual((single-68)/32,(preferred-98)/72,delta=1/32)
            for _ in range(100):
                self.assertEqual(reading_window_height(preferred,32,40,False),single)
                self.assertEqual(reading_window_height(preferred,32,40,True),preferred)

    def test_fractional_row_minimum_restores_exact_bilingual_minimum(self):
        preferred=paired_window_height(114,45.5,39)
        self.assertEqual(reading_window_height(preferred,45.5,39,True),183)

    def test_source_only_resize_roundtrip(self):
        for height in (100,150,279):
            paired=paired_window_height(height,32,40)
            self.assertEqual(reading_window_height(paired,32,40,False),height)

    def test_blank_translation_has_no_label_or_lane(self):
        styles={k:StyleMetrics(k,24,20) for k in ('source','translation')}
        lines,_=build_lines([EntryView('source','')],styles,300,1,True,ParagraphLedger(),lambda t:len(t)*10)
        result=reading_positions(lines,10,100,1,False,{k:24 for k in styles})
        self.assertEqual(set(result['reading_lanes']),{'source'})
        self.assertEqual(set(result['reading_labels']),{'source'})

    def test_one_complete_row_per_language(self):
        styles={'source':StyleMetrics('source',31,20),'translation':StyleMetrics('translation',47,30)}
        lines,_=build_lines([EntryView('source','译文')],styles,300,1,True,ParagraphLedger(),lambda t:len(t)*10)
        height=reading_window_height(0,31,47,True)
        result=reading_positions(lines,10,height-40,1,True,{k:v.line_height for k,v in styles.items()})
        self.assertEqual(len(result['reading_positions']),2)

    def test_scrollbar_top_bottom_and_outside_clamping(self):
        for height in (100,176,500):
            for offset in (-100,0,500,1000,1200):
                bar=scrollbar_geometry(799,height,1200,1000,offset)
                self.assertEqual(bar['right']-bar['left'],20)
                self.assertGreaterEqual(bar['thumb_top'],bar['top'])
                self.assertLessEqual(bar['thumb_top']+bar['thumb_height'],bar['bottom']+.001)

    def test_stable_keys_reuse_wrap_cache_after_history_prune(self):
        ledger=ParagraphLedger();calls=[]
        def measure(text):calls.append(text);return len(text)*10
        styles={k:StyleMetrics(k,24,20) for k in ('source','translation')}
        entries=[EntryView('source '+str(i),'译文 '+str(i),key=i) for i in range(64)]
        build_lines(entries,styles,300,1,True,ledger,measure)
        calls.clear()
        build_lines(entries[1:],styles,300,1,True,ledger,measure)
        self.assertFalse(calls)

    def test_history_retains_exact_latest_64_and_rejects_evicted_translation(self):
        history=SubtitleHistory()
        for seq in range(1,301):history.update_source(seq,seq,'source '+str(seq),'en')
        self.assertEqual(history.max_blocks,64)
        self.assertEqual(list(history.blocks),list(range(237,301)))
        history.update_translation(236,'old')
        self.assertFalse(history._pending_translations)
        self.assertEqual(history.blocks[300].source,'source 300')
