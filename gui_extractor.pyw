import sys
import os
from pathlib import Path

# 屏蔽 PyQt 大量字体报错导致的 stderr 输出卡死
sys.stderr = open(os.devnull, 'w')

from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, 
    QPushButton, QMessageBox, QFileDialog, QProgressBar, QLineEdit, QComboBox, QCheckBox
)
from PyQt6.QtGui import QFont, QDragEnterEvent, QDropEvent
from PyQt6.QtCore import Qt, QThread, pyqtSignal

try:
    from coordinate_extractor import extract_slot, get_coordinate_count, parse_character_card
except ImportError:
    QMessageBox.critical(None, "导入错误", "找不到 coordinate_extractor.py，请确保文件在同一目录下。")
    sys.exit(1)

class ExtractWorker(QThread):
    progress = pyqtSignal(int, str)
    finished = pyqtSignal(list, list, list)
    started_info = pyqtSignal(int)
    
    def __init__(self, file_path, output_dir, skip_accessories=(), bust_abm="keep",
                 generic_plugins=True):
        super().__init__()
        self.file_path = file_path
        self.output_dir = output_dir
        self.skip_accessories = skip_accessories
        self.bust_abm = bust_abm
        self.generic_plugins = generic_plugins

    def run(self):
        success_files = []
        errors = []
        warnings = []
        
        try:
            card = parse_character_card(self.file_path)
            total = get_coordinate_count(card)
        except Exception as e:
            self.finished.emit([], [f"读取人物卡失败: {e}"], [])
            return

        self.started_info.emit(total)
        for slot in range(total):
            self.progress.emit(slot + 1, f"正在提取 Slot {slot} / {total - 1}...")
            try:
                out_path = extract_slot(self.file_path, slot, output_dir=self.output_dir,
                                        skip_accessories=self.skip_accessories,
                                        bust_abm=self.bust_abm,
                                        generic_plugins=self.generic_plugins,
                                        warnings=warnings)
                success_files.append(out_path)
            except Exception as e:
                errors.append(f"Slot {slot}: {str(e)}")
        
        self.finished.emit(success_files, errors, warnings)

class ExtractorGUI(QWidget):
    def __init__(self):
        super().__init__()
        # 获取程序所在目录
        self.default_out_dir = str(Path(__file__).parent.resolve())
        self.initUI()
        
    def initUI(self):
        self.setWindowTitle('Koikatu 服装卡提取器')
        self.resize(500, 480)
        self.setAcceptDrops(True)
        
        main_layout = QVBoxLayout()
        
        # 1. 拖拽区域
        self.drop_label = QLabel("将人物卡 (.png) 拖拽到此处\n或点击下方按钮选择")
        self.drop_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.drop_label.setStyleSheet("""
            QLabel {
                border: 2px dashed #aaa;
                border-radius: 10px;
                padding: 40px;
                font-size: 16px;
                color: #666;
                background-color: #f9f9f9;
            }
        """)
        main_layout.addWidget(self.drop_label)
        
        # 2. 选择文件按钮
        self.btn_select = QPushButton("选择人物卡")
        self.btn_select.clicked.connect(self.select_file)
        main_layout.addWidget(self.btn_select)
        
        # 3. 输出路径选择区域
        out_layout = QHBoxLayout()
        out_layout.addWidget(QLabel("输出目录:"))
        
        self.out_path_edit = QLineEdit()
        self.out_path_edit.setText(self.default_out_dir)
        out_layout.addWidget(self.out_path_edit)
        
        self.btn_browse = QPushButton("浏览...")
        self.btn_browse.clicked.connect(self.browse_output_dir)
        out_layout.addWidget(self.btn_browse)
        
        main_layout.addLayout(out_layout)

        # 3.5 可选：强制清空的饰品槽
        skip_layout = QHBoxLayout()
        skip_layout.addWidget(QLabel("跳过饰品槽:"))
        self.skip_acc_edit = QLineEdit()
        self.skip_acc_edit.setPlaceholderText("可留空；例如 0 或 0,3（人物卡比服装卡多饰品时用）")
        skip_layout.addWidget(self.skip_acc_edit)
        main_layout.addLayout(skip_layout)

        # 3.6 胸骨 ABM 处理方式
        bust_layout = QHBoxLayout()
        bust_layout.addWidget(QLabel("胸骨 ABM:"))
        self.bust_combo = QComboBox()
        self.bust_combo.addItem("保持原版（推荐，遵循游戏机制）", "keep")
        self.bust_combo.addItem("胸骨归一化 1.0（穿衣变大，脱衣也变大）", "reset")
        self.bust_combo.addItem("删除胸骨（穿衣变大，脱衣也变大）", "remove")
        bust_layout.addWidget(self.bust_combo)
        main_layout.addLayout(bust_layout)

        # 3.7 新坐标插件通用提取
        self.generic_cb = QCheckBox("通用提取规则库中的新坐标插件（建议开启）")
        self.generic_cb.setChecked(True)
        self.generic_cb.setToolTip(
            "对 plugin_catalog.json 中标记为“人物级/服装级”、但尚未显式实现的新插件，"
            "尝试按坐标键/Coordinate 字段做通用提取；纯人物级插件仍然丢弃。")
        main_layout.addWidget(self.generic_cb)
        
        # 4. 进度条
        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(False)
        main_layout.addWidget(self.progress_bar)
        
        self.setLayout(main_layout)

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.accept()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent):
        urls = event.mimeData().urls()
        if urls:
            file_path = urls[0].toLocalFile()
            if file_path.lower().endswith('.png'):
                self.start_extraction(file_path)
            else:
                QMessageBox.warning(self, "格式错误", "请拖入 .png 格式的人物卡")

    def browse_output_dir(self):
        """选择输出目录"""
        dir_path = QFileDialog.getExistingDirectory(self, "选择输出目录", self.out_path_edit.text())
        if dir_path:
            self.out_path_edit.setText(dir_path)

    def select_file(self):
        """选择人物卡文件"""
        file_path, _ = QFileDialog.getOpenFileName(self, "选择人物卡", "", "Koikatu 人物卡 (*.png)")
        if file_path:
            self.start_extraction(file_path)

    def start_extraction(self, file_path):
        # 检查输出路径是否为空，为空则退回默认路径
        out_dir = self.out_path_edit.text().strip()
        if not out_dir:
            out_dir = self.default_out_dir
            self.out_path_edit.setText(out_dir)

        skip_text = self.skip_acc_edit.text().strip()
        skip_accessories = []
        if skip_text:
            try:
                skip_accessories = [int(x) for x in skip_text.replace("，", ",").split(",") if x.strip()]
            except ValueError:
                QMessageBox.warning(self, "输入错误", "跳过饰品槽只能填数字，用逗号分隔，例如：0 或 0,3")
                return
            
        # 自动创建不存在的目录
        if not os.path.exists(out_dir):
            try:
                os.makedirs(out_dir)
            except Exception as e:
                QMessageBox.critical(self, "错误", f"无法创建输出目录:\n{e}")
                return

        self.btn_select.setEnabled(False)
        self.btn_browse.setEnabled(False)
        self.drop_label.setText("正在提取，请稍候...")
        self.progress_bar.setVisible(True)
        self.progress_bar.setMaximum(1)
        self.progress_bar.setValue(0)
        
        self.worker = ExtractWorker(file_path, out_dir, skip_accessories,
                                    bust_abm=self.bust_combo.currentData(),
                                    generic_plugins=self.generic_cb.isChecked())
        self.worker.started_info.connect(self.on_started)
        self.worker.progress.connect(self.update_progress)
        self.worker.finished.connect(self.on_finished)
        self.worker.start()

    def on_started(self, total):
        self.progress_bar.setMaximum(total)
        self.progress_bar.setValue(0)

    def update_progress(self, value, text):
        self.progress_bar.setValue(value)
        self.drop_label.setText(text)

    def on_finished(self, success_files, errors, warnings):
        self.btn_select.setEnabled(True)
        self.btn_browse.setEnabled(True)
        self.progress_bar.setVisible(False)
        self.drop_label.setText("将人物卡 (.png) 拖拽到此处\n或点击下方按钮选择")
        
        msg = f"成功提取 {len(success_files)} 套服装！\n"
        msg += f"已保存至:\n{self.out_path_edit.text()}"
        
        if warnings:
            relevant = [w for w in dict.fromkeys(warnings)
                        if "character，已丢弃" not in w]
            if relevant:
                shown = relevant[:20]
                msg += "\n\n插件处理提示:\n" + "\n".join(shown)
                if len(relevant) > len(shown):
                    msg += f"\n...（共 {len(relevant)} 条，完整内容见控制台）"
        if errors:
            msg += "\n\n部分失败:\n" + "\n".join(errors)
            QMessageBox.warning(self, "完成", msg)
        else:
            QMessageBox.information(self, "完成", msg)

if __name__ == '__main__':
    app = QApplication(sys.argv)
    safe_font = QFont("Microsoft YaHei", 9)
    app.setFont(safe_font)
    
    window = ExtractorGUI()
    window.show()
    
    sys.exit(app.exec())