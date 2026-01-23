# -*- coding: utf-8 -*-
"""
Geodiversity Calculator v2.0 Dialog
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
