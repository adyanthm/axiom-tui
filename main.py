from textual.app import App
from textual.containers import Horizontal, Vertical
from textual.widgets import TextArea, Header, Footer, DirectoryTree, Static, Input, OptionList
from textual.widgets.option_list import Option
from textual.binding import Binding
from textual.reactive import reactive
import sys
import os

from lsp import LspClient, LANG_SERVERS

# ext -> syntax lang
LANGUAGES = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".jsx": "javascript",
    ".tsx": "typescript",
    ".json": "json",
    ".html": "html",
    ".css": "css",
    ".md": "markdown",
    ".markdown": "markdown",
    ".sh": "bash",
    ".bash": "bash",
    ".yml": "yaml",
    ".yaml": "yaml",
    ".toml": "toml",
    ".xml": "xml",
    ".sql": "sql",
    ".rs": "rust",
    ".go": "go",
    ".rb": "ruby",
    ".java": "java",
    ".c": "c",
    ".cpp": "cpp",
    ".h": "c",
    ".hpp": "cpp",
}

# textarea has its own theme system separate from the app theme,
# so we map each app theme to the closest editor theme
EDITOR_THEMES = {
    "textual-dark": "vscode_dark",
    "textual-light": "github_light",
    "nord": "vscode_dark",
    "gruvbox": "monokai",
    "catppuccin-mocha": "monokai",
    "catppuccin-latte": "github_light",
    "catppuccin-frappe": "monokai",
    "catppuccin-macchiato": "monokai",
    "dracula": "dracula",
    "tokyo-night": "vscode_dark",
    "monokai": "monokai",
    "flexoki": "github_light",
    "solarized-light": "github_light",
    "solarized-dark": "vscode_dark",
    "rose-pine": "dracula",
    "rose-pine-moon": "dracula",
    "rose-pine-dawn": "github_light",
    "atom-one-dark": "vscode_dark",
    "atom-one-light": "github_light",
    "ansi-dark": "vscode_dark",
    "ansi-light": "github_light",
}


def detect_language(path):
    _, ext = os.path.splitext(path)
    return LANGUAGES.get(ext.lower())


def safe_read(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return None


class StatusBar(Static):
    # bottom bar showing cursor pos, lang, unsaved indicator
    line = reactive(1)
    col = reactive(1)
    language = reactive("plain text")
    dirty = reactive(False)
    filename = reactive("untitled")

    def render(self):
        marker = " •" if self.dirty else ""
        return (
            f" {self.filename}{marker}"
            f"    Ln {self.line}, Col {self.col}"
            f"    {self.language}"
        )


class CompletionMenu(OptionList):
    # floating autocomplete dropdown, never takes focus

    DEFAULT_CSS = """
    CompletionMenu {
        layer: autocomplete;
        display: none;
        height: auto;
        max-height: 8;
        width: auto;
        min-width: 20;
        max-width: 50;
        border: solid $accent;
        background: $surface;
        padding: 0;
    }
    """

    can_focus = False

    def __init__(self):
        super().__init__(id="completion-menu")
        self.items = []

    def show(self, items, offset):
        self.items = items
        self.clear_options()
        for item in items:
            self.add_option(Option(item["label"]))
        self.styles.offset = offset
        self.display = True
        self.highlighted = 0

    def hide(self):
        self.display = False
        self.items = []

    @property
    def visible(self):
        return self.display and len(self.items) > 0

    def move_up(self):
        if self.highlighted is not None and self.highlighted > 0:
            self.highlighted -= 1

    def move_down(self):
        if self.highlighted is not None and self.highlighted < self.option_count - 1:
            self.highlighted += 1

    def selected_item(self):
        idx = self.highlighted
        if idx is not None and idx < len(self.items):
            return self.items[idx]
        return None


class Editor(App):
    TITLE = "axiom-tui"

    CSS = """
    #workspace {
        layout: horizontal;
    }

    #sidebar {
        width: 30;
        dock: left;
        border-right: solid $accent;
    }

    #editor-area {
        width: 1fr;
        layers: default autocomplete;
    }

    #editor {
        height: 1fr;
    }

    /* hidden by default, toggled with ctrl+f */
    #search-input {
        width: 1fr;
        display: none;
    }

    #status-bar {
        dock: bottom;
        height: 1;
        padding: 0 1;
        color: $text-muted;
        background: $surface;
    }
    """

    BINDINGS = [
        Binding("ctrl+s", "save", "Save", priority=True),
        Binding("ctrl+f", "find", "Find", priority=True),
        Binding("ctrl+b", "toggle_sidebar", "Sidebar"),
        Binding("ctrl+q", "quit", "Quit"),
        Binding("escape", "dismiss", "Dismiss", show=False, priority=True),
    ]

    show_sidebar = reactive(True)

    def __init__(self, file_path=None):
        super().__init__()
        self.file_path = os.path.abspath(file_path) if file_path else None
        self._saved_text = ""
        self.lsp = LspClient()
        self._current_lang = None
        self._completion_timer = None

    def compose(self):
        yield Header()

        with Horizontal(id="workspace"):
            start_dir = "."
            if self.file_path:
                start_dir = os.path.dirname(self.file_path) or "."

            yield DirectoryTree(start_dir, id="sidebar")

            with Vertical(id="editor-area"):
                yield Input(placeholder="Search…", id="search-input")
                yield TextArea(show_line_numbers=True, id="editor")
                yield CompletionMenu()

        yield StatusBar(id="status-bar")
        yield Footer()

    def on_mount(self):
        editor = self.query_one("#editor", TextArea)

        if self.file_path:
            lang = detect_language(self.file_path)
            if lang:
                try:
                    editor.language = lang
                except Exception:
                    pass

            content = safe_read(self.file_path)
            if content is not None:
                editor.text = content
            else:
                editor.text = ""
                if os.path.exists(self.file_path):
                    self.notify(f"Could not read {self.file_path}", severity="error")

            self._saved_text = editor.text
            status = self.query_one("#status-bar", StatusBar)
            status.filename = os.path.basename(self.file_path)
            if lang:
                status.language = lang

            self._start_lsp(lang, editor.text)
        else:
            self._saved_text = ""

        self._sync_editor_theme()
        editor.focus()

    # key handling — intercept up/down/enter/tab when the menu is open

    def on_key(self, event):
        menu = self.query_one("#completion-menu", CompletionMenu)
        if not menu.visible:
            return

        if event.key == "up":
            menu.move_up()
            event.prevent_default()
            event.stop()
        elif event.key == "down":
            menu.move_down()
            event.prevent_default()
            event.stop()
        elif event.key in ("enter", "tab"):
            item = menu.selected_item()
            if item:
                self._insert_completion(item["insert"])
            menu.hide()
            event.prevent_default()
            event.stop()

    # events

    def on_text_area_changed(self, event):
        status = self.query_one("#status-bar", StatusBar)
        status.dirty = event.text_area.text != self._saved_text

        if self.lsp.running:
            self.lsp.did_change(event.text_area.text)
            # debounce: re-trigger completion on every keystroke
            self._schedule_completion()

    def on_text_area_selection_changed(self, event):
        cursor = event.text_area.cursor_location
        status = self.query_one("#status-bar", StatusBar)
        status.line = cursor[0] + 1
        status.col = cursor[1] + 1

    def on_directory_tree_file_selected(self, event):
        self._open_file(str(event.path))

    def on_input_submitted(self, event):
        if event.input.id == "search-input":
            self._run_search(event.value)

    # actions

    def action_save(self):
        editor = self.query_one("#editor", TextArea)

        if not self.file_path:
            self.file_path = os.path.abspath("untitled.txt")

        try:
            with open(self.file_path, "w", encoding="utf-8") as f:
                f.write(editor.text)
        except OSError as exc:
            self.notify(f"Save failed: {exc}", severity="error")
            return

        self._saved_text = editor.text
        status = self.query_one("#status-bar", StatusBar)
        status.dirty = False
        self.notify(f"Saved {os.path.basename(self.file_path)}")

    def action_find(self):
        search = self.query_one("#search-input", Input)
        search.display = not search.display
        if search.display:
            search.focus()
        else:
            self.query_one("#editor", TextArea).focus()

    def action_dismiss(self):
        # dismiss completion menu first, then search bar
        menu = self.query_one("#completion-menu", CompletionMenu)
        if menu.visible:
            menu.hide()
            return

        search = self.query_one("#search-input", Input)
        if search.display:
            search.display = False
            self.query_one("#editor", TextArea).focus()

    def action_toggle_sidebar(self):
        sidebar = self.query_one("#sidebar", DirectoryTree)
        sidebar.display = not sidebar.display

    async def action_quit(self):
        if self.lsp.running:
            await self.lsp.stop()
        self.exit()

    def watch_theme(self, old_theme, new_theme):
        self._sync_editor_theme()

    # completion logic

    def _schedule_completion(self):
        if self._completion_timer:
            self._completion_timer.stop()
        self._completion_timer = self.set_timer(0.1, self._trigger_completion)

    def _trigger_completion(self):
        if not self.lsp.running:
            return
        editor = self.query_one("#editor", TextArea)
        row, col = editor.cursor_location

        # only trigger if the cursor is at the end of a word or after a dot
        lines = editor.text.split("\n")
        if row < len(lines) and col > 0:
            ch = lines[row][col - 1]
            if ch.isalnum() or ch == "_" or ch == ".":
                self.run_worker(self._fetch_completions(row, col), exclusive=True, group="completion")
                return

        # nothing worth completing, hide the menu
        menu = self.query_one("#completion-menu", CompletionMenu)
        if menu.visible:
            menu.hide()

    async def _fetch_completions(self, row, col):
        items = await self.lsp.complete(row, col)
        menu = self.query_one("#completion-menu", CompletionMenu)

        if not items:
            menu.hide()
            return

        editor = self.query_one("#editor", TextArea)

        # position the menu near the cursor
        cursor_offset = editor.cursor_screen_offset
        area_region = self.query_one("#editor-area").region

        x = cursor_offset.x - area_region.x
        y = cursor_offset.y - area_region.y + 1

        menu.show(items, (x, y))

    def _insert_completion(self, text):
        editor = self.query_one("#editor", TextArea)
        row, col = editor.cursor_location

        # walk back to find where the current word starts
        lines = editor.text.split("\n")
        line = lines[row] if row < len(lines) else ""
        word_start = col
        while word_start > 0 and (line[word_start - 1].isalnum() or line[word_start - 1] == "_"):
            word_start -= 1

        # strip parentheses/signatures from insert text (e.g. "getcwd()" -> "getcwd")
        clean = text.split("(")[0] if "(" in text else text

        editor.replace(clean, (row, word_start), (row, col))

    # helpers

    def _sync_editor_theme(self):
        # keep syntax highlighting in sync with the app theme
        editor = self.query_one("#editor", TextArea)
        target = EDITOR_THEMES.get(self.theme, "vscode_dark")
        if editor.theme != target:
            editor.theme = target

    def _open_file(self, path):
        content = safe_read(path)
        if content is None:
            self.notify(f"Cannot read {path}", severity="error")
            return

        self.file_path = os.path.abspath(path)
        editor = self.query_one("#editor", TextArea)
        editor.text = content

        lang = detect_language(path)
        try:
            editor.language = lang
        except Exception:
            pass

        self._saved_text = content

        status = self.query_one("#status-bar", StatusBar)
        status.filename = os.path.basename(path)
        status.language = lang or "plain text"
        status.dirty = False
        status.line = 1
        status.col = 1

        self._start_lsp(lang, content)
        editor.focus()

    def _start_lsp(self, lang, text):
        if not lang or lang not in LANG_SERVERS:
            return

        if lang != self._current_lang:
            self.run_worker(self._swap_lsp(lang, text), exclusive=True, group="lsp")
        elif self.lsp.running:
            self.lsp.did_open(self.file_path, text)

    async def _swap_lsp(self, lang, text):
        await self.lsp.stop()
        root = os.path.dirname(self.file_path) or "."
        ok = await self.lsp.start(lang, root)
        if ok:
            self._current_lang = lang
            self.lsp.did_open(self.file_path, text)
        else:
            self._current_lang = None

    def _run_search(self, query):
        if not query:
            return

        editor = self.query_one("#editor", TextArea)
        text = editor.text
        idx = text.find(query)

        if idx == -1:
            self.notify(f'No results for "{query}"', severity="warning")
            return

        # string index -> (row, col)
        row = text[:idx].count("\n")
        last_nl = text.rfind("\n", 0, idx)
        col = idx if last_nl == -1 else idx - last_nl - 1

        editor.cursor_location = (row, col)
        editor.focus()
        self.notify(f'Found "{query}" at Ln {row + 1}')


def run():
    path = sys.argv[1] if len(sys.argv) > 1 else None
    Editor(path).run()


if __name__ == "__main__":
    run()