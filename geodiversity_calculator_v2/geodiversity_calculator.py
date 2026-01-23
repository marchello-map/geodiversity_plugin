# -*- coding: utf-8 -*-
"""
/***************************************************************************
 Geodiversity Calculator v2.0
                                 A QGIS plugin
 The world's most advanced, robust geodiversity assessment plugin.
 Handles national-scale datasets with bulletproof error handling.
 Based on the proven methodology by Márton Pál, enhanced for v2.0.
                              -------------------
        begin                : 2026-01-23
        copyright            : (C) 2026 v2.0 Team (enhanced from Márton Pál by Márton Pál and Emmanuel Owusu-Acheampong)
        email                : pal.marton@inf.elte.hu & emmaoacheamp@student.elte.hu
 ***************************************************************************/
"""
from qgis.PyQt.QtCore import QSettings, QTranslator, QCoreApplication, QVariant, QDateTime
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction, QFileDialog, QDialogButtonBox, QProgressBar
from qgis.core import (
    Qgis, QgsVectorLayer, QgsProject, QgsField, QgsProcessingFeedback,
    QgsRasterLayer, QgsMessageLog, QgsVectorLayerJoinInfo,
    QgsExpression, QgsExpressionContext, QgsExpressionContextScope, edit,
    QgsStyle, QgsGraduatedSymbolRenderer
)
from .geodiversity_calculator_dialog import GeodiversityCalculatorDialog
import os
import os.path
import processing
import math
import traceback

class GeodiversityCalculator:
    """Geodiversity calculator"""
    
    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.actions = []
        self.first_start = True
        self.dlg = None
        
    def tr(self, message):
        return QCoreApplication.translate('GeodiversityCalculator', message)
    
    def log(self, message, level=Qgis.Info):
        """Enhanced logging"""
        QgsMessageLog.logMessage(message, 'Geodiversity Calculator', level)


    def _set_progress(self, progressbar, value: int):
        """Update progress bar and let the UI breathe."""
        try:
            if progressbar:
                progressbar.setValue(int(value))
                QCoreApplication.processEvents()
        except Exception:
            pass

    def _remove_field_if_exists(self, layer: QgsVectorLayer, field_name: str):
        """Physically remove a field from a layer (datasource) if present.

        Note: this modifies the source layer on disk (shp/gpkg).
        """
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

    def _suggest_grid_spacing_from_boundary(self, boundary_path: str):
        """Auto-suggest grid spacing based on boundary extent area (km²)."""
        try:
            # Only auto-overwrite spacing if it hasn't been manually edited.
            if hasattr(self, "_spacing_autofill") and not self._spacing_autofill:
                return

            # Recompute every time (user may pick multiple boundaries without reloading)
            boundary_path = (boundary_path or "").strip()
            if not boundary_path or not os.path.exists(boundary_path):
                return

            layer = QgsVectorLayer(boundary_path, "boundary", "ogr")
            if not layer.isValid():
                return
            area_km2 = layer.extent().width() * layer.extent().height() / 1_000_000.0
            area = area_km2
            if area <= 0:
                return

            # Explicit interval-based categories (km²).
            # Note: the minimum and maximum categories are open-ended; all others are closed intervals.
            if area > 5_000_000:
                suggested = 50000
            elif 1_000_000 < area <= 5_000_000:
                suggested = 20000
            elif 100_000 < area <= 1_000_000:
                suggested = 10000
            elif 50_000 < area <= 100_000:
                suggested = 7500
            elif 10_000 < area <= 50_000:
                suggested = 5000
            elif 2_500 < area <= 10_000:
                suggested = 2500
            else:  # 0 < area <= 5_000
                suggested = 1000

            # Overwrite both H and V spacing when in autofill mode
            self.dlg.lineEdit_3.setText(str(suggested))
            self.dlg.lineEdit_4.setText(str(suggested))
        except Exception:
            # silent - suggestion should never break the workflow
            return

    def _add_normalized_fields(self, grid):
        """Create normalized subindex fields (0-1) and their sum (N_sum)."""
        # Source fields
        src = {
            "N_geol": "J_geol_variety",
            "N_pedo": "J_pedo_variety",
            "N_geom": "J_geom_variety",
            "N_miner": "J_mineral_idx",
            "N_foss": "J_fossil_idx",
        }

        # Compute maxima (hydro uses (strahler + lakes) BEFORE normalization)
        max_vals = {k: 0.0 for k in src.keys()}
        max_hydro = 0.0

        for f in grid.getFeatures():
            for out_name, in_name in src.items():
                v = f[in_name]
                try:
                    v = float(v) if v is not None else 0.0
                except Exception:
                    v = 0.0
                if v > max_vals[out_name]:
                    max_vals[out_name] = v

            stra = f["J_stra_max"]
            lakes = f["_lakes"]
            try:
                stra = float(stra) if stra is not None else 0.0
            except Exception:
                stra = 0.0
            try:
                lakes = float(lakes) if lakes is not None else 0.0
            except Exception:
                lakes = 0.0
            hydro = stra + lakes
            if hydro > max_hydro:
                max_hydro = hydro

        # Avoid divide-by-zero (if max=0, keep all zeros)
        for k in max_vals:
            if max_vals[k] <= 0:
                max_vals[k] = 0.0
        if max_hydro <= 0:
            max_hydro = 0.0

        # Add fields if missing
        prov = grid.dataProvider()
        to_add = []
        for fn in list(src.keys()) + ["N_hydro", "N_sum"]:
            if grid.fields().lookupField(fn) == -1:
                to_add.append(QgsField(fn, QVariant.Double))
        if to_add:
            prov.addAttributes(to_add)
            grid.updateFields()

        idxs = {fn: grid.fields().lookupField(fn) for fn in list(src.keys()) + ["N_hydro", "N_sum"]}

        with edit(grid):
            for f in grid.getFeatures():
                vals = {}
                n_sum = 0.0

                for out_name, in_name in src.items():
                    raw = f[in_name]
                    try:
                        raw = float(raw) if raw is not None else 0.0
                    except Exception:
                        raw = 0.0
                    mx = max_vals[out_name]
                    n = (raw / mx) if mx and mx > 0 else 0.0
                    vals[idxs[out_name]] = n
                    n_sum += n

                stra = f["J_stra_max"]
                lakes = f["_lakes"]
                try:
                    stra = float(stra) if stra is not None else 0.0
                except Exception:
                    stra = 0.0
                try:
                    lakes = float(lakes) if lakes is not None else 0.0
                except Exception:
                    lakes = 0.0
                hydro_raw = stra + lakes
                n_hydro = (hydro_raw / max_hydro) if max_hydro and max_hydro > 0 else 0.0
                vals[idxs["N_hydro"]] = n_hydro
                n_sum += n_hydro

                vals[idxs["N_sum"]] = n_sum
                grid.dataProvider().changeAttributeValues({f.id(): vals})

    def _apply_output_style(self, layer, field_name: str):
        """Apply an initial graduated style (Reds ramp, Jenks)."""
        try:
            if layer is None or not layer.isValid():
                return
            if layer.fields().lookupField(field_name) == -1:
                return

            style = QgsStyle.defaultStyle()
            ramp = style.colorRamp("Reds") if style else None
            if ramp is None:
                return

            renderer = QgsGraduatedSymbolRenderer()
            renderer.setClassAttribute(field_name)
            renderer.setSourceColorRamp(ramp)

            renderer.updateClasses(layer, QgsGraduatedSymbolRenderer.Jenks, 5)
            renderer.updateColorRamp(ramp)

            layer.setRenderer(renderer)
            layer.triggerRepaint()
        except Exception:
            return
        
    def add_action(self, icon_path, text, callback, parent=None):
        icon = QIcon(icon_path)
        action = QAction(icon, text, parent)
        action.triggered.connect(callback)
        self.iface.addToolBarIcon(action)
        self.iface.addPluginToMenu(self.tr(u'&Geodiversity Calculator v2.0'), action)
        self.actions.append(action)
        return action
    
    def initGui(self):
        icon_path = os.path.join(self.plugin_dir, 'icon.png')
        self.add_action(icon_path, text=self.tr(u'Geodiversity Calculator v2.0'),
                       callback=self.run, parent=self.iface.mainWindow())
    
    def unload(self):
        for action in self.actions:
            self.iface.removePluginMenu(self.tr(u'&Geodiversity Calculator v2.0'), action)
            self.iface.removeToolBarIcon(action)
    
    def select_result_folder(self):
        folder = QFileDialog.getExistingDirectory(self.dlg, "Select result folder")
        if folder:
            self.dlg.lineEdit_16.setText(folder)
    
    def select_boundary(self):
        filename, _ = QFileDialog.getOpenFileName(
            self.dlg, "Select boundary", "", "Vector (*.gpkg *.shp)"
        )
        if filename:
            self.dlg.lineEdit.setText(filename)
            self._suggest_grid_spacing_from_boundary(filename)
    
    def select_geology(self):
        filename, _ = QFileDialog.getOpenFileName(
            self.dlg, "Select geology", "", "Vector (*.gpkg *.shp)"
        )
        if filename:
            self.dlg.lineEdit_5.setText(filename)
    
    def select_pedology(self):
        filename, _ = QFileDialog.getOpenFileName(
            self.dlg, "Select pedology", "", "Vector (*.gpkg *.shp)"
        )
        if filename:
            self.dlg.lineEdit_7.setText(filename)
    
    def select_dem(self):
        filename, _ = QFileDialog.getOpenFileName(
            self.dlg, "Select DEM", "", "Raster (*.tif)"
        )
        if filename:
            self.dlg.lineEdit_9.setText(filename)
    
    def select_lakes(self):
        filename, _ = QFileDialog.getOpenFileName(
            self.dlg, "Select lakes/seas", "", "Vector (*.gpkg *.shp)"
        )
        if filename:
            self.dlg.lineEdit_17.setText(filename)
    
    def select_mineral(self):
        filename, _ = QFileDialog.getOpenFileName(
            self.dlg, "Select mineralogy", "", "Vector (*.gpkg *.shp)"
        )
        if filename:
            self.dlg.lineEdit_12.setText(filename)
    
    def select_palaeo(self):
        filename, _ = QFileDialog.getOpenFileName(
            self.dlg, "Select palaeontology", "", "Vector (*.gpkg *.shp)"
        )
        if filename:
            self.dlg.lineEdit_13.setText(filename)
    
    # NOTE: No browse button for output grid - user just types the name!
    # The grid_name is read from lineEdit_14 and auto-saved to working_dir
    
    def clear_all(self):
        widgets = [
            self.dlg.lineEdit_16, self.dlg.lineEdit, self.dlg.lineEdit_3, self.dlg.lineEdit_4,
            self.dlg.lineEdit_5, self.dlg.lineEdit_6, self.dlg.lineEdit_7, self.dlg.lineEdit_8,
            self.dlg.lineEdit_9, self.dlg.lineEdit_17, self.dlg.lineEdit_12, self.dlg.lineEdit_10,
            self.dlg.lineEdit_13, self.dlg.lineEdit_11, self.dlg.lineEdit_14
        ]
        for widget in widgets:
            widget.clear()
    
    def run(self):
        if self.first_start:
            self.first_start = False
            self.dlg = GeodiversityCalculatorDialog()

            # Start with empty spacing; auto-fill happens when boundary is selected.
            try:
                self.dlg.lineEdit_3.clear()
                self.dlg.lineEdit_4.clear()
            except Exception:
                pass

            # Grid spacing autofill is enabled until user edits spacing fields.
            self._spacing_autofill = True
            try:
                self.dlg.lineEdit_3.textEdited.connect(lambda _t: setattr(self, "_spacing_autofill", False))
                self.dlg.lineEdit_4.textEdited.connect(lambda _t: setattr(self, "_spacing_autofill", False))
            except Exception:
                pass

            # Update spacing suggestion if boundary path is changed manually (no plugin reload needed).
            try:
                self.dlg.lineEdit.textChanged.connect(self._suggest_grid_spacing_from_boundary)
            except Exception:
                pass
            # Connect all browse buttons
            self.dlg.pushButton_14.clicked.connect(self.select_result_folder)
            self.dlg.pushButton.clicked.connect(self.select_boundary)
            self.dlg.pushButton_3.clicked.connect(self.select_geology)
            self.dlg.pushButton_5.clicked.connect(self.select_pedology)
            self.dlg.pushButton_7.clicked.connect(self.select_dem)
            self.dlg.pushButton_13.clicked.connect(self.select_lakes)
            self.dlg.pushButton_10.clicked.connect(self.select_mineral)
            self.dlg.pushButton_11.clicked.connect(self.select_palaeo)
            # No browse button for output grid - user types name in lineEdit_14
            self.dlg.button_box.button(QDialogButtonBox.Reset).clicked.connect(self.clear_all)

            # (boundary textChanged is already connected above)
        
        self.dlg.show()
        if self.dlg.exec_():
            self.execute()
    
    def execute(self):
        """Main execution"""
        progressbar = None
        try:
            # Initialize progress bar
            self.iface.messageBar().clearWidgets()
            progressbar = QProgressBar()
            self.iface.messageBar().pushWidget(progressbar)
            self._set_progress(progressbar, 0)
            
            # Get inputs
            working_dir = self.dlg.lineEdit_16.text().strip()
            boundary_path = self.dlg.lineEdit.text().strip()
            grid_name = self.dlg.lineEdit_14.text().strip()
            
            try:
                h_txt = (self.dlg.lineEdit_3.text() or '').strip()
                v_txt = (self.dlg.lineEdit_4.text() or '').strip()
                if not h_txt or not v_txt:
                    raise Exception('Grid spacing values are required (set manually or select a boundary for auto-fill).')
                h_spacing = float(h_txt)
                v_spacing = float(v_txt)
            except ValueError:
                raise Exception("Invalid grid spacing values!")
            
            geology_path = self.dlg.lineEdit_5.text().strip() or None
            geol_field = self.dlg.lineEdit_6.text().strip() or None
            pedology_path = self.dlg.lineEdit_7.text().strip() or None
            pedo_field = self.dlg.lineEdit_8.text().strip() or None
            dem_path = self.dlg.lineEdit_9.text().strip() or None
            lakes_path = self.dlg.lineEdit_17.text().strip() or None
            mineral_path = self.dlg.lineEdit_12.text().strip() or None
            mineral_field = self.dlg.lineEdit_10.text().strip() or None
            palaeo_path = self.dlg.lineEdit_13.text().strip() or None
            palaeo_field = self.dlg.lineEdit_11.text().strip() or None
            
            if not all([boundary_path, working_dir, grid_name]):
                raise Exception("Required fields missing!")
            
            self.log("Starting Geodiversity Calculator v2.0 analysis...", Qgis.Info)
            
            # STEP 1: Create Grid (3%)
            self.log("Creating analysis grid...", Qgis.Info)
            grid0 = working_dir + "/" + grid_name + ".gpkg"
            grid0_temp = None  # will use processing TEMPORARY_OUTPUT
            hatar0 = QgsVectorLayer(boundary_path, "boundary", "ogr")
            
            if not hatar0.isValid():
                raise Exception("Boundary layer is invalid!")
            
            QgsProject.instance().addMapLayer(hatar0)
            
            # Calculate adaptive resolution for huge datasets
            boundary_area_km2 = hatar0.extent().width() * hatar0.extent().height() / 1_000_000
            self.log(f"Boundary extent area: {boundary_area_km2:.0f} km²", Qgis.Info)
            
            # Suggest optimal grid size (H/V spacing) based on boundary extent area (km²)
            area = boundary_area_km2
            if area > 5_000_000:
                suggested_spacing = 50000
                self.log(f"HUGE dataset detected ({area:,.0f} km²). Consider using {suggested_spacing} m grid spacing.", Qgis.Warning)
            elif 1_000_000 < area <= 5_000_000:
                suggested_spacing = 20000
                self.log(f"Large dataset detected ({area:,.0f} km²). Consider using {suggested_spacing} m grid spacing.", Qgis.Warning)
            elif 100_000 < area <= 1_000_000:
                suggested_spacing = 10000
                self.log(f"Medium dataset detected ({area:,.0f} km²). Consider using {suggested_spacing} m grid spacing.", Qgis.Info)
            elif 50_000 < area <= 100_000:
                suggested_spacing = 5000
                self.log(f"Moderate dataset detected ({area:,.0f} km²). Consider using {suggested_spacing} m grid spacing.", Qgis.Info)
            elif 20_000 < area <= 50_000:
                suggested_spacing = 2500
                self.log(f"Small–moderate dataset detected ({area:,.0f} km²). Consider using {suggested_spacing} m grid spacing.", Qgis.Info)
            elif 5_000 < area <= 20_000:
                suggested_spacing = 1000
                self.log(f"Small dataset detected ({area:,.0f} km²). Consider using {suggested_spacing} m grid spacing.", Qgis.Info)
            elif 0 < area <= 5_000:
                suggested_spacing = 500
                self.log(f"Very small dataset detected ({area:,.0f} km²). Consider using {suggested_spacing} m grid spacing.", Qgis.Info)
            else:
                suggested_spacing = None

            crs0 = hatar0.crs().authid()
            extent0 = hatar0.extent()
            
            grid_type = 2
            try:
                if hasattr(self.dlg, 'radioButton_diamond') and self.dlg.radioButton_diamond.isChecked():
                    grid_type = 3
                elif hasattr(self.dlg, 'radioButton_hexagon') and self.dlg.radioButton_hexagon.isChecked():
                    grid_type = 4
                else:
                    grid_type = 2
            except Exception:
                grid_type = 2

            create_res = processing.run("native:creategrid", {
                'TYPE': grid_type,
                'EXTENT': f"{extent0.xMinimum()},{extent0.xMaximum()},{extent0.yMinimum()},{extent0.yMaximum()}",
                'HSPACING': h_spacing,
                'VSPACING': v_spacing,
                'CRS': crs0,
                'OUTPUT': 'TEMPORARY_OUTPUT'
            })
            grid0_temp = create_res.get('OUTPUT')
            
            self._set_progress(progressbar, 2)
            
            # STEP 1B: Select grid cells that intersect boundary (keep whole cells like original)
            self.log("Selecting grid cells that intersect boundary...", Qgis.Info)
            processing.run("native:extractbylocation", {
                'INPUT': grid0_temp,
                'PREDICATE': [0],  # intersect
                'INTERSECT': boundary_path,
                'OUTPUT': grid0
            })
            
            addGrid0 = QgsVectorLayer(grid0, grid_name, "ogr")
            if not addGrid0.isValid():
                raise Exception("Grid creation failed!")
            
            # Count grid cells
            grid_cell_count = addGrid0.featureCount()
            self.log(f"Grid created with {grid_cell_count} cells (whole cells that touch boundary)", Qgis.Info)
            
            self._set_progress(progressbar, 5)
            
            # STEP 2: Process Geology (20%)
            geol_layer = None
            if geology_path and geol_field:
                self.log("Processing geology data...", Qgis.Info)
                try:
                    layer11 = QgsVectorLayer(geology_path, "geology", "ogr")
                    if layer11.isValid():
                        QgsProject.instance().addMapLayer(layer11)
                        
                        # Add r_value field
                        newField11 = QgsField('r_value', QVariant.Int)
                        layer11.dataProvider().addAttributes([newField11])
                        layer11.updateFields()
                        
                        # Fill unique values
                        unique11 = []
                        with edit(layer11):
                            for feature in layer11.getFeatures():
                                if feature[geol_field] not in unique11:
                                    unique11.append(feature[geol_field])
                                new_value = unique11.index(feature[geol_field]) + 1
                                feature.setAttribute(feature.fieldNameIndex('r_value'), new_value)
                                layer11.updateFeature(feature)
                        
                        # Rasterize with adaptive resolution
                        raster11 = working_dir + "/geology_raster.tif"
                        extent11 = layer11.extent()
                        
                        # Adaptive pixel size based on grid spacing
                        pixel_size = max(5, h_spacing / 200)
                        
                        processing.run("gdal:rasterize", {
                            'INPUT': layer11,
                            'FIELD': 'r_value',
                            'UNITS': 1,
                            'WIDTH': pixel_size,
                            'HEIGHT': pixel_size,
                            'EXTENT': f"{extent11.xMinimum()},{extent11.xMaximum()},{extent11.yMinimum()},{extent11.yMaximum()}",
                            'NODATA': 0,
                            'DATA_TYPE': 5,
                            'OUTPUT': raster11
                        })
                        
                        addRaster11 = QgsRasterLayer(raster11, "geology_raster")
                        QgsProject.instance().addMapLayer(addRaster11)
                        
                        # Zonal statistics
                        output_grid31 = working_dir + "/geology_grid.gpkg"
                        processing.run("qgis:zonalstatisticsfb", {
                            'INPUT': grid0,
                            'INPUT_RASTER': raster11,
                            'RASTER_BAND': 1,
                            'COLUMN_PREFIX': '_geol_',
                            'STATISTICS': [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
                            'OUTPUT': output_grid31
                        })

                        # Cleanup: remove temporary field from the original geology layer
                        # (requested: delete 'r_value' once the geology subindex grid is created)
                        self._remove_field_if_exists(layer11, 'r_value')
                        
                        geol_layer = QgsVectorLayer(output_grid31, "geology_grid", "ogr")
                        geol_layer.dataProvider().deleteAttributes([6, 7, 8, 9, 10, 11, 12, 13, 14])
                        geol_layer.updateFields()
                        QgsProject.instance().addMapLayer(geol_layer)
                    
                except Exception as e:
                    self.log(f"Geology processing error (skipping): {str(e)}", Qgis.Warning)
            
            self._set_progress(progressbar, 20)
            
            # STEP 3: Process Pedology (35%)
            pedo_layer = None
            if pedology_path and pedo_field:
                self.log("Processing pedology data...", Qgis.Info)
                try:
                    layer12 = QgsVectorLayer(pedology_path, "pedology", "ogr")
                    if layer12.isValid():
                        QgsProject.instance().addMapLayer(layer12)
                        
                        newField12 = QgsField('r_value', QVariant.Int)
                        layer12.dataProvider().addAttributes([newField12])
                        layer12.updateFields()
                        
                        unique12 = []
                        with edit(layer12):
                            for feature in layer12.getFeatures():
                                if feature[pedo_field] not in unique12:
                                    unique12.append(feature[pedo_field])
                                new_value = unique12.index(feature[pedo_field]) + 1
                                feature.setAttribute(feature.fieldNameIndex('r_value'), new_value)
                                layer12.updateFeature(feature)
                        
                        raster12 = working_dir + "/pedology_raster.tif"
                        extent12 = layer12.extent()
                        
                        # Adaptive pixel size based on grid spacing
                        pixel_size = max(5, h_spacing / 200)
                        
                        processing.run("gdal:rasterize", {
                            'INPUT': layer12,
                            'FIELD': 'r_value',
                            'UNITS': 1,
                            'WIDTH': pixel_size,
                            'HEIGHT': pixel_size,
                            'EXTENT': f"{extent12.xMinimum()},{extent12.xMaximum()},{extent12.yMinimum()},{extent12.yMaximum()}",
                            'NODATA': 0,
                            'DATA_TYPE': 5,
                            'OUTPUT': raster12
                        })
                        
                        addRaster12 = QgsRasterLayer(raster12, "pedology_raster")
                        QgsProject.instance().addMapLayer(addRaster12)
                        
                        output_grid32 = working_dir + "/pedology_grid.gpkg"
                        processing.run("qgis:zonalstatisticsfb", {
                            'INPUT': grid0,
                            'INPUT_RASTER': raster12,
                            'RASTER_BAND': 1,
                            'COLUMN_PREFIX': '_pedo_',
                            'STATISTICS': [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
                            'OUTPUT': output_grid32
                        })

                        # Cleanup: remove temporary field from the original pedology layer
                        # (requested: delete 'r_value' once the pedology subindex grid is created)
                        self._remove_field_if_exists(layer12, 'r_value')
                        
                        pedo_layer = QgsVectorLayer(output_grid32, "pedology_grid", "ogr")
                        pedo_layer.dataProvider().deleteAttributes([6, 7, 8, 9, 10, 11, 12, 13, 14])
                        pedo_layer.updateFields()
                        QgsProject.instance().addMapLayer(pedo_layer)
                
                except Exception as e:
                    self.log(f"Pedology processing error (skipping): {str(e)}", Qgis.Warning)
            
            self._set_progress(progressbar, 35)
            
            # STEP 4: Process DEM - Geomorphon & Strahler (70%)
            geom_layer = None
            stra_layer = None
            if dem_path:
                self.log("Processing DEM (geomorphon & Strahler)...", Qgis.Info)
                try:
                    # Clip DEM
                    cut_dem2 = working_dir + "/cut_dem.tif"
                    crs2 = hatar0.crs().authid()
                    
                    try:
                        processing.run("gdal:cliprasterbymasklayer", {
                            'INPUT': dem_path,
                            'MASK': boundary_path,
                            'SOURCE_CRS': crs2,
                            'TARGET_CRS': crs2,
                            'CROP_TO_CUTLINE': True,
                            'DATA_TYPE': 0,
                            'OUTPUT': cut_dem2
                        })
                    except:
                        self.log("DEM clipping failed, trying alternative...", Qgis.Warning)
                        processing.run("gdal:warpreproject", {
                            'INPUT': dem_path,
                            'TARGET_CRS': crs2,
                            'OUTPUT': cut_dem2
                        })
                    
                    self._set_progress(progressbar, 45)
                    
                    # Geomorphon (CORRECT ALGORITHM!)
                    geom2 = working_dir + "/geomorphon.tif"
                    try:
                        processing.run("grass7:r.geomorphon", {
                            'elevation': cut_dem2,
                            'search': 3,
                            'skip': 0,
                            'flat': 3,
                            'dist': 0,
                            'forms': geom2,
                            '-m': False,
                            '-e': False
                        })
                        
                        addRaster2 = QgsRasterLayer(geom2, "geomorphon_raster")
                        QgsProject.instance().addMapLayer(addRaster2)
                        
                        output_grid33 = working_dir + "/geomorphon_grid.gpkg"
                        processing.run("qgis:zonalstatisticsfb", {
                            'INPUT': grid0,
                            'INPUT_RASTER': geom2,
                            'RASTER_BAND': 1,
                            'COLUMN_PREFIX': '_geom_',
                            'STATISTICS': [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
                            'OUTPUT': output_grid33
                        })
                        
                        geom_layer = QgsVectorLayer(output_grid33, "geomorphon_grid", "ogr")
                        geom_layer.dataProvider().deleteAttributes([6, 7, 8, 9, 10, 11, 12, 13, 14])
                        geom_layer.updateFields()
                        QgsProject.instance().addMapLayer(geom_layer)
                    
                    except Exception as e:
                        self.log(f"Geomorphon failed: {str(e)}", Qgis.Warning)
                    
                    self._set_progress(progressbar, 55)
                    
                    # Strahler (BULLETPROOF WITH FLOW DIRECTION & WATERSHED!)
                    try:
                        filled_output5 = working_dir + "/filled_dem.sdat"
                        flow_dir_output = 'TEMPORARY_OUTPUT'
                        watershed_output = 'TEMPORARY_OUTPUT'
                        
                        try:
                            self.log("Filling DEM sinks and calculating flow direction...", Qgis.Info)
                            processing.run("sagang:fillsinkswangliu", {
                                'ELEV': cut_dem2,
                                'FILLED': filled_output5,
                                'FDIR': flow_dir_output,
                                'WSHED': watershed_output,
                                'MINSLOPE': 0.1
                            })
                        except:
                            self.log("Wang & Liu fill failed, using simple fill...", Qgis.Warning)
                            processing.run("saga:fillsinks", {
                                'DEM': cut_dem2,
                                'RESULT': filled_output5
                            })
                        
                        self._set_progress(progressbar, 60)
                        
                        strahler_output5 = working_dir + "/strahler.sdat"
                        processing.run("sagang:strahlerorder", {
                            'DEM': filled_output5,
                            'STRAHLER': strahler_output5
                        })
                        
                        self._set_progress(progressbar, 65)
                        
                        output_grid5 = working_dir + "/strahler_grid.gpkg"
                        processing.run("qgis:zonalstatisticsfb", {
                            'INPUT': grid0,
                            'INPUT_RASTER': strahler_output5,
                            'RASTER_BAND': 1,
                            'COLUMN_PREFIX': '_stra_',
                            'STATISTICS': [6],
                            'OUTPUT': output_grid5
                        })
                        
                        stra_layer = QgsVectorLayer(output_grid5, "strahler_grid", "ogr")
                        stra_layer.updateFields()
                        QgsProject.instance().addMapLayer(stra_layer)
                        
                        # Divide Strahler by 2 (NULL-safe)
                        with edit(stra_layer):
                            for feature in stra_layer.getFeatures():
                                val = feature['_stra_max']
                                if val is None:
                                    val = 0
                                new_value = math.ceil(val / 2)
                                feature.setAttribute(feature.fieldNameIndex('_stra_max'), new_value)
                                stra_layer.updateFeature(feature)
                    
                    except Exception as e:
                        self.log(f"Strahler processing error (skipping): {str(e)}", Qgis.Warning)
                
                except Exception as e:
                    self.log(f"DEM processing error: {str(e)}", Qgis.Warning)
            
            self._set_progress(progressbar, 70)
            
            # STEP 5: Process Mineralogy (80%)
            mine_layer = None
            if mineral_path and mineral_field:
                self.log("Processing mineralogy data...", Qgis.Info)
                try:
                    layer61 = QgsVectorLayer(mineral_path, "mineral_occurences", "ogr")
                    if layer61.isValid():
                        QgsProject.instance().addMapLayer(layer61)
                        
                        newField61 = QgsField('r_value', QVariant.Int)
                        layer61.dataProvider().addAttributes([newField61])
                        layer61.updateFields()
                        
                        unique61 = []
                        with edit(layer61):
                            for feature in layer61.getFeatures():
                                if feature[mineral_field] not in unique61:
                                    unique61.append(feature[mineral_field])
                                new_value = unique61.index(feature[mineral_field]) + 1
                                feature.setAttribute(feature.fieldNameIndex('r_value'), new_value)
                                layer61.updateFeature(feature)
                        
                        mineral_grid = working_dir + '/mineral_grid.gpkg'
                        processing.run("native:countpointsinpolygon", {
                            'POLYGONS': grid0,
                            'POINTS': layer61,
                            'CLASSFIELD': 'r_value',
                            'FIELD': '_mineral_idx',
                            'OUTPUT': mineral_grid
                        })
                        
                        mine_layer = QgsVectorLayer(mineral_grid, 'mineral_grid', "ogr")
                        QgsProject.instance().addMapLayer(mine_layer)
                
                except Exception as e:
                    self.log(f"Mineralogy error (skipping): {str(e)}", Qgis.Warning)
            
            self._set_progress(progressbar, 80)
            
            # STEP 6: Process Palaeontology (85%)
            foss_layer = None
            if palaeo_path and palaeo_field:
                self.log("Processing palaeontology data...", Qgis.Info)
                try:
                    layer62 = QgsVectorLayer(palaeo_path, "fossil_occurences", "ogr")
                    if layer62.isValid():
                        QgsProject.instance().addMapLayer(layer62)
                        
                        newField62 = QgsField('r_value', QVariant.Int)
                        layer62.dataProvider().addAttributes([newField62])
                        layer62.updateFields()
                        
                        unique62 = []
                        with edit(layer62):
                            for feature in layer62.getFeatures():
                                if feature[palaeo_field] not in unique62:
                                    unique62.append(feature[palaeo_field])
                                new_value = unique62.index(feature[palaeo_field]) + 1
                                feature.setAttribute(feature.fieldNameIndex('r_value'), new_value)
                                layer62.updateFeature(feature)
                        
                        fossil_grid = working_dir + '/fossil_grid.gpkg'
                        processing.run("native:countpointsinpolygon", {
                            'POLYGONS': grid0,
                            'POINTS': layer62,
                            'CLASSFIELD': 'r_value',
                            'FIELD': '_fossil_idx',
                            'OUTPUT': fossil_grid
                        })
                        
                        foss_layer = QgsVectorLayer(fossil_grid, 'fossil_grid', "ogr")
                        QgsProject.instance().addMapLayer(foss_layer)
                
                except Exception as e:
                    self.log(f"Palaeontology error (skipping): {str(e)}", Qgis.Warning)
            
            self._set_progress(progressbar, 85)
            
            # STEP 7: Process Lakes (87%)
            if lakes_path:
                self.log("Processing lake/sea data...", Qgis.Info)
                try:
                    lakes7_input = QgsVectorLayer(lakes_path, "lakes", "ogr")
                    if lakes7_input.isValid():
                        newField7 = QgsField('_lakes', QVariant.Int)
                        addGrid0.dataProvider().addAttributes([newField7])
                        addGrid0.updateFields()
                        
                        selection = processing.run("native:selectbylocation", {
                            'INPUT': addGrid0,
                            'INTERSECT': lakes7_input,
                            'METHOD': 0,
                            'PREDICATE': [0]
                        })
                        
                        with edit(addGrid0):
                            for id in selection['OUTPUT'].selectedFeatureIds():
                                feature = addGrid0.getFeature(id)
                                feature['_lakes'] = 3
                                addGrid0.updateFeature(feature)
                        
                        addGrid0.removeSelection()
                
                except Exception as e:
                    self.log(f"Lakes error (skipping): {str(e)}", Qgis.Warning)
            
            self._set_progress(progressbar, 87)
            
            # STEP 8: Join Layers to Grid (90%)
            self.log("Joining thematic fields to grid...", Qgis.Info)
            grid = addGrid0
            
            # Join geology
            if geol_layer:
                try:
                    joinObject1 = QgsVectorLayerJoinInfo()
                    joinObject1.setJoinFieldName('id')
                    joinObject1.setTargetFieldName('id')
                    joinObject1.setJoinLayerId(geol_layer.id())
                    joinObject1.setUsingMemoryCache(True)
                    joinObject1.setJoinLayer(geol_layer)
                    joinObject1.setPrefix('J')
                    joinObject1.setJoinFieldNamesSubset(['_geol_variety'])
                    grid.addJoin(joinObject1)
                except Exception as e:
                    self.log(f"Geology join failed: {str(e)}", Qgis.Warning)
            
            # Join pedology
            if pedo_layer:
                try:
                    joinObject2 = QgsVectorLayerJoinInfo()
                    joinObject2.setJoinFieldName('id')
                    joinObject2.setTargetFieldName('id')
                    joinObject2.setJoinLayerId(pedo_layer.id())
                    joinObject2.setUsingMemoryCache(True)
                    joinObject2.setJoinLayer(pedo_layer)
                    joinObject2.setPrefix('J')
                    joinObject2.setJoinFieldNamesSubset(['_pedo_variety'])
                    grid.addJoin(joinObject2)
                except Exception as e:
                    self.log(f"Pedology join failed: {str(e)}", Qgis.Warning)
            
            # Join geomorphon
            if geom_layer:
                try:
                    joinObject3 = QgsVectorLayerJoinInfo()
                    joinObject3.setJoinFieldName('id')
                    joinObject3.setTargetFieldName('id')
                    joinObject3.setJoinLayerId(geom_layer.id())
                    joinObject3.setUsingMemoryCache(True)
                    joinObject3.setJoinLayer(geom_layer)
                    joinObject3.setPrefix('J')
                    joinObject3.setJoinFieldNamesSubset(['_geom_variety'])
                    grid.addJoin(joinObject3)
                except Exception as e:
                    self.log(f"Geomorphon join failed: {str(e)}", Qgis.Warning)
            
            # Join strahler
            if stra_layer:
                try:
                    joinObject4 = QgsVectorLayerJoinInfo()
                    joinObject4.setJoinFieldName('id')
                    joinObject4.setTargetFieldName('id')
                    joinObject4.setJoinLayerId(stra_layer.id())
                    joinObject4.setUsingMemoryCache(True)
                    joinObject4.setJoinLayer(stra_layer)
                    joinObject4.setPrefix('J')
                    joinObject4.setJoinFieldNamesSubset(['_stra_max'])
                    grid.addJoin(joinObject4)
                except Exception as e:
                    self.log(f"Strahler join failed: {str(e)}", Qgis.Warning)
            
            # Join mineralogy
            if mine_layer:
                try:
                    joinObject5 = QgsVectorLayerJoinInfo()
                    joinObject5.setJoinFieldName('id')
                    joinObject5.setTargetFieldName('id')
                    joinObject5.setJoinLayerId(mine_layer.id())
                    joinObject5.setUsingMemoryCache(True)
                    joinObject5.setJoinLayer(mine_layer)
                    joinObject5.setPrefix('J')
                    joinObject5.setJoinFieldNamesSubset(['_mineral_idx'])
                    grid.addJoin(joinObject5)
                except Exception as e:
                    self.log(f"Mineralogy join failed: {str(e)}", Qgis.Warning)
            
            # Join palaeontology
            if foss_layer:
                try:
                    joinObject6 = QgsVectorLayerJoinInfo()
                    joinObject6.setJoinFieldName('id')
                    joinObject6.setTargetFieldName('id')
                    joinObject6.setJoinLayerId(foss_layer.id())
                    joinObject6.setUsingMemoryCache(True)
                    joinObject6.setJoinLayer(foss_layer)
                    joinObject6.setPrefix('J')
                    joinObject6.setJoinFieldNamesSubset(['_fossil_idx'])
                    grid.addJoin(joinObject6)
                except Exception as e:
                    self.log(f"Palaeontology join failed: {str(e)}", Qgis.Warning)
            
            self._set_progress(progressbar, 90)
            
            # STEP 9: Delete NULL geology features (ONLY if geology was provided)
            if geol_layer:
                try:
                    with edit(grid):
                        features_to_delete = []
                        for feature in grid.getFeatures():
                            geol_val = feature['J_geol_variety']
                            if geol_val is None or geol_val == NULL:
                                features_to_delete.append(feature.id())
                        
                        if features_to_delete:
                            grid.deleteFeatures(features_to_delete)
                            self.log(f"Deleted {len(features_to_delete)} cells with NULL geology", Qgis.Info)
                except Exception as e:
                    self.log(f"NULL deletion warning: {str(e)}", Qgis.Warning)
            
            self._set_progress(progressbar, 93)

            # OPTIONAL: Normalize subindices (0-1) and write normalized fields
            try:
                do_norm = bool(getattr(self.dlg, "checkBox_normalize", None) and self.dlg.checkBox_normalize.isChecked())
            except Exception:
                do_norm = False

            if do_norm:
                self.log("Normalizing subindices (0-1)...", Qgis.Info)
                self._add_normalized_fields(grid)

            # STEP 10: Calculate Final GEODIVERSITY Index (97%)
            self.log("Calculating final geodiversity index...", Qgis.Info)
            prov = grid.dataProvider()
            newField8 = QgsField('_GEODIV', QVariant.Int)
            prov.addAttributes([newField8])
            grid.updateFields()
            
            idx = grid.fields().lookupField('_GEODIV')
            context = QgsExpressionContext()
            expression = QgsExpression(
                '(CASE WHEN "_lakes" IS NOT NULL THEN "_lakes" ELSE 0 END) + '
                '(CASE WHEN "J_geol_variety" IS NOT NULL THEN "J_geol_variety" ELSE 0 END) + '
                '(CASE WHEN "J_pedo_variety" IS NOT NULL THEN "J_pedo_variety" ELSE 0 END) + '
                '(CASE WHEN "J_geom_variety" IS NOT NULL THEN "J_geom_variety" ELSE 0 END) + '
                '(CASE WHEN "J_stra_max" IS NOT NULL THEN "J_stra_max" ELSE 0 END) + '
                '(CASE WHEN "J_mineral_idx" IS NOT NULL THEN "J_mineral_idx" ELSE 0 END) + '
                '(CASE WHEN "J_fossil_idx" IS NOT NULL THEN "J_fossil_idx" ELSE 0 END)'
            )
            
            scope = QgsExpressionContextScope()
            scope.setFields(grid.fields())
            context.appendScope(scope)
            expression.prepare(context)
            
            with edit(grid):
                for feature in grid.getFeatures():
                    context.setFeature(feature)
                    geodiv = expression.evaluate(context)
                    atts = {idx: geodiv}
                    grid.dataProvider().changeAttributeValues({feature.id(): atts})
            
            self._set_progress(progressbar, 97)
            
            # STEP 11: Add final grid to project & Generate Output Manifest
            QgsProject.instance().addMapLayer(addGrid0)

            # OPTIONAL: initial styling for output layer
            try:
                style_field = "N_sum" if ("do_norm" in locals() and do_norm) else "_GEODIV"
                self._apply_output_style(addGrid0, style_field)
            except Exception:
                pass
            
            # Generate output file manifest
            import os
            output_files = []
            manifest_path = working_dir + "/" + grid_name + "_MANIFEST.txt"
            
            for file in os.listdir(working_dir):
                if os.path.isfile(os.path.join(working_dir, file)):
                    file_size_mb = os.path.getsize(os.path.join(working_dir, file)) / (1024 * 1024)
                    output_files.append(f"{file} ({file_size_mb:.2f} MB)")
            
            with open(manifest_path, 'w', encoding='utf-8') as f:
                f.write("=" * 70 + "\n")
                f.write("GEODIVERSITY CALCULATOR v2.0 - OUTPUT FILE MANIFEST\n")
                f.write("=" * 70 + "\n\n")
                f.write(f"Analysis Date: {QDateTime.currentDateTime().toString('yyyy-MM-dd HH:mm:ss')}\n")
                f.write(f"Boundary Area: {boundary_area_km2:.0f} km²\n")
                f.write(f"Grid Spacing: {h_spacing}m x {v_spacing}m\n")
                f.write(f"Grid Cells: {grid_cell_count}\n")
                f.write(f"Working Directory: {working_dir}\n\n")
                f.write("=" * 70 + "\n")
                f.write("OUTPUT FILES:\n")
                f.write("=" * 70 + "\n\n")
                for file in sorted(output_files):
                    f.write(f"  • {file}\n")
                f.write("\n" + "=" * 70 + "\n")
                f.write("ANALYSIS COMPONENTS:\n")
                f.write("=" * 70 + "\n\n")
                if geology_path:
                    f.write("  ✓ Geology (variety)\n")
                if pedology_path:
                    f.write("  ✓ Pedology (variety)\n")
                if dem_path:
                    f.write("  ✓ DEM (geomorphon + Strahler stream order)\n")
                    f.write("  ✓ Flow direction & watershed\n")
                if lakes_path:
                    f.write("  ✓ Lakes/water bodies\n")
                if mineral_path:
                    f.write("  ✓ Mineralogy (occurrence diversity)\n")
                if palaeo_path:
                    f.write("  ✓ Palaeontology (fossil diversity)\n")
                f.write("\n" + "=" * 70 + "\n")
                f.write(f"Final Grid: {grid_name}.gpkg\n")
                f.write(f"Total Files: {len(output_files)}\n")
                f.write("=" * 70 + "\n")
            
            self.log(f"Output manifest saved: {manifest_path}", Qgis.Info)
            self._set_progress(progressbar, 100)
            
            self.iface.messageBar().pushMessage(
                "Success",
                f"GeoDiversity v2.0 completed! {grid_cell_count} cells, {len(output_files)} files created. See {grid_name}_MANIFEST.txt",
                level=Qgis.Success,
                duration=20
            )
            
            self.log("Analysis complete!", Qgis.Info)
        
        except Exception as e:
            error_msg = f"GeoDiversity v2.0 Error: {str(e)}"
            self.log(error_msg, Qgis.Critical)
            self.log(traceback.format_exc(), Qgis.Critical)
            self.iface.messageBar().pushMessage("Error", error_msg, level=Qgis.Critical, duration=15)
        
        finally:
            if progressbar:
                self.iface.messageBar().clearWidgets()
