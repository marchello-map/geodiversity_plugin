Update 1 June 2026: Geodiversity Calculator v2.1.1 is published

    QGIS 4.0 changes applied (PyQt6 / Qt 6):
       - QVariant removed; QgsField uses Python-native types (int, float, str) via QMetaType.Type constants.
       - qgis.PyQt  → PyQt6 (with qgis.PyQt shim used where available for safety)
       - QDialogButtonBox.Reset → QDialogButtonBox.StandardButton.Reset  (Qt6 enums)
       - exec_()  → exec()  (Qt6 deprecates the underscore variant)
       - QgsGraduatedSymbolRenderer.Jenks → QgsClassificationJenks renderer helper
       - grass7:r.geomorphon  → grass:r.geomorphon  (GRASS provider rename)
       - qgis:zonalstatisticsfb → native:zonalstatisticsfb  (algorithm id change)
       - feature.fieldNameIndex() → layer.fields().lookupField()  (API clean-up)

Update 28 January 2026: Geodiversity Calculator v2.1 is published

    geological and pedological subindices re-determined without involving raster conversion
    intermediate layers can be chosen to be unopened in QGIS

Update 23 January 2026: Geodiversity Calculator v2.0 is published

    new design
    smoother run
    built-in error handling
    the normalization is built in as an option (if the user ticks it, normalized fields will be additionally created) - hydro is normalized after summing lakes+Strahler
    automatic styling and Natural Breaks data classification is set (Reds scale)
    grid unit shapes can be selected: rectangle, diamond, hexagon
    automatic grid spacing suggestion is corrected and other categories are inputted
    the code has been cleaned
