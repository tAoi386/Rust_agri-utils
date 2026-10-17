"""Live, presentation-only localization for the desktop application.

Chinese source text remains the stable application text. Translation never
rewrites editable values, gene codes, inventory identifiers or user names.
Only this module's local widget subclasses override their own Qt setters;
Qt itself and the breeding model are unchanged.
"""

from __future__ import annotations

from functools import lru_cache
import re
from string import Formatter
import weakref

from PySide6.QtCore import QObject, QEvent, QSignalBlocker, QTranslator, Signal
from PySide6.QtWidgets import (
    QApplication, QAbstractButton, QComboBox, QDialogButtonBox, QGroupBox, QLabel, QLineEdit,
    QPlainTextEdit, QPushButton, QSpinBox, QStatusBar, QTableWidget,
    QTabWidget, QWidget,
)


LANGUAGES = (('zh_CN', '简体中文'), ('en', 'English'), ('ru', 'Русский'))
_manager = None
_VERBATIM_FIELDS = frozenset({'name', 'project', 'project_name', 'path', 'filename',
                              'id', 'seed_id', 'output_id', 'genes', 'gene', 'pattern',
                              'actual', 'expected', 'code', 'email'})
_STANDARD = {
    'OK': ('确定', 'OK', 'ОК'), 'Cancel': ('取消', 'Cancel', 'Отмена'),
    'Save': ('保存', 'Save', 'Сохранить'), 'Open': ('打开', 'Open', 'Открыть'),
    'Close': ('关闭', 'Close', 'Закрыть'), 'Yes': ('是', 'Yes', 'Да'),
    'No': ('否', 'No', 'Нет'), 'Apply': ('应用', 'Apply', 'Применить'),
    'Discard': ('不保存', 'Discard', 'Не сохранять'),
    'Save All': ('全部保存', 'Save All', 'Сохранить всё'),
    'Reset': ('重置', 'Reset', 'Сбросить'), 'Help': ('帮助', 'Help', 'Справка'),
}


def _catalogs():
    try:
        from .translation_catalog import TRANSLATIONS, TEMPLATES
    except ImportError:
        return {}, {}
    return TRANSLATIONS, TEMPLATES


def _standard_text(source, language):
    value = _STANDARD.get(source.replace('&', ''))
    if value is None:
        return ''
    return value[{'zh_CN': 0, 'en': 1, 'ru': 2}[language]]


class LanguageManager(QObject):
    changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.language = 'zh_CN'
        self._widgets = weakref.WeakSet()

    def register(self, widget):
        self._widgets.add(widget)

    def set_language(self, code):
        aliases = {'zh': 'zh_CN', 'zh-CN': 'zh_CN', 'en_US': 'en',
                   'en-US': 'en', 'ru_RU': 'ru', 'ru-RU': 'ru'}
        code = aliases.get(code, code)
        if code not in {'zh_CN', 'en', 'ru'}:
            raise ValueError('Unsupported language: ' + str(code))
        if code == self.language:
            return
        self.language = code
        for widget in tuple(self._widgets):
            try:
                widget._retranslate()
            except RuntimeError:
                # Deferred Qt deletion may have removed the C++ object while
                # the wrapper still awaits ordinary Python collection.
                continue
        self.changed.emit(code)


def language_manager():
    global _manager
    if _manager is None:
        _manager = LanguageManager()
    return _manager


def get_language():
    return language_manager().language


def set_language(code):
    language_manager().set_language(code)


@lru_cache(maxsize=4)
def _compiled_templates(language):
    _, templates = _catalogs()
    result = []
    for source, translated in templates.get(language, {}).items():
        pieces = []
        names = set()
        literal_count = 0
        for literal, name, spec, conversion in Formatter().parse(source):
            pieces.append(re.escape(literal))
            literal_count += len(literal)
            if name is not None:
                if not name.isidentifier() or spec or conversion:
                    raise ValueError('Invalid translation template: ' + source)
                pieces.append(f'(?P={name})' if name in names else f'(?P<{name}>.*?)')
                names.add(name)
        # A template consisting only of a placeholder could accidentally
        # translate arbitrary user data and therefore is never accepted.
        if literal_count and names:
            flags = re.DOTALL if '\n' in source else 0
            result.append((literal_count, re.compile(''.join(pieces), flags), translated))
    result.sort(key=lambda row: -row[0])
    return tuple(result)


def _format_values(values, language):
    return {name: (str(value) if name in _VERBATIM_FIELDS else _translate(str(value), language, 1))
            for name, value in values.items()}


@lru_cache(maxsize=8192)
def _translate(source, language, depth=0):
    if language == 'zh_CN' or not source:
        return source
    catalog, _ = _catalogs()
    translated = catalog.get(language, {}).get(source)
    if translated is not None:
        return translated
    if depth < 7:
        for _, pattern, template in _compiled_templates(language):
            match = pattern.fullmatch(source)
            if match is not None:
                values = {name: (value if name in _VERBATIM_FIELDS else _translate(value, language, depth + 1))
                          for name, value in match.groupdict().items()}
                try:
                    return template.format(**values)
                except (KeyError, ValueError):
                    continue
        # Multi-line reports often combine several independently translated
        # clauses. Preserve their exact ordering and every captured gene code.
        if '\n' in source:
            return '\n'.join(_translate(line, language, depth + 1) for line in source.split('\n'))
    return source


def tr(source, **values):
    """Translate source text or a named template without changing its data."""
    source = str(source)
    language = get_language()
    if values:
        catalog, templates = _catalogs()
        translated = catalog.get(language, {}).get(source, templates.get(language, {}).get(source, source))
        return translated.format(**_format_values(values, language))
    return _translate(source, language)


def clear_translation_cache():
    """Useful after a catalog update, and for isolated localization checks."""
    _translate.cache_clear()
    _compiled_templates.cache_clear()


class _LiveText:
    def _init_localization(self):
        self._i18n_source_text = ''
        self._i18n_source_tooltip = ''
        language_manager().register(self)

    def setText(self, source):
        self._i18n_image = False
        self._i18n_source_text = str(source)
        return super().setText(str(source) if self.property('i18nUserText') else tr(source))

    def setToolTip(self, source):
        self._i18n_source_tooltip = str(source)
        return super().setToolTip(tr(source))

    def _retranslate(self):
        if not getattr(self, '_i18n_image', False):
            super().setText(self._i18n_source_text if self.property('i18nUserText') else tr(self._i18n_source_text))
        super().setToolTip(tr(self._i18n_source_tooltip))

    def clear(self):
        self.setText('')


class LocalizedLabel(_LiveText, QLabel):
    def __init__(self, text='', parent=None):
        super().__init__(parent)
        self._init_localization()
        self.setText(text)

    def setPixmap(self, pixmap):
        self._i18n_image = True
        super().setPixmap(pixmap)


class LocalizedPushButton(_LiveText, QPushButton):
    def __init__(self, text='', parent=None):
        super().__init__(parent)
        self._init_localization()
        self.setText(text)


class LocalizedComboBox(QComboBox):
    def __init__(self, parent=None):
        super().__init__(parent)
        language_manager().register(self)
        self._i18n_items = []

    def addItem(self, *args):
        values = list(args)
        text_index = 0 if isinstance(values[0], str) else 1
        source = values[text_index]
        values[text_index] = tr(source)
        self._i18n_items.append(str(source))
        super().addItem(*values)

    def itemSourceText(self, index):
        """Return application text before translation when copying controls."""
        if 0 <= index < len(self._i18n_items):
            return self._i18n_items[index]
        return self.itemText(index)

    def setItemText(self, index, source):
        if 0 <= index < len(self._i18n_items):
            self._i18n_items[index] = str(source)
        super().setItemText(index, tr(source))

    def clear(self):
        self._i18n_items.clear()
        super().clear()

    def _retranslate(self):
        blocker = QSignalBlocker(self)
        for index, source in enumerate(self._i18n_items):
            if index < self.count():
                super().setItemText(index, tr(source))
        del blocker
        _translate_property(self, 'toolTip', self.setToolTip)


class LocalizedLineEdit(QLineEdit):
    """Only hints are translated. The editable draft is always verbatim."""

    def __init__(self, *args):
        super().__init__(*args)
        self._i18n_placeholder = ''
        language_manager().register(self)

    def setPlaceholderText(self, source):
        self._i18n_placeholder = str(source)
        super().setPlaceholderText(tr(source))

    def _retranslate(self):
        super().setPlaceholderText(tr(self._i18n_placeholder))
        _translate_property(self, 'toolTip', self.setToolTip)


class LocalizedTabWidget(QTabWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        language_manager().register(self)

    def addTab(self, widget, *args):
        values = list(args)
        source = str(values[-1])
        values[-1] = tr(source)
        widget._i18n_tab_source = source
        return super().addTab(widget, *values)

    def setTabText(self, index, source):
        page = self.widget(index)
        if page is not None:
            page._i18n_tab_source = str(source)
        super().setTabText(index, tr(source))

    def _retranslate(self):
        blocker = QSignalBlocker(self)
        for index in range(self.count()):
            page = self.widget(index)
            source = getattr(page, '_i18n_tab_source', self.tabText(index))
            super().setTabText(index, tr(source))
        del blocker


class LocalizedTableWidget(QTableWidget):
    def __init__(self, *args):
        super().__init__(*args)
        language_manager().register(self)

    def setHorizontalHeaderLabels(self, sources):
        self._i18n_headers = tuple(str(x) for x in sources)
        super().setHorizontalHeaderLabels([tr(x) for x in self._i18n_headers])
        for column, source in enumerate(self._i18n_headers):
            item = self.horizontalHeaderItem(column)
            if item is not None:
                item._i18n_source_text = source

    def setItem(self, row, column, item):
        if item is not None and column in (self.property('i18nColumns') or []):
            set_translatable_item(item, item.text())
        super().setItem(row, column, item)

    def _retranslate(self):
        if hasattr(self, '_i18n_headers'):
            super().setHorizontalHeaderLabels([tr(x) for x in self._i18n_headers])
        _translate_table_cells(self)


def set_translatable_item(item, source):
    """Localize a status cell explicitly; editable gene/name cells never use it."""
    item._i18n_source_text = str(source)
    item.setText(tr(source))


def _translate_property(obj, getter, setter):
    value = str(getattr(obj, getter)())
    sources = getattr(obj, '_i18n_properties', None)
    if sources is None:
        sources = obj._i18n_properties = {}
    source, previous = sources.get(getter, (value, None))
    if previous is not None and value != previous:
        source = value
    translated = tr(source)
    if value != translated:
        setter(translated)
    sources[getter] = source, translated


def _translate_table_cells(table):
    columns = table.property('i18nColumns') or []
    if not columns:
        return
    blocker = QSignalBlocker(table)
    for row in range(table.rowCount()):
        for column in columns:
            item = table.item(row, column)
            if item is not None:
                source = getattr(item, '_i18n_source_text', item.text())
                set_translatable_item(item, source)
    del blocker


def localize_tree(root):
    """Localize presentation properties, preserving selections and all drafts."""
    widgets = [root, *root.findChildren(QWidget)]
    for widget in widgets:
        if widget.property('i18nSkip'):
            continue
        if isinstance(widget, _LiveText):
            widget._retranslate()
        elif isinstance(widget, QAbstractButton) and isinstance(widget.parentWidget(), QDialogButtonBox):
            source = getattr(widget, '_i18n_standard_source', None)
            if source is None:
                value = widget.text().replace('&', '')
                source = next((key for key, words in _STANDARD.items() if value == key or value in words), None)
                widget._i18n_standard_source = source
            if source:
                widget.setText(_standard_text(source, get_language()))
            else:
                _translate_property(widget, 'text', widget.setText)
        elif isinstance(widget, (QLabel, QAbstractButton)) and not widget.property('i18nUserText'):
            _translate_property(widget, 'text', widget.setText)
        _translate_property(widget, 'toolTip', widget.setToolTip)
        _translate_property(widget, 'accessibleName', widget.setAccessibleName)
        _translate_property(widget, 'accessibleDescription', widget.setAccessibleDescription)
        if widget.isWindow():
            _translate_property(widget, 'windowTitle', widget.setWindowTitle)
        if isinstance(widget, (QLineEdit, QPlainTextEdit)):
            _translate_property(widget, 'placeholderText', widget.setPlaceholderText)
        if isinstance(widget, QGroupBox):
            _translate_property(widget, 'title', widget.setTitle)
        if isinstance(widget, QSpinBox):
            _translate_property(widget, 'prefix', widget.setPrefix)
            _translate_property(widget, 'suffix', widget.setSuffix)
        if isinstance(widget, LocalizedComboBox):
            widget._retranslate()
        elif isinstance(widget, QComboBox):
            blocker = QSignalBlocker(widget)
            sources = getattr(widget, '_i18n_item_sources', {})
            updated = {}
            for index in range(widget.count()):
                text = widget.itemText(index)
                old_source, old_text = sources.get(index, (text, None))
                source = old_source if text == old_text else text
                translated = tr(source)
                widget.setItemText(index, translated)
                updated[index] = source, translated
            widget._i18n_item_sources = updated
            del blocker
        if isinstance(widget, LocalizedTabWidget):
            widget._retranslate()
        elif isinstance(widget, QTabWidget):
            blocker = QSignalBlocker(widget)
            for index in range(widget.count()):
                page = widget.widget(index)
                source = getattr(page, '_i18n_tab_source', widget.tabText(index))
                page._i18n_tab_source = source
                widget.setTabText(index, tr(source))
            del blocker
        if isinstance(widget, QTableWidget):
            blocker = QSignalBlocker(widget)
            for column in range(widget.columnCount()):
                item = widget.horizontalHeaderItem(column)
                if item is not None:
                    source = getattr(item, '_i18n_source_text', item.text())
                    set_translatable_item(item, source)
            _translate_table_cells(widget)
            del blocker
        for action in widget.actions():
            _translate_property(action, 'text', action.setText)
            _translate_property(action, 'toolTip', action.setToolTip)


class _StandardTranslator(QTranslator):
    def translate(self, context, source, disambiguation=None, n=-1):
        if context.startswith('Q'):
            return _standard_text(source, get_language()) or None
        # None is Qt's null QString: this translator has no entry and Qt must
        # retain its original text. Returning '' instead is an intentional
        # empty translation and even breaks QImage's automatic format lookup.
        return None


class _LocalizationFilter(QObject):
    def __init__(self, app):
        super().__init__(app)
        self._active = False

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Show and isinstance(watched, QWidget) and not self._active:
            self._active = True
            try:
                localize_tree(watched)
            finally:
                self._active = False
        return False


def install_localization(app=None):
    """Install event-driven dialog localization once; no polling or Qt patch."""
    app = app or QApplication.instance()
    if app is None:
        raise RuntimeError('Create QApplication before installing localization.')
    if not hasattr(app, '_seed_localization_filter'):
        app._seed_localization_filter = _LocalizationFilter(app)
        app.installEventFilter(app._seed_localization_filter)
        app._seed_standard_translator = _StandardTranslator(app)
        app.installTranslator(app._seed_standard_translator)
        # Refresh existing windows without rendering or rebuilding any model.
        language_manager().changed.connect(lambda _code: [localize_tree(x) for x in app.topLevelWidgets()])
    return language_manager()
