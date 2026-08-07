from __future__ import annotations

import os
import sys
import types
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

if sys.platform != "win32":
    sys.modules.setdefault("winsound", types.SimpleNamespace(Beep=lambda *_args: None))

from PyQt5.QtCore import QPoint
from PyQt5.QtGui import QTabletEvent
from PyQt5.QtWidgets import QApplication

from tablet_experiment import CalibrationCanvas


class _FakeTabletEvent:
    def __init__(self, event_type, *, x=100, y=100, pressure=0.5):
        self._event_type = event_type
        self._position = QPoint(x, y)
        self._pressure = pressure
        self.accepted = False

    def type(self):
        return self._event_type

    def globalPos(self):
        return self._position

    def pressure(self):
        return self._pressure

    def accept(self):
        self.accepted = True


class CalibrationInputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.canvas = CalibrationCanvas()

    def tearDown(self):
        self.canvas.close()

    def test_pressured_moves_without_new_press_do_not_record_phantom_corner(self):
        with patch("tablet_experiment.time.time", side_effect=[10.0, 10.0, 10.7]):
            self.canvas.tabletEvent(
                _FakeTabletEvent(QTabletEvent.TabletMove, pressure=0.6)
            )
            self.canvas.tabletEvent(
                _FakeTabletEvent(QTabletEvent.TabletMove, pressure=0.6)
            )

        self.assertEqual(self.canvas.current_step, 0)
        self.assertEqual(self.canvas.calibration_points, [])
        self.assertIsNone(self.canvas.touch_start_time)

    def test_new_press_followed_by_held_move_records_one_corner(self):
        with patch("tablet_experiment.time.time", side_effect=[20.0, 20.6]):
            self.canvas.tabletEvent(
                _FakeTabletEvent(QTabletEvent.TabletPress, x=120, y=140)
            )
            self.canvas.tabletEvent(
                _FakeTabletEvent(QTabletEvent.TabletMove, x=120, y=140)
            )

        self.assertEqual(self.canvas.current_step, 1)
        self.assertEqual(self.canvas.calibration_points, [(120, 140)])

    def test_zero_pressure_move_cancels_active_press(self):
        with patch("tablet_experiment.time.time", return_value=30.0):
            self.canvas.tabletEvent(_FakeTabletEvent(QTabletEvent.TabletPress))

        self.canvas.tabletEvent(
            _FakeTabletEvent(QTabletEvent.TabletMove, pressure=0.0)
        )

        self.assertFalse(self.canvas.tablet_press_active)
        self.assertIsNone(self.canvas.touch_start_time)


if __name__ == "__main__":
    unittest.main()
