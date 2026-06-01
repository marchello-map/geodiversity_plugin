# -*- coding: utf-8 -*-
"""
Geodiversity Calculator v2.1.1 Dialog – QGIS 4.0 compatible port

QGIS 4.0 changes:
  - exec_() → exec()  (Qt6 removes the deprecated underscore variant)
  - The uic.loadUiType import path is unchanged (qgis.PyQt shim still works).
"""
import os
from qgis.PyQt import uic
from qgis.PyQt.QtWidgets import QDialog

FORM_CLASS, _ = uic.loadUiType(os.path.join(
    os.path.dirname(__file__), 'geodiversity_calculator_dialog_base.ui'))


class GeodiversityCalculatorDialog(QDialog, FORM_CLASS):
    def __init__(self, parent=None):
        super(GeodiversityCalculatorDialog, self).__init__(parent)
        self.setupUi(self)
        # Ensure OK/Cancel buttons work reliably
        try:
            self.button_box.accepted.connect(self.accept)
            self.button_box.rejected.connect(self.reject)
        except Exception:
            pass

    # Qt6 / PyQt6: exec_() is removed; exec() is the standard name.
    # PyQt5 supports both, so overriding here is harmless on QGIS 3.
    def exec(self):
        return super().exec()
