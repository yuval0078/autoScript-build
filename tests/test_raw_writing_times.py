import csv
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from analyzer_refactored import PenDataPlayer, raw_writing_times_ms, write_analysis_csv


class RawWritingTimeTests(unittest.TestCase):
    def events(self):
        return [dict(type=kind, absolute_time=t) for kind, t in [
            ('move', 9), ('press', 10), ('move', 10.1), ('release', 10.2),
            ('move', 10.4), ('press', 10.7), ('release', 11), ('move', 12)]]

    def test_contact_air_and_elapsed_exclude_hover(self):
        for actual, expected in zip(raw_writing_times_ms(self.events()), (1000, 500, 500)):
            self.assertAlmostEqual(actual, expected)

    def test_empty_single_and_incomplete_strokes(self):
        self.assertEqual(raw_writing_times_ms([]), (0, 0, 0))
        self.assertEqual(raw_writing_times_ms([dict(type='move', absolute_time=1)]), (0, 0, 0))
        self.assertEqual(raw_writing_times_ms([dict(type='press', absolute_time=1), dict(type='release', absolute_time=2)]), (1000, 0, 1000))
        self.assertEqual(raw_writing_times_ms([dict(type='press', absolute_time=1), dict(type='move', absolute_time=1.5)]), (500, 0, 500))

    def test_both_exports_ignore_analyzer_edits(self):
        word = dict(word='ab', pen_events=self.events())
        participant = SimpleNamespace(words=[word], participant_number=1, timestamp='20261020_120000',
            experiment_name='test', experiment_id='exp', block_name='block', block_id='block',
            block_index=1, block_count=1, session_id='session', age=20, gender='')
        names = ['Total writing time', 'Total air time', 'Total Stroke time']
        with tempfile.TemporaryDirectory() as tmp:
            for edited in (False, True):
                if edited:
                    word.update(assigned_letters={'1': 'x', '5': 'y'}, stroke_slices=[2], audio_start_time=1)
                cloud = Path(tmp) / 'cloud.csv'
                write_analysis_csv([participant], [0], cloud)
                player = SimpleNamespace(participants=[participant], pen_data=[])
                with patch('analyzer_refactored.QMessageBox') as message, patch('analyzer_refactored.QFileDialog.getSaveFileName', return_value=(str(Path(tmp) / 'manual.csv'), '')):
                    message.question.return_value = message.No
                    PenDataPlayer.export_to_csv(player)
                    message.critical.assert_not_called()
                for path in (cloud, Path(tmp) / '1_20261020' / 'manual.csv'):
                    with path.open(encoding='utf-8-sig', newline='') as handle:
                        reader = csv.DictReader(handle)
                        row = next(reader)
                        self.assertNotIn(None, row)
                        self.assertEqual([float(row[n]) for n in names], [1000, 500, 500])
