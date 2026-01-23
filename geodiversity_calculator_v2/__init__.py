# -*- coding: utf-8 -*-
"""Geodiversity Calculator v2.0"""

def classFactory(iface):
    from .geodiversity_calculator import GeodiversityCalculator
    return GeodiversityCalculator(iface)
