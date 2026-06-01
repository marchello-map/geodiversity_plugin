# geodiversity_plugin
An open-source QGIS tool for geodiversity assessment.

In case of any question, write to: pal.marton@inf.elte.hu

Update 1 June 2026: Geodiversity Calculator v2.1.2 is published

  - compatible with QGIS 4.0 (Qt6 / PyQt6); for QGIS 3.x, use v2.1
  - hydrography works without SAGA — Strahler stream order is now computed inside the plugin, so no extra packages need to be installed
  - hydrography subindex corrected: a cell with a lake scores 3, otherwise it gets round(Strahler ÷ 2) capped at 3 (not the sum of the two)
  - clear on-screen feedback: a message confirms completion, and any skipped or failed subindices are reported instead of failing silently
  - code cleaned and de-duplicated

Update 28 January 2026: Geodiversity Calculator v2.1 is published
  - no more experimental
  - geology and pedology assessments work without rasterization
  - intermediate layers can be left "silent": users can select whether they want to add them or just the final layer to QGIS

Update 23 January 2026: Geodiversity Calculator v2.0 is published
  - new design
  - smoother run
  - built-in error handling
  - the normalization is built in as an option (if the user ticks it, normalized fields will be additionally created) - hydro is normalized after summing lakes+Strahler
  - automatic styling and Natural Breaks data classification is set (Reds scale)
  - grid unit shapes can be selected: rectangle, diamond, hexagon
  - automatic grid spacing suggestion is corrected and other categories are inputted
  - the code has been cleaned

Update: 28 April 2025: Geodiversity Calculator v1.01 is published
  - This version works with SAGA NextGen.

Update 6 July 2022: Geodiversity Calculator v1.00 is published
  - You can change what subindeces to produce
  - Placeholder texts are written inside line edits to give clearer instructions
  - A reset button is added to clear all line edits at one click

