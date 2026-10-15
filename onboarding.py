"""First-use walkthrough. Draft settings stay separate from live snapshots."""
from pathlib import Path
import queue
import threading

from PySide6.QtCore import Qt, Signal, QObject, QTimer
from theme_redesign import SURFACES as THEME_SURFACES, neutralize_legacy_css
from ui_controls import scrollbar_style
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QFileDialog, QFrame, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QScrollArea, QSpinBox, QStackedWidget,
    QVBoxLayout, QWidget,
)


GUIDE_SEEN_KEY = 'onboarding/quickStartSeen'
GUIDE_COMPLETE_KEY = 'onboarding/quickStartCompleted'
GUIDE_VERSION = '2'


def _run_directory_search(finder, cancellable, cancelled, results):
    """The filesystem thread owns no Qt objects, including its result sink."""
    try:
        found = finder(cancelled.is_set) if cancellable else finder()
        result, error = (str(found) if found else None), ''
    except Exception as exception:
        result, error = None, str(exception)
    if not cancelled.is_set():
        results.put((result, error))


class DirectorySearch(QObject):
    """Keep slow launcher scans and their lifetime separate from GUI objects."""
    finished = Signal()

    def __init__(self, finder, parent=None, *, cancellable=False):
        super().__init__(parent)
        self.finder = finder
        self.cancellable = cancellable
        self.result = None
        self.error = ''
        self._running = False
        self._cancelled = threading.Event()
        self._results = queue.SimpleQueue()
        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._poll_result)
        # Destruction can interrupt further traversal even when a single
        # filesystem call is still blocked. No worker signal targets Qt.
        cancelled = self._cancelled
        self.destroyed.connect(lambda *_args: cancelled.set())

    def start(self):
        if self._running:
            return
        self._running = True
        threading.Thread(
            target=_run_directory_search,
            args=(self.finder, self.cancellable, self._cancelled, self._results),
            name='ZJ-HUB-directory-search', daemon=True,
        ).start()
        self._timer.start()

    def isRunning(self):
        return self._running

    def isInterruptionRequested(self):
        return self._cancelled.is_set()

    def requestInterruption(self):
        self._cancelled.set()
        self._running = False
        self._timer.stop()

    def _poll_result(self):
        try:
            result, error = self._results.get_nowait()
        except queue.Empty:
            return
        self._timer.stop()
        self._running = False
        if not self._cancelled.is_set():
            self.result, self.error = result, error
            self.finished.emit()


class QuickStartDialog(QDialog):
    settingsRequested = Signal()
    saveRequested = Signal(dict)
    findRequested = Signal()
    recordingRequested = Signal()

    def __init__(self, parent=None, theme='light'):
        super().__init__(parent)
        self.setWindowTitle('ZJ HUB · 使用引导')
        self.setObjectName('quickStartGuide')
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setSizeGripEnabled(True)
        self.resize(750, 670)
        self.setMinimumSize(560, 460)
        screen = self.screen() or QApplication.primaryScreen()
        if screen:
            area = screen.availableGeometry()
            self.setMinimumSize(min(560, max(320, area.width() - 40)), min(460, max(280, area.height() - 60)))
            self.resize(min(750, area.width() - 40), min(670, area.height() - 60))
        self._loaded = False
        self._loading = False
        self._saving = False
        self._finding = False
        self._saved = False
        self._rate_locked = False
        self._submitted = None
        self._target_at_search = ''
        self._apply_style(theme)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 20)
        outer.setSpacing(14)
        header = QHBoxLayout()
        brand = self._label('ZJ HUB  /  使用引导', 'eyebrow')
        header.addWidget(brand, 1)
        self.skip_button = self._button('暂时跳过', self.reject)
        header.addWidget(self.skip_button)
        outer.addLayout(header)
        self.steps = []
        progress = QHBoxLayout()
        for index, name in enumerate(('认识流程', '选择目录', '开始录制', '回放与皮肤')):
            item = self._label(f'{index + 1}  {name}', 'step')
            item.setAlignment(Qt.AlignmentFlag.AlignCenter)
            progress.addWidget(item, 1)
            self.steps.append(item)
        outer.addLayout(progress)
        self.stack = QStackedWidget()
        outer.addWidget(self.stack, 1)
        self._welcome_page()
        self._directory_page()
        self._recording_page()
        self._replay_page()
        self.status = self._label('', 'status')
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        outer.addWidget(self.status)
        footer = QHBoxLayout()
        self.back_button = self._button('上一步', self.previous)
        footer.addWidget(self.back_button)
        self.later_button = self._button('稍后配置，先看教程', self.skip_directories)
        footer.addWidget(self.later_button)
        footer.addStretch(1)
        self.next_button = self._button('开始引导', self.next)
        self.next_button.setObjectName('guidePrimary')
        self.next_button.setDefault(True)
        footer.addWidget(self.next_button)
        outer.addLayout(footer)
        self._show_step(0)

    @staticmethod
    def _label(text, role='body'):
        item = QLabel(text)
        item.setWordWrap(True)
        item.setProperty('guideRole', role)
        item.setTextFormat(Qt.TextFormat.PlainText)
        return item

    @staticmethod
    def _button(text, callback):
        item = QPushButton(text)
        item.setCursor(Qt.CursorShape.PointingHandCursor)
        item.setMinimumHeight(36)
        item.setAutoDefault(False)
        item.clicked.connect(callback)
        return item

    def _page(self, title, subtitle):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(2, 8, 10, 8)
        layout.setSpacing(14)
        layout.addWidget(self._label(title, 'title'))
        layout.addWidget(self._label(subtitle, 'muted'))
        scroll.setWidget(content)
        self.stack.addWidget(scroll)
        return layout

    def _card(self, layout, title, text):
        card = QFrame()
        card.setObjectName('guideCard')
        box = QVBoxLayout(card)
        box.setContentsMargins(16, 13, 16, 13)
        box.setSpacing(6)
        box.addWidget(self._label(title, 'heading'))
        box.addWidget(self._label(text))
        layout.addWidget(card)

    def _welcome_page(self):
        box = self._page('准备好你的第一段回放', '花一点时间选好目录，录制、导出和换电脑播放都会更顺手。')
        self._card(box, '01  先选三个设置', '保存目录存放原始录制和 ZIP；回放目录是安装了 Flashback 的游戏实例；采样频率决定每秒读取次数，默认 20 tick/s。')
        self._card(box, '02  录制，再导出', '在 Minecraft 1.20.1 中进入世界后注入组件，选择 20～120 tick/s，再开始录制；生成的回放交给 Minecraft 1.21.11 的 Flashback 播放。')
        self._card(box, '03  把回放、皮肤和日志一起带走', '回放列表使用“安装并打开”。皮肤保存在所选播放器版本的 flashback/recorded_skins 文件夹；ZJ HUB 的临时缓存关闭后会清理。')
        box.addStretch(1)

    def _directory_page(self):
        box = self._page('两个目录，各有用途', '现在可以直接配置；不会启动游戏，也不会开始录制。')
        box.addWidget(self._label('录制保存目录', 'heading'))
        box.addWidget(self._label('存放录制原文件和导出的 ZIP；新文件夹会自动创建。', 'muted'))
        row = QHBoxLayout()
        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText('选择保存位置')
        self.output_edit.setAccessibleName('录制保存目录')
        row.addWidget(self.output_edit, 1)
        self.output_browse = self._button('选择', lambda: self._choose_directory(self.output_edit))
        row.addWidget(self.output_browse)
        box.addLayout(row)
        self.output_hint = self._label('', 'muted')
        box.addWidget(self.output_hint)
        box.addWidget(self._label('Flashback 回放游戏目录（可选）', 'heading'))
        box.addWidget(self._label('选择内含 mods 的 1.21.11 游戏实例根目录，不要选 mods 或 replays 子文件夹。', 'muted'))
        row = QHBoxLayout()
        self.target_edit = QLineEdit()
        self.target_edit.setPlaceholderText('留空：只保存 ZIP，稍后再安装回放')
        self.target_edit.setClearButtonEnabled(True)
        self.target_edit.setAccessibleName('Flashback 回放游戏目录')
        row.addWidget(self.target_edit, 1)
        self.target_browse = self._button('选择', lambda: self._choose_directory(self.target_edit))
        row.addWidget(self.target_browse)
        self.find_button = self._button('自动查找', self.find_directory)
        row.addWidget(self.find_button)
        box.addLayout(row)
        box.addWidget(self._label('多个启动器实例时，请选择你真正用于播放回放的那个。留空会关闭自动安装回放和皮肤。', 'muted'))
        self.auto_zip = QCheckBox('停止录制后自动生成回放 ZIP')
        box.addWidget(self.auto_zip)
        box.addWidget(self._label('录制采样频率', 'heading'))
        box.addWidget(self._label('20 tick/s 是最低且最稳的设置；想提高运动细节可选 40～120。只允许在开始录制前修改，开始后本段频率固定。', 'muted'))
        rate_row = QHBoxLayout()
        self.rate_spin = QSpinBox()
        self.rate_spin.setRange(20, 120)
        self.rate_spin.setSingleStep(10)
        self.rate_spin.setValue(20)
        self.rate_spin.setSuffix(' tick/s')
        self.rate_spin.setAccessibleName('录制采样频率')
        rate_row.addWidget(self.rate_spin)
        rate_row.addStretch(1)
        box.addLayout(rate_row)
        self.rate_hint = self._label('', 'muted')
        box.addWidget(self.rate_hint)
        self.retry_button = self._button('重新读取当前设置', self.load_settings)
        box.addWidget(self.retry_button)
        box.addStretch(1)

    def _recording_page(self):
        box = self._page('从一小段录制开始', '完成引导后，点击“进入录制页”按以下顺序操作。')
        self._card(box, '1  进入世界，选择进程', '打开 Minecraft 1.20.1 并进入世界。在 Flashback 录制页点“刷新进程”，选择正在玩的游戏。')
        self._card(box, '2  注入组件，设置频率，再开始', '点击“注入录制组件”，等待组件就绪。在录制页把采样频率改为需要的 20～120 tick/s，点击“保存设置”，再点“开始录制”。录制开始后不能修改本段频率。')
        self._card(box, '3  停止后，等导出完成', '点击“停止录制”。勾选自动生成 ZIP 后，等待日志提示导出完成；也可以点击“导出最近录制”。')
        self._card(box, '4  出现错误先看两处', '设置 → 日志可打开 ZJ HUB 日志和 Flashback 引擎日志。若提示组件过旧或运行时被占用，完全退出 Minecraft 后重新打开，再注入一次。')
        box.addWidget(self._label('更新软件后，如果提示游戏内录制组件过旧，请先重启游戏再重新注入。', 'muted'))
        box.addStretch(1)

    def _replay_page(self):
        box = self._page('让回放里的皮肤也跟过来', '换电脑后最关键的一步：用 ZJ HUB 安装回放到正确的游戏实例。')
        self._card(box, '换电脑的三个步骤', '① 在新电脑安装匹配 1.21.11 的 Flashback。\n② 重新选择这台电脑的回放游戏目录。\n③ 退出正在播放的回放，再点“安装已有回放”选择导出的 ZIP。')
        self._card(box, '皮肤文件夹会自动创建', '回放 ZIP 中保留录制皮肤，安装时复制到当前播放器版本的 flashback/recorded_skins，回放引用这里的副本；仅手动打开 ZIP 不等于完成皮肤安装。')
        self._card(box, '仍然看不到皮肤？', '先核对游戏实例，再查看“设置 → 日志”。手工皮肤覆盖会优先保留；如果它还指向旧电脑，请在 Flashback 中清除该覆盖后重新安装。损坏或未录到的皮肤无法自动补回。')
        self._card(box, '清理缓存后怎么恢复？', 'ZJ HUB 关闭后会清理自己的临时皮肤缓存；播放器里的已安装皮肤保留。下次重新导出或安装回放会重建缓存，不会删除录制文件和已有编辑状态。')
        self.summary = self._label('', 'muted')
        box.addWidget(self.summary)
        box.addStretch(1)

    def _apply_style(self, theme):
        dark = theme != 'light'
        bg, card = ('#142131', '#1d2e43') if dark else ('#f5f7fb', '#ffffff')
        fg, muted = ('#eff5ff', '#c0cfe1') if dark else ('#172d44', '#536b82')
        border = '#435871' if dark else '#d2ddea'
        accent = '#285f9a' if dark else '#245c98'
        if theme in THEME_SURFACES:
            surface = THEME_SURFACES[theme]
            bg, card, fg, muted, border = (surface[name] for name in ('page', 'card', 'text', 'muted', 'border'))
            accent = surface['focus'] if theme == 'blue' else '#626262'
        style = f'''
QDialog#quickStartGuide {{ background: {bg}; color: {fg}; }}
QDialog#quickStartGuide QWidget {{ font-family: "Microsoft YaHei UI"; font-size: 13px; }}
QDialog#quickStartGuide QLabel {{ color: {fg}; background: transparent; border: none; padding: 0; }}
QDialog#quickStartGuide QLabel[guideRole="title"] {{ font-size: 25px; font-weight: 700; }}
QDialog#quickStartGuide QLabel[guideRole="heading"] {{ font-size: 15px; font-weight: 600; }}
QDialog#quickStartGuide QLabel[guideRole="muted"], QDialog#quickStartGuide QLabel[guideRole="eyebrow"] {{ color: {muted}; }}
QDialog#quickStartGuide QLabel[guideRole="step"] {{ background: {card}; border: 1px solid {border}; border-radius: 8px; padding: 9px 2px; font-size: 12px; }}
QDialog#quickStartGuide QLabel[guideRole="step"][active="true"] {{ background: {accent}; color: #ffffff; border-color: {accent}; }}
QDialog#quickStartGuide QFrame#guideCard {{ background: {card}; border: 1px solid {border}; border-radius: 12px; }}
QDialog#quickStartGuide QScrollArea, QDialog#quickStartGuide QScrollArea > QWidget > QWidget {{ background: {bg}; border: none; }}
QDialog#quickStartGuide QLineEdit {{ background: {card}; color: {fg}; border: 1px solid {border}; border-radius: 7px; padding: 9px; min-height: 20px; }}
QDialog#quickStartGuide QLineEdit:focus {{ border-color: #438bda; }}
QDialog#quickStartGuide QPushButton {{ background: {card}; color: {fg}; border: 1px solid {border}; border-radius: 8px; padding: 6px 12px; }}
QDialog#quickStartGuide QPushButton:hover {{ border-color: #438bda; }}
QDialog#quickStartGuide QPushButton#guidePrimary {{ background: {accent}; color: #ffffff; border-color: {accent}; font-weight: 600; }}
QDialog#quickStartGuide QPushButton:disabled {{ color: {muted}; background: {bg}; }}
QDialog#quickStartGuide QCheckBox {{ color: {fg}; spacing: 8px; }}
QDialog#quickStartGuide QLabel[guideRole="status"] {{ color: {muted}; }}
'''
        self.setStyleSheet(neutralize_legacy_css(style, theme) +
                          (scrollbar_style(theme) if theme in THEME_SURFACES else ''))

    def _show_step(self, index):
        self.stack.setCurrentIndex(index)
        self.status.clear()
        for number, item in enumerate(self.steps):
            item.setProperty('active', number == index)
            item.style().unpolish(item)
            item.style().polish(item)
        self.back_button.setVisible(index > 0)
        self.later_button.setVisible(index == 1)
        self.next_button.setText(('开始引导', '保存并继续', '下一步', '进入录制页')[index])
        self.summary.setText('目录设置已保存。可以开始录制了。' if self._saved else '目录暂未在本次引导中保存，可在录制页继续配置。')
        self._update_enabled()
        if index == 1 and not self._loaded and not self._loading:
            self.load_settings()

    def load_settings(self):
        if self._saving:
            return
        self._loading = True
        self.status.setText('正在读取当前目录设置…')
        self._update_enabled()
        self.settingsRequested.emit()

    def settings_loaded(self, settings, controls=None):
        if not self._loading:
            return
        self._loading = False
        self._loaded = True
        self.output_edit.setText(str(settings.get('output') or ''))
        self.target_edit.setText(str(settings.get('flashbackDirectory') or ''))
        self.auto_zip.setChecked(bool(settings.get('autoZip', True)))
        try:
            self.rate_spin.setValue(max(20, min(120, int(settings.get('readRate') or 20))))
        except (TypeError, ValueError):
            self.rate_spin.setValue(20)
        locked = (controls or {}).get('output') is False
        self._rate_locked = (controls or {}).get('rate') is False
        self.output_edit.setReadOnly(locked)
        self.output_browse.setVisible(not locked)
        self.output_hint.setText('已有活动录制会话，保存目录暂时不能修改。可保持目录继续。' if locked else '')
        self.rate_hint.setText('本段已经开始，采样频率已锁定；停止后重新设置。' if self._rate_locked else '当前设置会在下一段录制开始时生效。')
        if self.stack.currentIndex() == 1:
            self.status.setText('已读取当前设置。回放目录可以留空。')
        self._update_enabled()

    def operation_failed(self, message):
        self._loading = self._saving = False
        self.status.setText(str(message))
        self._update_enabled()

    @staticmethod
    def _norm_setting_path(value):
        text = str(value or '').strip()
        if not text:
            return ''
        return os.path.normcase(os.path.normpath(text))

    def saved(self, settings):
        if not self._saving:
            return
        expected = self._submitted or {}
        actual_match = (
            self._norm_setting_path(settings.get('output')) == self._norm_setting_path(expected.get('output'))
            and self._norm_setting_path(settings.get('flashbackDirectory')) == self._norm_setting_path(expected.get('target'))
            and bool(settings.get('autoZip')) == bool(expected.get('autoZip'))
            and int(settings.get('readRate') or 20) == int(expected.get('rate') or 20)
        )
        if not actual_match:
            self.operation_failed('引擎返回的目录设置与本次提交不一致，请重新保存。')
            return
        self._saving = False
        self._saved = True
        self._show_step(2)

    def find_directory(self):
        if self._finding:
            return
        self._finding = True
        self._target_at_search = self.target_edit.text()
        self.status.setText('正在查找常见启动器的 Flashback 实例…')
        self._update_enabled()
        self.findRequested.emit()

    def directory_found(self, path, error=''):
        self._finding = False
        if error:
            self.status.setText('自动查找失败，请点击“选择”指定游戏目录。')
        elif path and self.target_edit.text() == self._target_at_search:
            self.target_edit.setText(path)
            self.status.setText('已找到一个 Flashback 实例，请确认它是实际用于播放回放的实例。')
        else:
            self.status.setText('未找到唯一的 Flashback 实例，或目录已被修改。请点击“选择”手动指定。')
        self._update_enabled()

    def _choose_directory(self, field):
        initial = Path(field.text().strip()).expanduser() if field.text().strip() else Path.home()
        try:
            if not initial.is_dir():
                initial = initial.parent if initial.parent.is_dir() else Path.home()
        except (OSError, ValueError):
            initial = Path.home()
        selected = QFileDialog.getExistingDirectory(self, '选择文件夹', str(initial))
        if selected:
            field.setText(selected)

    def _update_enabled(self):
        editable = self._loaded and not self._loading and not self._saving
        for widget in (self.output_edit, self.output_browse, self.target_edit, self.target_browse, self.auto_zip):
            widget.setEnabled(editable and not self._finding)
        self.rate_spin.setEnabled(editable and not self._finding and not self._rate_locked)
        self.find_button.setEnabled(editable and not self._finding)
        self.retry_button.setVisible(not self._loaded and not self._loading)
        self.next_button.setEnabled(not self._saving and (self.stack.currentIndex() != 1 or (editable and not self._finding)))
        self.back_button.setEnabled(not self._saving)
        self.skip_button.setEnabled(not self._saving)
        self.later_button.setEnabled(not self._saving)

    def next(self):
        index = self.stack.currentIndex()
        if self._saving:
            return
        if index == 1:
            if not self._loaded or self._loading or self._finding:
                return
            payload = {}
            try:
                for key, field in (('output', self.output_edit), ('target', self.target_edit)):
                    value = field.text().strip()
                    if not value and key == 'target':
                        payload[key] = ''
                        continue
                    path = Path(value).expanduser()
                    if not value or not path.is_absolute():
                        raise ValueError('请通过“选择”指定完整的保存或回放目录。')
                    payload[key] = str(path.resolve())
            except (OSError, ValueError, RuntimeError) as error:
                self.status.setText(str(error))
                return
            payload['autoZip'] = self.auto_zip.isChecked()
            payload['rate'] = int(self.rate_spin.value())
            self._submitted = payload.copy()
            self._saving = True
            self.status.setText('正在保存目录设置…')
            self._update_enabled()
            self.saveRequested.emit(payload)
        elif index == 3:
            self.accept()
            self.recordingRequested.emit()
        else:
            self._show_step(index + 1)

    def previous(self):
        if not self._saving:
            self._show_step(max(0, self.stack.currentIndex() - 1))

    def skip_directories(self):
        if not self._saving:
            self._show_step(2)

    def reject(self):
        if self._saving:
            self.status.setText('正在等待保存结果，请稍候。')
            return
        super().reject()
