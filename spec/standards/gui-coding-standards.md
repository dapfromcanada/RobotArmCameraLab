# Desktop GUI Coding Standards for AI Agents (Python / Qt)

> Scope: **desktop GUI apps built with PySide6/PyQt.** For web UIs use
> `web-app-standards.md`; for CLI/API use `cli-api-standards.md`. The class-
> structure discipline below (named widgets, separated setup methods, factory
> pattern) is the spirit to carry into any UI framework even when the Qt
> specifics don't apply.
>
> These rules must be enforced on every phase spec that involves desktop GUI code.
> AI agents must follow these conventions exactly. No exceptions.
> Reference this file at the start of every GUI phase before writing any widget code.

---

## 1. Widget Class Structure (Mandatory)

Every QWidget subclass must follow this exact method order and separation:

```python
class MyWidget(QWidget):

    def __init__(self, parent=None):
        super().__init__(parent)
        self._create_widgets()     # build widgets only
        self._create_layouts()     # arrange widgets into layouts only
        self._connect_signals()    # wire signals/slots only
        self._apply_styles()       # styling only
        self._load_initial_state() # populate data, load settings only

    # --- Widget Construction ---
    def _create_widgets(self):
        """Create all child widgets. No layout, no signals, no logic."""
        ...

    # --- Layout ---
    def _create_layouts(self):
        """Arrange widgets into layouts. No business logic."""
        ...

    # --- Signals & Slots ---
    def _connect_signals(self):
        """Connect all signals to slots. Nothing else."""
        ...

    # --- Styling ---
    def _apply_styles(self):
        """Apply stylesheets and visual properties. No logic."""
        ...

    # --- Initial State ---
    def _load_initial_state(self):
        """Load settings, populate fields, set defaults."""
        ...

    # --- Business Logic (slots and helpers below this line) ---
    def _on_save_button_clicked(self):
        ...
```

**Rules:**
- `__init__` must ONLY call the 5 setup helpers — no direct widget creation in `__init__`
- Never mix layout code into `_create_widgets()`
- Never mix signal connections into `_create_layouts()`
- Never put business logic inside any `_create_*` or `_connect_*` method
- Slot methods are named `_on_<widget>_<event>` (e.g. `_on_save_button_clicked`)

---

## 2. One Widget Per Named Variable (Mandatory)

Every widget must have its own clearly named instance variable:

```python
# CORRECT
self.save_button = QPushButton("Save")
self.cancel_button = QPushButton("Cancel")
self.name_label = QLabel("Name:")
self.name_input = QLineEdit()

# WRONG — anonymous widgets dropped directly into layout
layout.addWidget(QPushButton("Save"))
layout.addWidget(QLabel("Name:"))
```

**Why this matters:** Anonymous widgets cannot be found, moved, restyled,
or referenced later. If an agent needs to "move a label", it must be able
to find it by name in `_create_widgets()` and adjust only its entry in
`_create_layouts()` — without touching anything else.

---

## 3. Layout Naming Convention

Every layout must have a named instance variable:

```python
self.main_layout = QVBoxLayout(self)
self.toolbar_layout = QHBoxLayout()
self.form_layout = QFormLayout()
self.button_layout = QHBoxLayout()
```

Named layouts can be reorganized without rewriting the whole method.

---

## 4. No Magic Numbers in Layout

```python
# CORRECT — define constants at the top of the file or class
MARGIN = 12
SPACING = 8
BUTTON_HEIGHT = 32

self.main_layout.setContentsMargins(MARGIN, MARGIN, MARGIN, MARGIN)
self.main_layout.setSpacing(SPACING)
self.save_button.setFixedHeight(BUTTON_HEIGHT)

# WRONG
self.main_layout.setContentsMargins(12, 12, 12, 12)
self.save_button.setFixedHeight(32)
```

---

## 5. Qt Designer Boundary Rule (when .ui files are used)

- `.ui` files are owned by Qt Designer — **never hand-edit them**
- Compile with: `pyside6-uic form.ui -o ui_form.py`
- `ui_form.py` is auto-generated — **never hand-edit it** — regenerate instead
- Business logic lives only in the Python class that loads the .ui file
- **AI agents must never modify `.ui` or `ui_*.py` files**

---

## 6. Dynamic Widget Generation Rule

For widgets generated at runtime (cards, rows, list items, exercise entries, etc.):

```python
def _create_card_widget(self, data: MyData) -> QWidget:
    """Factory method — creates and returns ONE card widget."""
    card = QFrame()
    card.setObjectName(f"card_{data.id}")
    title = QLabel(data.name)
    layout = QVBoxLayout(card)
    layout.addWidget(title)
    return card

def _populate_card_list(self, items: list[MyData]):
    """Clears and repopulates the card container."""
    self._clear_card_list()
    for item in items:
        card = self._create_card_widget(item)
        self.card_layout.addWidget(card)

def _clear_card_list(self):
    """Remove all dynamically generated cards safely."""
    while self.card_layout.count():
        child = self.card_layout.takeAt(0)
        if child.widget():
            child.widget().deleteLater()
```

**Rules:**
- One factory method per card/row type (`_create_<type>_widget`)
- Populate and clear are always separate methods
- Never inline card creation in a loop inside `__init__` or any `_create_*` method
- Always use `deleteLater()` when removing dynamic widgets

---

## 7. Stylesheet Rule

- All stylesheets go in `_apply_styles()` or a dedicated `styles.py` module
- Never set stylesheets inline during widget creation in `_create_widgets()`
- Use `setObjectName()` for targeted per-widget CSS selectors

```python
# styles.py example
BUTTON_PRIMARY = """
    QPushButton {
        background-color: #3498db;
        color: white;
        border-radius: 4px;
        padding: 6px 12px;
    }
    QPushButton:hover { background-color: #2980b9; }
    QPushButton:disabled { background-color: #7f8c8d; }
"""

DARK_BACKGROUND = "#2c3e50"
TEXT_COLOR = "#ecf0f1"
```

---

## 8. Agent Modification Protocol

When an agent is asked to move, rename, resize, or restyle a widget:

1. Find the widget by its instance variable name in `_create_widgets()`
2. Find its layout placement in `_create_layouts()`
3. Make changes ONLY in those two methods
4. Never touch `_connect_signals()` or business logic methods
5. Run the full test suite after every GUI change
6. If a test fails after a GUI change, fix it before moving on

---

## 9. Always Dark Mode

All GUI projects use dark mode by default:

```python
# Recommended base palette
DARK_BG        = "#2c3e50"
DARK_BG_ALT    = "#34495e"
TEXT_PRIMARY   = "#ecf0f1"
TEXT_SECONDARY = "#bdc3c7"
ACCENT_BLUE    = "#3498db"
ACCENT_GREEN   = "#27ae60"
ACCENT_RED     = "#e74c3c"
ACCENT_ORANGE  = "#f39c12"
BORDER_COLOR   = "#1a252f"
```

---

## 10. Pre-Submit Checklist for Agents

Before submitting any GUI code, verify all of the following:

- [ ] Every widget has a named instance variable (`self.widget_name`)
- [ ] `__init__` only calls the 5 setup helpers — nothing else
- [ ] No business logic inside any `_create_*` method
- [ ] No layout code inside `_create_widgets()`
- [ ] No signal connections outside `_connect_signals()`
- [ ] All magic numbers are named constants
- [ ] Stylesheets are only in `_apply_styles()` or `styles.py`
- [ ] Dynamic widgets use the factory + populate + clear pattern
- [ ] Slot methods named `_on_<widget>_<event>`
- [ ] Dark mode palette applied
- [ ] All tests pass after changes
