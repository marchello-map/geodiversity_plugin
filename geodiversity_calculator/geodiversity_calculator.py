# -*- coding: utf-8 -*-
"""
/***************************************************************************
 Geodiversity Calculator v2.1.2  –  QGIS 4.0 compatible
                                 A QGIS plugin
 Calculates spatial geodiversity indices (geology, pedology, geomorphology,
 hydrography, mineralogy, palaeontology) on a regular grid.
 Based on the methodology by Márton Pál.
                              -------------------
        begin                : 2026-01-26
        copyright            : (C) 2026 Márton Pál and Emmanuel Owusu-Acheampong
        email                : pal.marton@inf.elte.hu & emmaoacheamp@student.elte.hu

 QGIS 4.0 / Qt6 notes:
   - QgsField types use QMetaType (QVariant removed); a shim supports both.
   - Qt6 scoped enums (QDialogButtonBox.StandardButton.Reset), exec().
   - QgsClassificationJenks for graduated styling.
   - grass:r.geomorphon, native:zonalstatisticsfb.
   - Self-contained Strahler stream order (no SAGA / GRASS add-ons).

 Maintenance pass: removed unused imports/locals and de-duplicated the
 repeated subindex, file-picker and grid-spacing logic. No behaviour, UI, or
 output changes.
 ***************************************************************************/
"""

# ---------------------------------------------------------------------------
# Qt / QGIS imports  – prefer qgis.PyQt shim; it will re-export PyQt6 in QGIS 4
# ---------------------------------------------------------------------------
from qgis.PyQt.QtCore import QCoreApplication, QDateTime
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import (
    QAction, QFileDialog, QDialogButtonBox, QProgressBar, QMessageBox,
)

# QMetaType replaces QVariant for field-type constants in QGIS 4 / Qt 6.
# A small shim keeps the field-creation helpers working on both QGIS 4
# (PyQt6 / QMetaType) and, defensively, QGIS 3 (PyQt5 / QVariant).
try:
    from PyQt6.QtCore import QMetaType  # QGIS 4 + PyQt6

    def _make_int_field(name):
        return QgsField(name, QMetaType.Type.Int)

    def _make_float_field(name):
        return QgsField(name, QMetaType.Type.Double)

except ImportError:
    from qgis.PyQt.QtCore import QVariant  # noqa: F401

    def _make_int_field(name):
        return QgsField(name, QVariant.Int)

    def _make_float_field(name):
        return QgsField(name, QVariant.Double)

from qgis.core import (
    Qgis, QgsVectorLayer, QgsProject, QgsField,
    QgsRasterLayer, QgsMessageLog, QgsSpatialIndex,
    QgsExpression, QgsExpressionContext, QgsExpressionContextScope, edit,
    QgsStyle, QgsGraduatedSymbolRenderer, QgsClassificationJenks,
)
from .geodiversity_calculator_dialog import GeodiversityCalculatorDialog
import os
import processing
import math
import traceback


# Grid-spacing lookup tables: (upper_area_bound_km2, spacing_m).
# The first row whose bound is >= area wins; the final None bound is the
# catch-all for the largest datasets. These replace long if/elif chains.
#
# Autofill table — used to pre-fill the spacing fields when a boundary is
# chosen in the dialog.
_AUTOFILL_SPACING = [
    (2_500,     1000),
    (10_000,    2500),
    (50_000,    5000),
    (100_000,   7500),
    (1_000_000, 10000),
    (5_000_000, 20000),
    (None,      50000),
]

# Advisory table — used only to log a human-readable dataset-size hint at the
# start of a run (label, upper_area_bound_km2, suggested_spacing_m).
_ADVISORY_SPACING = [
    ("Very small",     5_000,     500),
    ("Small",          20_000,    1000),
    ("Small-moderate", 50_000,    2500),
    ("Moderate",       100_000,   5000),
    ("Medium",         1_000_000, 10000),
    ("Large",          5_000_000, 20000),
    ("HUGE",           None,      50000),
]


def _lookup_spacing(table, area):
    """Return the spacing for `area` from a (bound, spacing) lookup table."""
    for bound, spacing in table:
        if bound is None or area <= bound:
            return spacing
    return table[-1][1]


class GeodiversityCalculator:
    """Geodiversity calculator – QGIS 4.0 compatible."""

    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.actions = []
        self.first_start = True
        self.dlg = None
        # Per-run status tracking for end-of-run user feedback.
        # Each subindex maps to one of: 'ok', 'skipped', 'failed', 'absent'.
        self._status = {}
        self._status_notes = []

    def tr(self, message):
        return QCoreApplication.translate('GeodiversityCalculator', message)

    def log(self, message, level=Qgis.MessageLevel.Info):
        """Enhanced logging.

        QGIS 4 uses Qgis.MessageLevel enum; fall back to bare int for QGIS 3.
        """
        QgsMessageLog.logMessage(message, 'Geodiversity Calculator', level)

    # ------------------------------------------------------------------
    # Run-status tracking & user feedback
    # ------------------------------------------------------------------

    def _reset_status(self):
        """Clear status tracking at the start of each run."""
        self._status = {}
        self._status_notes = []

    def _set_status(self, component: str, state: str, note: str = ""):
        """Record the outcome of one analysis component.

        state is one of:
          'ok'      - component computed successfully
          'absent'  - optional input not provided (expected, not a problem)
          'skipped' - input provided but component could not run
          'failed'  - an unexpected error occurred
        """
        self._status[component] = state
        if note:
            self._status_notes.append(f"{component}: {note}")

    def _build_status_summary(self):
        """Return (overall_level, summary_text) describing the run.

        overall_level is a Qgis.MessageLevel; summary_text is multi-line.
        """
        labels = {
            'geology':       'Geology',
            'pedology':      'Pedology',
            'geomorphology': 'Geomorphology',
            'hydrography':   'Hydrography (Strahler + lakes)',
            'mineralogy':    'Mineralogy',
            'palaeontology': 'Palaeontology',
        }
        ok, skipped, failed = [], [], []
        for comp, label in labels.items():
            st = self._status.get(comp, 'absent')
            if st == 'ok':
                ok.append(label)
            elif st == 'skipped':
                skipped.append(label)
            elif st == 'failed':
                failed.append(label)
            # 'absent' is expected and not reported in the body

        lines = []
        if ok:
            lines.append("Computed: " + ", ".join(ok) + ".")
        if skipped:
            lines.append(
                "Skipped (input provided but could not be processed): "
                + ", ".join(skipped) + ".")
        if failed:
            lines.append("Failed unexpectedly: " + ", ".join(failed) + ".")

        # Choose an overall severity for the message bar.
        if failed:
            level = Qgis.MessageLevel.Critical
        elif skipped:
            level = Qgis.MessageLevel.Warning
        else:
            level = Qgis.MessageLevel.Success

        return level, "\n".join(lines) if lines else "No subindices computed."

    def _notify(self, title: str, text: str,
                level=Qgis.MessageLevel.Info, duration: int = 15):
        """Show a message in the QGIS message bar (always visible to users)."""
        try:
            self.iface.messageBar().pushMessage(
                title, text, level=level, duration=duration)
        except Exception:
            pass

    def _notify_dialog(self, title: str, text: str, critical: bool = False):
        """Show a modal dialog for outcomes the user must not miss."""
        try:
            if critical:
                QMessageBox.critical(self.dlg or self.iface.mainWindow(),
                                     title, text)
            else:
                QMessageBox.information(self.dlg or self.iface.mainWindow(),
                                        title, text)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Self-contained Strahler stream order (no external add-ons)
    # ------------------------------------------------------------------

    def _compute_strahler_raster(self, stream_rast_path, drainage_rast_path,
                                 out_path):
        """Compute a Strahler-order raster purely in Python.

        Inputs are the stream-segment raster and the D8 drainage-direction
        raster produced by the CORE module grass:r.watershed. This avoids the
        r.stream.order ADD-ON entirely, so nothing extra must be installed.

        Algorithm:
          1. Read both rasters into numpy arrays.
          2. For each stream cell, follow its D8 drainage direction to find the
             downstream cell; this defines a directed graph over stream cells.
          3. A cell's in-degree = number of stream cells draining INTO it.
          4. Strahler order is propagated from headwaters (in-degree 0, order 1)
             downstream: when cells of equal max order n meet, the downstream
             order becomes n+1; otherwise it stays at the incoming max.
          5. Write the per-cell order back out as a raster.

        Returns out_path on success, or raises on failure.
        """
        import numpy as np
        from osgeo import gdal

        # GRASS r.watershed D8 drainage codes (1..8, counter-clockwise from NE)
        # map to (drow, dcol) offsets. Negative values = flow leaves region;
        # 0 = depression. We take abs() so edge-outflow still routes.
        # GRASS direction convention:
        #   1=NE 2=N 3=NW 4=W 5=SW 6=S 7=SE 8=E
        dir_offsets = {
            1: (-1,  1),
            2: (-1,  0),
            3: (-1, -1),
            4: ( 0, -1),
            5: ( 1, -1),
            6: ( 1,  0),
            7: ( 1,  1),
            8: ( 0,  1),
        }

        ds_stream = gdal.Open(stream_rast_path)
        ds_dir    = gdal.Open(drainage_rast_path)
        if ds_stream is None or ds_dir is None:
            raise Exception("Could not open r.watershed output rasters.")

        stream = ds_stream.GetRasterBand(1).ReadAsArray()
        drain  = ds_dir.GetRasterBand(1).ReadAsArray()
        if stream is None or drain is None:
            raise Exception("Could not read raster bands for Strahler.")

        nrows, ncols = stream.shape

        # A cell is a stream cell where stream > 0 (NULL/<=0 are non-stream).
        is_stream = stream > 0

        # Downstream target for each stream cell, plus in-degree counts.
        # downstream[(r,c)] = (r2,c2) or None if it exits the grid/region.
        downstream = {}
        indeg = np.zeros((nrows, ncols), dtype=np.int32)

        rows, cols = np.where(is_stream)
        for r, c in zip(rows.tolist(), cols.tolist()):
            d = int(drain[r, c])
            d = abs(d)  # edge-outflow encoded as negative in GRASS
            off = dir_offsets.get(d)
            if not off:
                downstream[(r, c)] = None
                continue
            r2, c2 = r + off[0], c + off[1]
            if 0 <= r2 < nrows and 0 <= c2 < ncols and is_stream[r2, c2]:
                downstream[(r, c)] = (r2, c2)
                indeg[r2, c2] += 1
            else:
                downstream[(r, c)] = None

        # Topological propagation from headwaters (in-degree 0).
        # Each cell accumulates the Strahler orders arriving from upstream.
        order = np.zeros((nrows, ncols), dtype=np.int32)
        # incoming[(r,c)] = list of upstream orders seen so far
        incoming = {}
        remaining = indeg.copy()

        from collections import deque
        queue = deque()
        for r, c in zip(rows.tolist(), cols.tolist()):
            if indeg[r, c] == 0:
                order[r, c] = 1
                queue.append((r, c))

        while queue:
            r, c = queue.popleft()
            o = order[r, c]
            tgt = downstream.get((r, c))
            if tgt is None:
                continue
            lst = incoming.setdefault(tgt, [])
            lst.append(o)
            remaining[tgt] -= 1
            if remaining[tgt] == 0:
                # All upstream contributors known: apply Strahler rule.
                mx = max(lst)
                if lst.count(mx) >= 2:
                    order[tgt] = mx + 1
                else:
                    order[tgt] = mx
                queue.append(tgt)

        # Any stream cells not reached (e.g. inside a cycle) default to order 1.
        unresolved = is_stream & (order == 0)
        if unresolved.any():
            order[unresolved] = 1

        # Write the order raster (Int32, 0 = NoData for non-stream cells).
        driver = gdal.GetDriverByName('GTiff')
        out_ds = driver.Create(out_path, ncols, nrows, 1, gdal.GDT_Int32)
        out_ds.SetGeoTransform(ds_stream.GetGeoTransform())
        out_ds.SetProjection(ds_stream.GetProjection())
        out_band = out_ds.GetRasterBand(1)
        out_band.WriteArray(order)
        out_band.SetNoDataValue(0)
        out_band.FlushCache()
        out_ds = None
        ds_stream = None
        ds_dir = None
        return out_path

    # ------------------------------------------------------------------
    # Helper utilities
    # ------------------------------------------------------------------

    def _set_progress(self, progressbar, value: int):
        """Update progress bar and let the UI breathe."""
        try:
            if progressbar:
                progressbar.setValue(int(value))
                QCoreApplication.processEvents()
        except Exception:
            pass

    def _remove_field_if_exists(self, layer: QgsVectorLayer, field_name: str):
        """Physically remove a field from a layer datasource if present."""
        try:
            if layer is None or not layer.isValid():
                return
            idx = layer.fields().lookupField(field_name)
            if idx == -1:
                return
            layer.dataProvider().deleteAttributes([idx])
            layer.updateFields()
        except Exception:
            return

    def _encode_unique_values(self, layer: QgsVectorLayer,
                              src_field: str, out_field: str = "r_value"):
        """Add/overwrite an integer category field based on unique values in src_field.

        Returns a dict mapping original values -> integer codes (starting at 1).
        """
        if layer is None or not layer.isValid():
            raise Exception("Layer invalid for encoding unique values.")
        if not src_field:
            raise Exception("Source field not provided for encoding unique values.")
        if layer.fields().lookupField(out_field) == -1:
            layer.dataProvider().addAttributes([_make_int_field(out_field)])
            layer.updateFields()

        mapping = {}
        next_code = 1
        out_idx = layer.fields().lookupField(out_field)

        with edit(layer):
            for f in layer.getFeatures():
                key = f[src_field]
                if key not in mapping:
                    mapping[key] = next_code
                    next_code += 1
                f.setAttribute(out_idx, mapping[key])
                layer.updateFeature(f)
        return mapping

    def _vector_touch_variety(self, grid_path: str, poly_layer: QgsVectorLayer,
                              out_path: str, out_field: str,
                              value_field: str = "r_value"):
        """Compute distinct-category variety per grid cell using vector 'any-touch' logic.

        Variety = count of DISTINCT values (value_field) among polygon features whose
        geometries INTERSECT the grid cell (includes boundary touch).
        Writes a new grid layer to out_path with out_field populated.
        """
        if not grid_path or not os.path.exists(grid_path):
            raise Exception("Grid path not found for vector variety computation.")
        if poly_layer is None or not poly_layer.isValid():
            raise Exception("Polygon layer invalid for vector variety computation.")

        # Save a copy of the grid to the requested output path
        processing.run("native:savefeatures", {
            'INPUT': grid_path,
            'OUTPUT': out_path,
        })

        out_grid = QgsVectorLayer(out_path, os.path.basename(out_path), "ogr")
        if not out_grid.isValid():
            raise Exception("Failed to create output grid for vector variety computation.")

        prov = out_grid.dataProvider()
        if out_grid.fields().lookupField(out_field) == -1:
            prov.addAttributes([_make_int_field(out_field)])
            out_grid.updateFields()
        out_idx = out_grid.fields().lookupField(out_field)

        sidx = QgsSpatialIndex(poly_layer.getFeatures())

        with edit(out_grid):
            for cell in out_grid.getFeatures():
                g = cell.geometry()
                if g is None or g.isEmpty():
                    prov.changeAttributeValues({cell.id(): {out_idx: 0}})
                    continue
                cand_ids = sidx.intersects(g.boundingBox())
                vals = set()
                for fid in cand_ids:
                    poly_feat = poly_layer.getFeature(fid)
                    pg = poly_feat.geometry()
                    if pg is None or pg.isEmpty():
                        continue
                    if pg.intersects(g):
                        vals.add(poly_feat[value_field])
                prov.changeAttributeValues({cell.id(): {out_idx: int(len(vals))}})

        out_grid.updateExtents()
        return out_grid

    def _suggest_grid_spacing_from_boundary(self, boundary_path: str):
        """Auto-suggest grid spacing based on boundary extent area (km²)."""
        try:
            if hasattr(self, "_spacing_autofill") and not self._spacing_autofill:
                return
            boundary_path = (boundary_path or "").strip()
            if not boundary_path or not os.path.exists(boundary_path):
                return
            layer = QgsVectorLayer(boundary_path, "boundary", "ogr")
            if not layer.isValid():
                return
            area = layer.extent().width() * layer.extent().height() / 1_000_000.0
            if area <= 0:
                return
            suggested = _lookup_spacing(_AUTOFILL_SPACING, area)
            self.dlg.lineEdit_3.setText(str(suggested))
            self.dlg.lineEdit_4.setText(str(suggested))
        except Exception:
            return

    def _add_normalized_fields(self, grid):
        """Create normalized subindex fields (0-1) and their sum (N_sum).

        Robust to MISSING source fields: any optional subindex that was not
        produced (e.g. _stra_max when the Strahler step is skipped) is simply
        treated as 0 and never accessed by name, so no KeyError can occur.
        """
        field_names = {grid.fields().at(i).name()
                       for i in range(grid.fields().count())}

        def _safe_get(feat, name):
            """Return float value of feat[name], or 0.0 if absent/NULL/bad."""
            if name not in field_names:
                return 0.0
            try:
                v = feat[name]
            except (KeyError, IndexError):
                return 0.0
            try:
                return float(v) if v is not None else 0.0
            except Exception:
                return 0.0

        # Output normalized field -> source field
        src = {
            "N_geol":  "_geol_variety",
            "N_pedo":  "_pedo_variety",
            "N_geom":  "_geom_variety",
            "N_miner": "_mineral_idx",
            "N_foss":  "_fossil_idx",
        }

        # Compute maxima. Hydrography uses the SINGLE combined field _hydro
        # (lake-or-Strahler, capped at 3) that was built before this step,
        # NOT strahler + lakes.
        max_vals  = {k: 0.0 for k in src.keys()}
        max_hydro = 0.0

        for f in grid.getFeatures():
            for out_name, in_name in src.items():
                v = _safe_get(f, in_name)
                if v > max_vals[out_name]:
                    max_vals[out_name] = v
            hydro = _safe_get(f, "_hydro")
            if hydro > max_hydro:
                max_hydro = hydro

        for k in max_vals:
            if max_vals[k] <= 0:
                max_vals[k] = 0.0
        if max_hydro <= 0:
            max_hydro = 0.0

        # Add normalized fields if missing
        prov   = grid.dataProvider()
        to_add = []
        for fn in list(src.keys()) + ["N_hydro", "N_sum"]:
            if grid.fields().lookupField(fn) == -1:
                to_add.append(_make_float_field(fn))
        if to_add:
            prov.addAttributes(to_add)
            grid.updateFields()

        idxs = {fn: grid.fields().lookupField(fn)
                for fn in list(src.keys()) + ["N_hydro", "N_sum"]}

        with edit(grid):
            for f in grid.getFeatures():
                vals  = {}
                n_sum = 0.0
                for out_name, in_name in src.items():
                    raw = _safe_get(f, in_name)
                    mx  = max_vals[out_name]
                    n   = (raw / mx) if mx and mx > 0 else 0.0
                    vals[idxs[out_name]] = n
                    n_sum += n

                hydro_raw = _safe_get(f, "_hydro")
                n_hydro   = ((hydro_raw / max_hydro)
                             if max_hydro and max_hydro > 0 else 0.0)
                vals[idxs["N_hydro"]] = n_hydro
                n_sum += n_hydro

                vals[idxs["N_sum"]] = n_sum
                # QgsVectorLayer.changeAttributeValues(fid, {idx: value})
                # (the data-provider variant takes {fid: {idx: value}})
                grid.changeAttributeValues(f.id(), vals)

    def _apply_output_style(self, layer, field_name: str):
        """Apply an initial graduated style (Reds ramp, Jenks).

        QGIS 4 change: updateClasses() no longer accepts the class-method
        enum constant QgsGraduatedSymbolRenderer.Jenks.  Instead, pass a
        QgsClassificationJenks() instance to setClassificationMethod() and
        call updateClasses() with just (layer, n_classes).
        """
        try:
            if layer is None or not layer.isValid():
                return
            if layer.fields().lookupField(field_name) == -1:
                return

            style = QgsStyle.defaultStyle()
            ramp  = style.colorRamp("Reds") if style else None
            if ramp is None:
                return

            renderer = QgsGraduatedSymbolRenderer()
            renderer.setClassAttribute(field_name)
            renderer.setSourceColorRamp(ramp)

            # QGIS 4 / QGIS 3.10+: use classification method object
            try:
                renderer.setClassificationMethod(QgsClassificationJenks())
                renderer.updateClasses(layer, 5)
            except TypeError:
                # QGIS 3 (older) fallback signature
                renderer.updateClasses(
                    layer, QgsGraduatedSymbolRenderer.Jenks, 5)

            renderer.updateColorRamp(ramp)
            layer.setRenderer(renderer)
            layer.triggerRepaint()
        except Exception:
            return

    # ------------------------------------------------------------------
    # Plugin lifecycle
    # ------------------------------------------------------------------

    def add_action(self, icon_path, text, callback, parent=None):
        icon   = QIcon(icon_path)
        action = QAction(icon, text, parent)
        action.triggered.connect(callback)
        self.iface.addToolBarIcon(action)
        self.iface.addPluginToMenu(self.tr(u'&Geodiversity Calculator v2.1.2'), action)
        self.actions.append(action)
        return action

    def initGui(self):
        icon_path = os.path.join(self.plugin_dir, 'icon.png')
        self.add_action(
            icon_path,
            text=self.tr(u'Geodiversity Calculator v2.1.2'),
            callback=self.run,
            parent=self.iface.mainWindow(),
        )

    def unload(self):
        for action in self.actions:
            self.iface.removePluginMenu(
                self.tr(u'&Geodiversity Calculator v2.1.2'), action)
            self.iface.removeToolBarIcon(action)

    # ------------------------------------------------------------------
    # File-picker helpers
    # ------------------------------------------------------------------

    # Browse-button name -> (target line-edit name, dialog caption, filter).
    # A filter of None means "pick a folder". The boundary picker additionally
    # triggers grid-spacing auto-fill (handled in _pick_file).
    _VECTOR_FILTER = "Vector (*.gpkg *.shp)"
    _FILE_PICKERS = {
        'pushButton_14': ('lineEdit_16', "Select result folder",  None),
        'pushButton':    ('lineEdit',    "Select boundary",       _VECTOR_FILTER),
        'pushButton_3':  ('lineEdit_5',  "Select geology",        _VECTOR_FILTER),
        'pushButton_5':  ('lineEdit_7',  "Select pedology",       _VECTOR_FILTER),
        'pushButton_7':  ('lineEdit_9',  "Select DEM",            "Raster (*.tif)"),
        'pushButton_13': ('lineEdit_17', "Select lakes/seas",     _VECTOR_FILTER),
        'pushButton_10': ('lineEdit_12', "Select mineralogy",     _VECTOR_FILTER),
        'pushButton_11': ('lineEdit_13', "Select palaeontology",  _VECTOR_FILTER),
    }

    def _pick_file(self, target_edit: str, caption: str, file_filter):
        """Open a file/folder dialog and write the result to a line edit.

        file_filter=None -> directory picker; otherwise an open-file dialog.
        Selecting the boundary also refreshes the grid-spacing suggestion.
        """
        if file_filter is None:
            path = QFileDialog.getExistingDirectory(self.dlg, caption)
        else:
            path, _ = QFileDialog.getOpenFileName(
                self.dlg, caption, "", file_filter)
        if not path:
            return
        getattr(self.dlg, target_edit).setText(path)
        if target_edit == 'lineEdit':   # boundary
            self._suggest_grid_spacing_from_boundary(path)

    def clear_all(self):
        for name in (
            'lineEdit_16', 'lineEdit', 'lineEdit_3', 'lineEdit_4',
            'lineEdit_5', 'lineEdit_6', 'lineEdit_7', 'lineEdit_8',
            'lineEdit_9', 'lineEdit_17', 'lineEdit_12', 'lineEdit_10',
            'lineEdit_13', 'lineEdit_11', 'lineEdit_14',
        ):
            getattr(self.dlg, name).clear()

    # ------------------------------------------------------------------
    # Dialog entry point
    # ------------------------------------------------------------------

    def run(self):
        if self.first_start:
            self.first_start = False
            self.dlg = GeodiversityCalculatorDialog()

            try:
                self.dlg.lineEdit_3.clear()
                self.dlg.lineEdit_4.clear()
            except Exception:
                pass

            self._spacing_autofill = True
            try:
                self.dlg.lineEdit_3.textEdited.connect(
                    lambda _t: setattr(self, "_spacing_autofill", False))
                self.dlg.lineEdit_4.textEdited.connect(
                    lambda _t: setattr(self, "_spacing_autofill", False))
            except Exception:
                pass

            try:
                self.dlg.lineEdit.textChanged.connect(
                    self._suggest_grid_spacing_from_boundary)
            except Exception:
                pass

            # Wire every browse button from the _FILE_PICKERS table. The
            # default-argument binding captures each row's parameters.
            for btn_name, (edit, caption, flt) in self._FILE_PICKERS.items():
                btn = getattr(self.dlg, btn_name, None)
                if btn is not None:
                    btn.clicked.connect(
                        lambda _checked=False, e=edit, c=caption, f=flt:
                        self._pick_file(e, c, f))

            # QGIS 4 / Qt6: enum is now QDialogButtonBox.StandardButton.Reset
            try:
                reset_btn = self.dlg.button_box.button(
                    QDialogButtonBox.StandardButton.Reset)
            except AttributeError:
                # QGIS 3 / Qt5 fallback
                reset_btn = self.dlg.button_box.button(QDialogButtonBox.Reset)
            if reset_btn:
                reset_btn.clicked.connect(self.clear_all)

        self.dlg.show()
        # Qt6: exec() replaces exec_() (exec_() is removed in PyQt6)
        if self.dlg.exec():
            self.execute()

    # ------------------------------------------------------------------
    # Main analysis
    # ------------------------------------------------------------------

    def execute(self):
        """Main execution – all processing logic."""
        progressbar = None
        self._reset_status()
        try:
            self.iface.messageBar().clearWidgets()
            progressbar = QProgressBar()
            self.iface.messageBar().pushWidget(progressbar)
            self._set_progress(progressbar, 0)

            # ---- Read UI inputs ----
            working_dir   = self.dlg.lineEdit_16.text().strip()
            boundary_path = self.dlg.lineEdit.text().strip()
            grid_name     = self.dlg.lineEdit_14.text().strip()

            try:
                h_txt = (self.dlg.lineEdit_3.text() or '').strip()
                v_txt = (self.dlg.lineEdit_4.text() or '').strip()
                if not h_txt or not v_txt:
                    raise Exception(
                        'Grid spacing values are required '
                        '(set manually or select a boundary for auto-fill).')
                h_spacing = float(h_txt)
                v_spacing = float(v_txt)
            except ValueError:
                raise Exception("Invalid grid spacing values!")

            geology_path  = self.dlg.lineEdit_5.text().strip()  or None
            geol_field    = self.dlg.lineEdit_6.text().strip()  or None
            pedology_path = self.dlg.lineEdit_7.text().strip()  or None
            pedo_field    = self.dlg.lineEdit_8.text().strip()  or None
            dem_path      = self.dlg.lineEdit_9.text().strip()  or None
            lakes_path    = self.dlg.lineEdit_17.text().strip() or None
            mineral_path  = self.dlg.lineEdit_12.text().strip() or None
            mineral_field = self.dlg.lineEdit_10.text().strip() or None
            palaeo_path   = self.dlg.lineEdit_13.text().strip() or None
            palaeo_field  = self.dlg.lineEdit_11.text().strip() or None

            if not all([boundary_path, working_dir, grid_name]):
                raise Exception("Required fields missing!")

            try:
                show_intermediate = bool(
                    getattr(self.dlg, "checkBox_show_sublayers", None) and
                    self.dlg.checkBox_show_sublayers.isChecked())
            except Exception:
                show_intermediate = False

            def _add_layer(layer, intermediate: bool = True):
                try:
                    if layer is None or not layer.isValid():
                        return
                    if (not intermediate) or show_intermediate:
                        QgsProject.instance().addMapLayer(layer)
                except Exception:
                    return

            self.log("Starting Geodiversity Calculator v2.1.2 analysis...",
                     Qgis.MessageLevel.Info)

            # ---- STEP 1: Create Grid (3%) ----
            self.log("Creating analysis grid...", Qgis.MessageLevel.Info)
            # Base grid uses a distinct working filename so it never collides
            # with the final output file (working_dir/grid_name.gpkg), which is
            # written only at the very end in Step 11.
            grid0    = working_dir + "/" + grid_name + "_base.gpkg"
            hatar0   = QgsVectorLayer(boundary_path, "boundary", "ogr")

            if not hatar0.isValid():
                raise Exception("Boundary layer is invalid!")

            _add_layer(hatar0, intermediate=True)

            boundary_area_km2 = (hatar0.extent().width() *
                                 hatar0.extent().height() / 1_000_000)
            self.log(f"Boundary extent area: {boundary_area_km2:.0f} km²",
                     Qgis.MessageLevel.Info)

            # Advisory only: log a dataset-size hint. The actual spacing always
            # comes from the UI fields (h_spacing / v_spacing); this value is
            # never used for computation.
            area = boundary_area_km2
            for _label, _bound, _spacing in _ADVISORY_SPACING:
                if _bound is None or area <= _bound:
                    _lvl = (Qgis.MessageLevel.Warning
                            if _spacing >= 20000 else Qgis.MessageLevel.Info)
                    self.log(
                        f"{_label} dataset detected ({area:,.0f} km²). "
                        f"Consider using {_spacing} m grid spacing.", _lvl)
                    break

            crs0    = hatar0.crs().authid()
            extent0 = hatar0.extent()

            grid_type = 2
            try:
                if (hasattr(self.dlg, 'radioButton_diamond') and
                        self.dlg.radioButton_diamond.isChecked()):
                    grid_type = 3
                elif (hasattr(self.dlg, 'radioButton_hexagon') and
                      self.dlg.radioButton_hexagon.isChecked()):
                    grid_type = 4
                else:
                    grid_type = 2
            except Exception:
                grid_type = 2

            xmin = extent0.xMinimum()
            xmax = extent0.xMaximum()
            ymin = extent0.yMinimum()
            ymax = extent0.yMaximum()

            if grid_type in (3, 4):
                pad_x = 0.5 * float(h_spacing)
                pad_y = 0.5 * float(v_spacing)
                xmin -= pad_x
                xmax += pad_x
                ymin -= pad_y
                ymax += pad_y

                w = xmax - xmin
                h = ymax - ymin

                def _snap_up(size, step):
                    step = float(step)
                    if step <= 0:
                        return size
                    rem = size % step
                    return size if rem == 0 else (size + (step - rem))

                w2   = _snap_up(w, h_spacing)
                h2   = _snap_up(h, v_spacing)
                xmax = xmin + w2
                ymin = ymax - h2

            extent_str = f"{xmin},{xmax},{ymin},{ymax}"
            create_res = processing.run("native:creategrid", {
                'TYPE':     grid_type,
                'EXTENT':   extent_str,
                'HSPACING': h_spacing,
                'VSPACING': v_spacing,
                'CRS':      crs0,
                'OUTPUT':   'TEMPORARY_OUTPUT',
            })
            grid0_temp = create_res.get('OUTPUT')

            self._set_progress(progressbar, 2)

            # ---- STEP 1B: Extract cells intersecting boundary ----
            self.log("Selecting grid cells that intersect boundary...",
                     Qgis.MessageLevel.Info)
            processing.run("native:extractbylocation", {
                'INPUT':     grid0_temp,
                'PREDICATE': [0],
                'INTERSECT': boundary_path,
                'OUTPUT':    grid0,
            })

            addGrid0 = QgsVectorLayer(grid0, grid_name, "ogr")
            if not addGrid0.isValid():
                raise Exception("Grid creation failed!")

            grid_cell_count = addGrid0.featureCount()
            self.log(
                f"Grid created with {grid_cell_count} cells "
                f"(whole cells that touch boundary)",
                Qgis.MessageLevel.Info)

            self._set_progress(progressbar, 5)

            # Geology and pedology share identical "polygon variety" logic;
            # a local helper keeps it in one place while still capturing the
            # surrounding locals (_add_layer, grid0, working_dir).
            def _polygon_variety(path, field, layer_name, out_name,
                                 out_field, status_key, label):
                """Compute a vector 'any-touch' variety subindex layer.

                Returns the result layer (or None). Records run status.
                """
                if not (path and field):
                    return None
                self.log(f"Processing {label} data...", Qgis.MessageLevel.Info)
                try:
                    src = QgsVectorLayer(path, layer_name, "ogr")
                    if not src.isValid():
                        self._set_status(status_key, 'skipped',
                                         "layer could not be loaded")
                        return None
                    _add_layer(src, intermediate=True)
                    self._encode_unique_values(src, field, out_field='r_value')
                    result = self._vector_touch_variety(
                        grid_path=grid0,
                        poly_layer=src,
                        out_path=working_dir + "/" + out_name,
                        out_field=out_field,
                        value_field='r_value',
                    )
                    self._remove_field_if_exists(src, 'r_value')
                    _add_layer(result, intermediate=True)
                    self._set_status(status_key, 'ok')
                    return result
                except Exception as e:
                    self.log(f"{label} processing error (skipping): {str(e)}",
                             Qgis.MessageLevel.Warning)
                    self._set_status(status_key, 'skipped', str(e))
                    return None

            # ---- STEP 2: Process Geology (20%) ----
            geol_layer = _polygon_variety(
                geology_path, geol_field, "geology", "geology_grid.gpkg",
                '_geol_variety', 'geology', "geology")
            self._set_progress(progressbar, 20)

            # ---- STEP 3: Process Pedology (35%) ----
            pedo_layer = _polygon_variety(
                pedology_path, pedo_field, "pedology", "pedology_grid.gpkg",
                '_pedo_variety', 'pedology', "pedology")
            self._set_progress(progressbar, 35)

            # ---- STEP 4: Process DEM – Geomorphon & Strahler (70%) ----
            geom_layer = None
            stra_layer = None
            if dem_path:
                self.log("Processing DEM (geomorphon & Strahler)...",
                         Qgis.MessageLevel.Info)
                try:
                    cut_dem2 = working_dir + "/cut_dem.tif"
                    crs2     = hatar0.crs().authid()

                    try:
                        processing.run("gdal:cliprasterbymasklayer", {
                            'INPUT':          dem_path,
                            'MASK':           boundary_path,
                            'SOURCE_CRS':     crs2,
                            'TARGET_CRS':     crs2,
                            'CROP_TO_CUTLINE': True,
                            'DATA_TYPE':      0,
                            'OUTPUT':         cut_dem2,
                        })
                    except Exception:
                        self.log("DEM clipping failed, trying alternative...",
                                 Qgis.MessageLevel.Warning)
                        processing.run("gdal:warpreproject", {
                            'INPUT':      dem_path,
                            'TARGET_CRS': crs2,
                            'OUTPUT':     cut_dem2,
                        })

                    self._set_progress(progressbar, 45)

                    # Geomorphon
                    # QGIS 4 change: provider renamed from "grass7" to "grass"
                    geom2 = working_dir + "/geomorphon.tif"
                    try:
                        try:
                            processing.run("grass:r.geomorphon", {
                                'elevation': cut_dem2,
                                'search':    3,
                                'skip':      0,
                                'flat':      3,
                                'dist':      0,
                                'forms':     geom2,
                                '-m':        False,
                                '-e':        False,
                            })
                        except Exception:
                            # Fallback for systems still registering as grass7
                            processing.run("grass7:r.geomorphon", {
                                'elevation': cut_dem2,
                                'search':    3,
                                'skip':      0,
                                'flat':      3,
                                'dist':      0,
                                'forms':     geom2,
                                '-m':        False,
                                '-e':        False,
                            })

                        addRaster2 = QgsRasterLayer(geom2, "geomorphon_raster")
                        _add_layer(addRaster2, intermediate=True)

                        output_grid33 = working_dir + "/geomorphon_grid.gpkg"
                        # QGIS 4 change: algorithm renamed from qgis: to native:
                        processing.run("native:zonalstatisticsfb", {
                            'INPUT':        grid0,
                            'INPUT_RASTER': geom2,
                            'RASTER_BAND':  1,
                            'COLUMN_PREFIX': '_geom_',
                            'STATISTICS':   [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
                            'OUTPUT':       output_grid33,
                        })

                        geom_layer = QgsVectorLayer(
                            output_grid33, "geomorphon_grid", "ogr")
                        geom_layer.dataProvider().deleteAttributes(
                            [6, 7, 8, 9, 10, 11, 12, 13, 14])
                        geom_layer.updateFields()
                        _add_layer(geom_layer, intermediate=True)
                        self._set_status('geomorphology', 'ok')
                    except Exception as e_geom:
                        self.log(f"Geomorphon failed (skipping): {str(e_geom)}",
                                 Qgis.MessageLevel.Warning)
                        self._set_status('geomorphology', 'skipped',
                                         "geomorphon computation failed")

                    self._set_progress(progressbar, 55)

                    # ---- Strahler stream order (self-contained) ----
                    #
                    # SAGA is gone from QGIS 4 and the r.stream.order GRASS
                    # add-on is not installed by default. To avoid ANY extra
                    # install for end users, we:
                    #   1. run grass:r.watershed (a CORE module, always present)
                    #      to get a stream raster + D8 drainage direction, then
                    #   2. compute true Strahler order ourselves in Python
                    #      (_compute_strahler_raster), then
                    #   3. summarise per grid cell with zonal statistics.
                    try:
                        self.log(
                            "Computing streams & drainage (GRASS r.watershed)...",
                            Qgis.MessageLevel.Info)
                        stream_path   = working_dir + "/_ws_stream.tif"
                        drainage_path = working_dir + "/_ws_drainage.tif"
                        # The "-s" flag forces single-flow-direction (D8), which
                        # our Strahler routing requires. Different QGIS GRASS
                        # provider versions expect the flag as '-s' or 's';
                        # try the common form, then fall back without it (the
                        # drainage map is still D8-coded either way).
                        ws_params = {
                            'elevation':    cut_dem2,
                            'threshold':    100,
                            'stream':       stream_path,
                            'drainage':     drainage_path,
                            'GRASS_REGION_CELLSIZE_PARAMETER': 0,
                        }
                        try:
                            processing.run("grass:r.watershed",
                                           dict(ws_params, **{'-s': True}))
                        except Exception:
                            processing.run("grass:r.watershed", ws_params)

                        self._set_progress(progressbar, 60)

                        self.log(
                            "Computing Strahler order (built-in algorithm)...",
                            Qgis.MessageLevel.Info)
                        strahler_output5 = working_dir + "/strahler.tif"
                        self._compute_strahler_raster(
                            stream_path, drainage_path, strahler_output5)

                        self._set_progress(progressbar, 65)

                        output_grid5 = working_dir + "/strahler_grid.gpkg"
                        processing.run("native:zonalstatisticsfb", {
                            'INPUT':         grid0,
                            'INPUT_RASTER':  strahler_output5,
                            'RASTER_BAND':   1,
                            'COLUMN_PREFIX': '_stra_',
                            'STATISTICS':    [6],   # maximum
                            'OUTPUT':        output_grid5,
                        })

                        stra_layer = QgsVectorLayer(
                            output_grid5, "strahler_grid", "ogr")
                        stra_layer.updateFields()
                        _add_layer(stra_layer, intermediate=True)

                        # NOTE: _stra_max here holds the RAW maximum Strahler
                        # order per cell. The hydrography transform
                        # (round(order/2), cap 3, lake override) is applied
                        # later in a single place (the _hydro step), so we do
                        # NOT modify the values here. Doing the transform in one
                        # place avoids depending on this intermediate field name
                        # and prevents accidental double-transformation.
                        self._set_status('hydrography', 'ok')

                    except Exception as e:
                        self.log(
                            f"Strahler processing error (skipping): {str(e)}",
                            Qgis.MessageLevel.Warning)
                        self._set_status(
                            'hydrography', 'skipped',
                            "stream-order computation failed")

                except Exception as e:
                    self.log(f"DEM processing error: {str(e)}",
                             Qgis.MessageLevel.Warning)
                    # The DEM drives BOTH geomorphology and hydrography.
                    if self._status.get('geomorphology') not in ('ok',):
                        self._set_status('geomorphology', 'skipped',
                                         "DEM processing failed")
                    if self._status.get('hydrography') not in ('ok',):
                        self._set_status('hydrography', 'skipped',
                                         "DEM processing failed")

            self._set_progress(progressbar, 70)

            # Mineralogy and palaeontology share identical point-in-polygon
            # counting logic; a local helper keeps it in one place.
            def _point_diversity(path, field, layer_name, out_name,
                                 out_field, status_key, label):
                """Count distinct point categories per cell. Returns layer/None."""
                if not (path and field):
                    return None
                self.log(f"Processing {label} data...", Qgis.MessageLevel.Info)
                try:
                    src = QgsVectorLayer(path, layer_name, "ogr")
                    if not src.isValid():
                        self._set_status(status_key, 'skipped',
                                         "layer could not be loaded")
                        return None
                    _add_layer(src, intermediate=True)
                    self._encode_unique_values(src, field, out_field='r_value')
                    out_path = working_dir + "/" + out_name
                    processing.run("native:countpointsinpolygon", {
                        'POLYGONS':   grid0,
                        'POINTS':     src,
                        'CLASSFIELD': 'r_value',
                        'FIELD':      out_field,
                        'OUTPUT':     out_path,
                    })
                    result = QgsVectorLayer(out_path, out_name, "ogr")
                    _add_layer(result, intermediate=True)
                    self._set_status(status_key, 'ok')
                    return result
                except Exception as e:
                    self.log(f"{label} error (skipping): {str(e)}",
                             Qgis.MessageLevel.Warning)
                    self._set_status(status_key, 'skipped', str(e))
                    return None

            # ---- STEP 5: Process Mineralogy (80%) ----
            mine_layer = _point_diversity(
                mineral_path, mineral_field, "mineral_occurences",
                "mineral_grid.gpkg", '_mineral_idx', 'mineralogy', "mineralogy")
            self._set_progress(progressbar, 80)

            # ---- STEP 6: Process Palaeontology (85%) ----
            foss_layer = _point_diversity(
                palaeo_path, palaeo_field, "fossil_occurences",
                "fossil_grid.gpkg", '_fossil_idx', 'palaeontology',
                "palaeontology")
            self._set_progress(progressbar, 85)

            # ---- STEP 7: Process Lakes (87%) ----
            if lakes_path:
                self.log("Processing lake/sea data...", Qgis.MessageLevel.Info)
                try:
                    lakes7_input = QgsVectorLayer(lakes_path, "lakes", "ogr")
                    if lakes7_input.isValid():
                        addGrid0.dataProvider().addAttributes(
                            [_make_int_field('_lakes')])
                        addGrid0.updateFields()

                        selection = processing.run("native:selectbylocation", {
                            'INPUT':     addGrid0,
                            'INTERSECT': lakes7_input,
                            'METHOD':    0,
                            'PREDICATE': [0],
                        })

                        with edit(addGrid0):
                            for fid in selection['OUTPUT'].selectedFeatureIds():
                                feature = addGrid0.getFeature(fid)
                                feature['_lakes'] = 3
                                addGrid0.updateFeature(feature)

                        addGrid0.removeSelection()
                        # Lakes alone are a valid hydrography contribution even
                        # if Strahler was skipped; mark hydrography ok if not
                        # already.
                        if self._status.get('hydrography') != 'ok':
                            self._set_status('hydrography', 'ok')
                except Exception as e:
                    self.log(f"Lakes error (skipping): {str(e)}",
                             Qgis.MessageLevel.Warning)

            self._set_progress(progressbar, 87)

            # ---- STEP 8: Merge all subindex fields into one in-memory layer ----
            #
            # Strategy: chain native:joinattributestable calls using
            # TEMPORARY_OUTPUT so nothing extra is written to disk.  Only the
            # very last step writes the single final .gpkg the user named in
            # the GUI.  This produces exactly one output file and no leftover
            # intermediate files.

            self.log("Joining subindex fields...", Qgis.MessageLevel.Info)

            # The zonal-statistics maximum field can be named '_stra_max' or
            # '_stra_maximum' depending on QGIS version. Detect the real name
            # from the Strahler layer so the join copies the correct column.
            stra_join_field = '_stra_max'
            if stra_layer is not None and stra_layer.isValid():
                for i in range(stra_layer.fields().count()):
                    nm = stra_layer.fields().at(i).name()
                    if nm.startswith('_stra_'):
                        stra_join_field = nm
                        break

            join_specs = [
                (geol_layer, '_geol_variety'),
                (pedo_layer, '_pedo_variety'),
                (geom_layer, '_geom_variety'),
                (stra_layer, stra_join_field),
                (mine_layer, '_mineral_idx'),
                (foss_layer, '_fossil_idx'),
            ]

            # Determine the single final output path the user named in the GUI.
            # IMPORTANT: write to a NEW, non-existing file. The base grid was
            # created at `grid0` during Step 1B (and edited by the lakes step),
            # so that exact path/handle is still "live". Reusing it as a
            # native:savefeatures OUTPUT is what previously caused the result to
            # be missing. We therefore build the final layer at `final_path` and
            # delete any stale copy first.
            final_path = os.path.join(working_dir, grid_name + ".gpkg")

            def _delete_gpkg(path):
                """Remove a .gpkg (and its sidecar files) if present."""
                for p in (path, path + "-wal", path + "-shm",
                          path + "-journal"):
                    try:
                        if os.path.exists(p):
                            os.remove(p)
                    except Exception:
                        pass

            # Force-close the base grid object so its file handle is released
            # before we read it back into the join chain.
            base_grid_path = grid0
            try:
                addGrid0.commitChanges()
            except Exception:
                pass

            # Run the join chain. The first INPUT is the on-disk base grid
            # (which already contains the _lakes column). Each join produces an
            # in-memory layer that feeds the next.
            current_input = base_grid_path   # str path for the first call
            any_join = False

            for src_layer, src_field in join_specs:
                if src_layer is None or not src_layer.isValid():
                    continue
                if src_layer.fields().lookupField(src_field) == -1:
                    self.log(
                        f"Field {src_field} not found in join source - skipping.",
                        Qgis.MessageLevel.Warning)
                    continue
                try:
                    result = processing.run("native:joinattributestable", {
                        'INPUT':              current_input,
                        'FIELD':              'id',
                        'INPUT_2':            src_layer,
                        'FIELD_2':            'id',
                        'FIELDS_TO_COPY':     [src_field],
                        'METHOD':             1,      # first matching feature
                        'DISCARD_NONMATCHING': False,
                        'PREFIX':             '',
                        'OUTPUT':             'TEMPORARY_OUTPUT',
                    })
                    current_input = result['OUTPUT']
                    any_join = True
                    self.log(f"Joined {src_field} into working layer.",
                             Qgis.MessageLevel.Info)
                except Exception as e:
                    self.log(
                        f"Join of {src_field} failed (skipping): {str(e)}",
                        Qgis.MessageLevel.Warning)

            self._set_progress(progressbar, 90)

            # Resolve the joined result into a single working layer.
            if isinstance(current_input, str):
                grid = QgsVectorLayer(current_input, grid_name, "ogr")
            else:
                grid = current_input
            if not grid.isValid():
                raise Exception("Joined grid layer is invalid.")

            # ---- STEP 9: Delete NULL geology features ----
            if geol_layer and grid.fields().lookupField('_geol_variety') != -1:
                try:
                    with edit(grid):
                        features_to_delete = [
                            f.id() for f in grid.getFeatures()
                            if f['_geol_variety'] is None
                        ]
                        if features_to_delete:
                            grid.deleteFeatures(features_to_delete)
                            self.log(
                                f"Deleted {len(features_to_delete)} cells "
                                f"with NULL geology",
                                Qgis.MessageLevel.Info)
                except Exception as e:
                    self.log(f"NULL deletion warning: {str(e)}",
                             Qgis.MessageLevel.Warning)

            self._set_progress(progressbar, 92)

            # ---- Build the combined hydrography subindex (_hydro) ----
            # Per the methodology, hydrography is a SINGLE value per cell,
            # capped at 3, and is NOT the sum of lakes + Strahler:
            #   - if the cell contains a lake  -> 3 (overrides everything)
            #   - otherwise                    -> round(maxStrahler / 2),
            #                                     capped at 3
            #     (round half up; e.g. order 3 -> round(1.5)=2; order>=6 -> 3)
            #
            # We read the RAW Strahler maximum here and apply the transform in
            # this single place. The zonal-statistics field name can vary by
            # QGIS version ('_stra_max' vs '_stra_maximum'), so we detect it by
            # prefix instead of hard-coding it.
            all_field_names = [grid.fields().at(i).name()
                               for i in range(grid.fields().count())]
            stra_field_name = None
            for fn in all_field_names:
                if fn.startswith('_stra_'):
                    stra_field_name = fn
                    break
            has_lakes = '_lakes' in all_field_names

            if grid.fields().lookupField('_hydro') == -1:
                grid.dataProvider().addAttributes([_make_int_field('_hydro')])
                grid.updateFields()
            hydro_idx = grid.fields().lookupField('_hydro')

            def _raw_strahler_to_hydro(order_val):
                """round(order/2) half-up, capped at 3; 0 if no stream."""
                try:
                    o = float(order_val) if order_val is not None else 0.0
                except Exception:
                    o = 0.0
                if o <= 0:
                    return 0
                v = int(math.floor(o / 2.0 + 0.5))   # round half up
                return 3 if v > 3 else v

            with edit(grid):
                for f in grid.getFeatures():
                    # Lake value (set to 3 in the lakes step; else 0/NULL)
                    lake_val = f['_lakes'] if has_lakes else None
                    try:
                        lake_val = int(lake_val) if lake_val is not None else 0
                    except Exception:
                        lake_val = 0

                    if lake_val > 0:
                        # Lake present -> hydrography is automatically 3,
                        # regardless of any stream order in the cell.
                        hydro = 3
                    else:
                        # No lake -> derive from the RAW Strahler order via
                        # round(order/2) capped at 3.
                        raw_stra = (f[stra_field_name]
                                    if stra_field_name else None)
                        hydro = _raw_strahler_to_hydro(raw_stra)

                    grid.changeAttributeValue(f.id(), hydro_idx, hydro)

            self._set_progress(progressbar, 93)

            # ---- Optional normalization (on the in-memory layer) ----
            try:
                do_norm = bool(
                    getattr(self.dlg, "checkBox_normalize", None) and
                    self.dlg.checkBox_normalize.isChecked())
            except Exception:
                do_norm = False

            if do_norm:
                self.log("Normalizing subindices (0-1)...",
                         Qgis.MessageLevel.Info)
                self._add_normalized_fields(grid)

            # ---- STEP 10: Calculate final GEODIVERSITY index ----
            # Done on the in-memory layer BEFORE writing to disk, so the saved
            # file already contains _GEODIV (and N_* if normalized).
            self.log("Calculating final geodiversity index...",
                     Qgis.MessageLevel.Info)

            if grid.fields().lookupField('_GEODIV') == -1:
                grid.dataProvider().addAttributes([_make_int_field('_GEODIV')])
                grid.updateFields()
            idx = grid.fields().lookupField('_GEODIV')

            # Only reference fields that actually exist on this layer, so a
            # missing optional subindex never breaks the expression.
            # Hydrography is represented by the single combined field _hydro
            # (lake-or-Strahler, capped at 3) — NOT by _lakes + _stra_max.
            present = {grid.fields().at(i).name()
                       for i in range(grid.fields().count())}
            terms = []
            for fld in ('_hydro', '_geol_variety', '_pedo_variety',
                        '_geom_variety', '_mineral_idx', '_fossil_idx'):
                if fld in present:
                    terms.append(
                        f'(CASE WHEN "{fld}" IS NOT NULL THEN "{fld}" ELSE 0 END)')
            geodiv_expr_str = " + ".join(terms) if terms else "0"

            context    = QgsExpressionContext()
            expression = QgsExpression(geodiv_expr_str)
            scope      = QgsExpressionContextScope()
            scope.setFields(grid.fields())
            context.appendScope(scope)
            expression.prepare(context)

            with edit(grid):
                for feature in grid.getFeatures():
                    context.setFeature(feature)
                    geodiv = expression.evaluate(context)
                    try:
                        geodiv = int(geodiv) if geodiv is not None else 0
                    except Exception:
                        geodiv = 0
                    grid.changeAttributeValue(feature.id(), idx, geodiv)

            self._set_progress(progressbar, 95)

            # ---- STEP 11: Write the SINGLE output file & add to project ----
            # Write the fully-computed layer to the user's named file. We delete
            # any pre-existing file at that path first so savefeatures creates a
            # clean GeoPackage rather than appending a second table.
            self.log(f"Writing final grid to {final_path} ...",
                     Qgis.MessageLevel.Info)
            _delete_gpkg(final_path)

            save_res = processing.run("native:savefeatures", {
                'INPUT':  grid,
                'OUTPUT': final_path,
            })

            # Release the temporary working layer (frees memory / handles).
            try:
                del grid
            except Exception:
                pass
            try:
                del addGrid0
            except Exception:
                pass

            # Prefer the layer object returned by savefeatures; fall back to
            # opening the written file by path.
            final_layer = None
            try:
                out_obj = save_res.get('OUTPUT')
                if isinstance(out_obj, QgsVectorLayer) and out_obj.isValid():
                    final_layer = out_obj
                    final_layer.setName(grid_name)
            except Exception:
                final_layer = None

            if final_layer is None:
                final_layer = QgsVectorLayer(final_path, grid_name, "ogr")

            if not final_layer.isValid():
                raise Exception(
                    f"Could not open the written output file: {final_path}")

            # Verify _GEODIV really made it to disk; warn loudly if not.
            if final_layer.fields().lookupField('_GEODIV') == -1:
                self.log(
                    "WARNING: _GEODIV missing from written file - "
                    "check field names in source layers.",
                    Qgis.MessageLevel.Warning)

            added = QgsProject.instance().addMapLayer(final_layer)
            if added is None:
                self.log("WARNING: addMapLayer returned None.",
                         Qgis.MessageLevel.Warning)
            else:
                self.log("Final layer added to project.",
                         Qgis.MessageLevel.Info)

            try:
                style_field = "N_sum" if do_norm else "_GEODIV"
                self._apply_output_style(final_layer, style_field)
            except Exception:
                pass

            # Keep grid0 referring to the actual output for the manifest below.
            grid0 = final_path

            # Release file handles held by the subindex layer objects so their
            # source files can be deleted in the cleanup step below.
            if not show_intermediate:
                for _lyr_name in ('geol_layer', 'pedo_layer', 'geom_layer',
                                  'stra_layer', 'mine_layer', 'foss_layer'):
                    try:
                        _obj = locals().get(_lyr_name)
                        if _obj is not None:
                            del _obj
                    except Exception:
                        pass

            # ---- Clean up intermediate working files ----
            # Remove every helper file the analysis produced, keeping ONLY the
            # single final grid the user named in the GUI. This includes the
            # base grid, per-theme subindex grids, and DEM-derived rasters.
            #
            # Skip cleanup if the user asked to keep intermediate layers in the
            # project (deleting their source files would break those layers).
            if not show_intermediate:
                self.log("Cleaning up intermediate files...",
                         Qgis.MessageLevel.Info)
                keep_basename = os.path.basename(final_path)
                intermediate_names = (
                    grid_name + "_base.gpkg",
                    "geology_grid.gpkg", "pedology_grid.gpkg",
                    "geomorphon_grid.gpkg", "strahler_grid.gpkg",
                    "mineral_grid.gpkg", "fossil_grid.gpkg",
                    "cut_dem.tif", "geomorphon.tif", "strahler.tif",
                    "_ws_stream.tif", "_ws_drainage.tif",
                )
                try:
                    for fname in os.listdir(working_dir):
                        fpath = os.path.join(working_dir, fname)
                        if not os.path.isfile(fpath):
                            continue
                        if fname == keep_basename:
                            continue
                        remove = False
                        if fname in intermediate_names:
                            remove = True
                        elif fname.startswith(grid_name + "_base.gpkg"):
                            remove = True  # -wal / -shm sidecars
                        elif (fname.startswith(("strahler.tif",
                                                "geomorphon.tif",
                                                "cut_dem.tif"))
                              and fname.endswith(".aux.xml")):
                            remove = True  # GDAL raster sidecars
                        if remove:
                            try:
                                os.remove(fpath)
                            except Exception:
                                pass
                except Exception as e:
                    self.log(f"Cleanup warning: {str(e)}",
                             Qgis.MessageLevel.Warning)

            self._set_progress(progressbar, 98)


            # ---- Generate output manifest ----
            output_files  = []
            manifest_path = working_dir + "/" + grid_name + "_MANIFEST.txt"

            for file in os.listdir(working_dir):
                if os.path.isfile(os.path.join(working_dir, file)):
                    file_size_mb = (os.path.getsize(
                        os.path.join(working_dir, file)) / (1024 * 1024))
                    output_files.append(f"{file} ({file_size_mb:.2f} MB)")

            with open(manifest_path, 'w', encoding='utf-8') as f:
                f.write("=" * 70 + "\n")
                f.write("GEODIVERSITY CALCULATOR v2.1.2 - OUTPUT FILE MANIFEST\n")
                f.write("=" * 70 + "\n\n")
                f.write(f"Analysis Date: "
                        f"{QDateTime.currentDateTime().toString('yyyy-MM-dd HH:mm:ss')}\n")
                f.write(f"Boundary Area: {boundary_area_km2:.0f} km²\n")
                f.write(f"Grid Spacing: {h_spacing}m x {v_spacing}m\n")
                f.write(f"Grid Cells: {grid_cell_count}\n")
                f.write(f"Working Directory: {working_dir}\n\n")
                f.write("=" * 70 + "\n")
                f.write("OUTPUT FILES:\n")
                f.write("=" * 70 + "\n\n")
                for file in sorted(output_files):
                    f.write(f"  - {file}\n")
                f.write("\n" + "=" * 70 + "\n")
                f.write("ANALYSIS COMPONENTS:\n")
                f.write("=" * 70 + "\n\n")
                _status_glyph = {
                    'ok': '[OK]', 'skipped': '[SKIPPED]',
                    'failed': '[FAILED]', 'absent': '[not provided]',
                }
                _comp_labels = [
                    ('geology',       'Geology (variety)'),
                    ('pedology',      'Pedology (variety)'),
                    ('geomorphology', 'Geomorphology (geomorphon)'),
                    ('hydrography',   'Hydrography (Strahler + lakes)'),
                    ('mineralogy',    'Mineralogy (occurrence diversity)'),
                    ('palaeontology', 'Palaeontology (fossil diversity)'),
                ]
                for _comp, _label in _comp_labels:
                    _st = self._status.get(_comp, 'absent')
                    f.write(f"  {_status_glyph.get(_st, '[?]')} {_label}\n")
                if self._status_notes:
                    f.write("\nNotes:\n")
                    for _note in self._status_notes:
                        f.write(f"  - {_note}\n")
                f.write("\n" + "=" * 70 + "\n")
                f.write(f"Final Grid: {os.path.basename(final_path)}\n")
                f.write(f"Total Files: {len(output_files)}\n")
                f.write("=" * 70 + "\n")

            self.log(f"Output manifest saved: {manifest_path}",
                     Qgis.MessageLevel.Info)
            self._set_progress(progressbar, 100)

            # ---- Final user feedback (status-aware) ----
            level, summary = self._build_status_summary()
            header = (f"Geodiversity grid created: {grid_cell_count} cells. "
                      f"Output: {os.path.basename(final_path)}")

            # Always show a message-bar notice (visible without opening logs).
            self._notify("Geodiversity Calculator",
                         header + "  —  " + summary.replace("\n", "  "),
                         level=level, duration=25)

            # For skipped/failed components, also show a modal dialog so the
            # user cannot miss that the result is partial. A fully successful
            # run shows no dialog (avoids nagging expert users).
            has_problems = any(
                self._status.get(c) in ('skipped', 'failed')
                for c in self._status)
            if has_problems:
                detail = summary
                if self._status_notes:
                    detail += "\n\nDetails:\n- " + "\n- ".join(
                        self._status_notes)
                detail += ("\n\nThe output grid was still created with the "
                           "components that succeeded. See the Log Messages "
                           "panel and the _MANIFEST.txt file for full details.")
                self._notify_dialog(
                    "Geodiversity Calculator - completed with warnings",
                    header + "\n\n" + detail,
                    critical=False)
            else:
                # Fully successful run: confirm completion with a dialog too,
                # so users get clear positive feedback (mirrors the error case).
                success_detail = summary + (
                    f"\n\nOutput file: {os.path.basename(final_path)}"
                    f"\nLocation: {working_dir}"
                    f"\nGrid cells: {grid_cell_count}"
                    "\n\nThe geodiversity grid has been added to your project.")
                self._notify_dialog(
                    "Geodiversity Calculator - analysis complete",
                    header + "\n\n" + success_detail,
                    critical=False)

            self.log("Analysis complete!", Qgis.MessageLevel.Info)

        except Exception as e:
            error_msg = f"GeoDiversity v2.1.2 Error: {str(e)}"
            self.log(error_msg, Qgis.MessageLevel.Critical)
            self.log(traceback.format_exc(), Qgis.MessageLevel.Critical)
            # Visible message bar AND a modal dialog: a hard failure means no
            # usable output, so the user must be told clearly.
            self._notify("Geodiversity Calculator - error", error_msg,
                         level=Qgis.MessageLevel.Critical, duration=0)
            self._notify_dialog(
                "Geodiversity Calculator - analysis failed",
                ("The analysis could not be completed:\n\n"
                 f"{str(e)}\n\n"
                 "No output grid was produced. See the Log Messages panel "
                 "for the full technical traceback."),
                critical=True)

        finally:
            if progressbar:
                self.iface.messageBar().clearWidgets()
