from textual.app import App
from textual.containers import Horizontal, Vertical
from textual.widgets import TextArea, Header, Footer, DirectoryTree, Static, Input
from textual.binding import Binding
from textual.reactive import reactive
import sys
import os

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


class Editor(App):
    TITLE = "axiom-tui"
    CSS_PATH = "style.tcss"

    BINDINGS = [
        Binding("ctrl+s", "save", "Save", priority=True),
        Binding("ctrl+f", "find", "Find", priority=True),
        Binding("ctrl+b", "toggle_sidebar", "Sidebar"),
        Binding("ctrl+q", "quit", "Quit"),
        Binding("escape", "dismiss_search", "Close Search", show=False),
    ]

    show_sidebar = reactive(True)

    def __init__(self, file_path=None):
        super().__init__()
        self.file_path = os.path.abspath(file_path) if file_path else None
        self._saved_text = ""

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
        else:
            self._saved_text = ""

        self._sync_editor_theme()
        editor.focus()

    # events

    def on_text_area_changed(self, event):
        status = self.query_one("#status-bar", StatusBar)
        status.dirty = event.text_area.text != self._saved_text

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

    def action_dismiss_search(self):
        search = self.query_one("#search-input", Input)
        if search.display:
            search.display = False
            self.query_one("#editor", TextArea).focus()

    def action_toggle_sidebar(self):
        sidebar = self.query_one("#sidebar", DirectoryTree)
        sidebar.display = not sidebar.display

    def watch_theme(self, old_theme, new_theme):
        self._sync_editor_theme()

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

        editor.focus()

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