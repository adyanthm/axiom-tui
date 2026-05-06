from textual.app import App
from textual.containers import Horizontal, Vertical
from textual.widgets import TextArea, Header, Footer, DirectoryTree, Static, Input, OptionList, TabbedContent, TabPane
from textual.widgets.option_list import Option
from textual.binding import Binding
from textual.reactive import reactive
from textual.theme import Theme
import sys
import os
import hashlib
from pathlib import Path

from lsp import LspClient, LANG_SERVERS, uri_to_path

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
    "axiom-pro": "dracula",
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

class AxiomEditor(TextArea):
    """TextArea with auto-indent and tab-as-spaces."""

    def on_mount(self):
        self.indent_width = 4
        self.indent_type = "spaces"
        self.tab_behavior = "indent"

    async def _on_key(self, event):
        if not self.read_only and event.key == "enter":
            event.stop()
            event.prevent_default()

            row, col = self.cursor_location
            lines = self.text.split("\n")
            current_line = lines[row] if row < len(lines) else ""

            # match existing indentation
            indent = 0
            for ch in current_line:
                if ch == " ":
                    indent += 1
                elif ch == "\t":
                    indent += self.indent_width
                else:
                    break

            # smart indent: increase after block openers
            text_before = current_line[:col].rstrip()
            if text_before and text_before[-1] in (":", "{", "[", "("):
                indent += self.indent_width

            start, end = self.selection
            self._replace_via_keyboard("\n" + " " * indent, start, end)
            return

        await super()._on_key(event)

class StatusBar(Static):
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
    DEFAULT_CSS = """
    CompletionMenu {
        layer: autocomplete;
        display: none;
        height: auto;
        max-height: 10;
        width: auto;
        min-width: 30;
        max-width: 60;
        border: round $accent;
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

class NewFileDialog(Vertical):
    DEFAULT_CSS = """
    NewFileDialog {
        layer: overlay;
        display: none;
        width: 65;
        height: auto;
        border: round $accent;
        background: $surface;
        padding: 0 1;
        offset-x: 25;
        offset-y: 2;
    }
    NewFileDialog Input {
        border: none;
        background: transparent;
    }
    NewFileDialog Input:focus {
        border: none;
    }
    """
    def compose(self):
        yield Input(id="new-file-input")

    def on_mount(self):
        self.border_title = "Add a new file or directory (directories end with a '/')"

    def on_input_submitted(self, event):
        self.app.action_create_file(event.value)
        self.display = False
        event.input.value = ""

class Editor(App):
    TITLE = "axiom-tui"

    CSS = """
    #workspace {
        layout: horizontal;
    }

    #sidebar {
        width: 30;
        dock: left;
    }

    #editor-area {
        width: 1fr;
        layers: default autocomplete overlay;
    }

    #tabs {
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
        Binding("ctrl+n", "new_file", "New File"),
        Binding("ctrl+w", "close_tab", "Close Tab"),
        Binding("ctrl+b", "toggle_sidebar", "Sidebar"),
        Binding("ctrl+e", "focus_sidebar", "Explorer", priority=True),
        Binding("ctrl+pageup", "prev_tab", "Prev Tab", show=False, priority=True),
        Binding("ctrl+pagedown", "next_tab", "Next Tab", show=False, priority=True),
        Binding("f12", "goto_definition", "Go to Definition", priority=True),
        Binding("ctrl+q", "quit", "Quit"),
        Binding("escape", "dismiss", "Dismiss", show=False, priority=True),
    ]

    show_sidebar = reactive(True)

    def __init__(self, target_path=None):
        super().__init__()
        
        self.start_dir = "."
        self.file_path = None
        
        if target_path:
            abs_path = os.path.abspath(target_path)
            if os.path.isdir(abs_path):
                self.start_dir = abs_path
            else:
                self.start_dir = os.path.dirname(abs_path) or "."
                self.file_path = abs_path

        self.open_files = {} # abs_path -> content
        self.pane_to_path = {} # pane_id -> abs_path
        self.lsp = LspClient()
        self._current_lang = None
        self._completion_timer = None

    def compose(self):
        yield Header()

        with Horizontal(id="workspace"):
            yield DirectoryTree(self.start_dir, id="sidebar")

            with Vertical(id="editor-area"):
                yield NewFileDialog(id="new-file-dialog")
                yield Input(placeholder="Search…", id="search-input")
                yield TabbedContent(id="tabs")
                yield CompletionMenu()

        yield StatusBar(id="status-bar")
        yield Footer()

    def on_mount(self):
        self.register_theme(
            Theme(
                name="axiom-pro",
                primary="#f5a97f",
                secondary="#8aadf4",
                background="#1e1e2e",
                surface="#1e1e2e",
                panel="#1e1e2e",
                warning="#eed49f",
                error="#ed8796",
                success="#a6da95",
                accent="#f5a97f",
                dark=True,
            )
        )
        self.theme = "textual-dark"

        if self.file_path:
            self._open_file(self.file_path)
        else:
            if self.start_dir != "." and os.path.isdir(self.start_dir):
                self.sub_title = os.path.basename(self.start_dir)
                self.query_one("#sidebar", DirectoryTree).focus()
            else:
                self.sub_title = "untitled"

    def _get_active_editor(self):
        tabs = self.query_one("#tabs", TabbedContent)
        if not tabs.active:
            return None
        try:
            pane = self.query_one(f"#{tabs.active}", TabPane)
            return pane.query_one(TextArea)
        except Exception:
            return None

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

    def on_text_area_changed(self, event):
        editor = self._get_active_editor()
        if editor and event.text_area == editor and self.file_path:
            status = self.query_one("#status-bar", StatusBar)
            saved_text = self.open_files.get(self.file_path, "")
            status.dirty = event.text_area.text != saved_text

        if self.lsp.running:
            self._schedule_completion()

    def on_text_area_selection_changed(self, event):
        editor = self._get_active_editor()
        if editor and event.text_area == editor:
            cursor = event.text_area.cursor_location
            status = self.query_one("#status-bar", StatusBar)
            status.line = cursor[0] + 1
            status.col = cursor[1] + 1

    def on_directory_tree_file_selected(self, event):
        self._open_file(str(event.path))

    def on_tabbed_content_tab_activated(self, event):
        if not event.pane or not event.pane.id or not event.pane.id.startswith("tab-"):
            return
            
        pane_id = event.pane.id
        abs_path = self.pane_to_path.get(pane_id)
        if not abs_path:
            return
            
        self.file_path = abs_path
        
        try:
            editor = event.pane.query_one(TextArea)
            self._update_status_bar(abs_path, editor)
            editor.focus()
            
            lang = detect_language(abs_path)
            self._start_lsp(lang, editor.text)
        except Exception:
            pass

    def on_input_submitted(self, event):
        if event.input.id == "search-input":
            self._run_search(event.value)

    def action_save(self):
        editor = self._get_active_editor()
        if not editor:
            return

        if not self.file_path:
            self.file_path = os.path.abspath("untitled.txt")

        try:
            with open(self.file_path, "w", encoding="utf-8") as f:
                f.write(editor.text)
        except OSError as exc:
            self.notify(f"Save failed: {exc}", severity="error")
            return

        self.open_files[self.file_path] = editor.text
        self._update_status_bar(self.file_path, editor)
        self.notify(f"Saved {os.path.basename(self.file_path)}")

    def action_find(self):
        search = self.query_one("#search-input", Input)
        search.display = not search.display
        if search.display:
            search.focus()
        else:
            editor = self._get_active_editor()
            if editor:
                editor.focus()

    def action_new_file(self):
        dialog = self.query_one("#new-file-dialog", NewFileDialog)
        dialog.display = True
        self.query_one("#new-file-input", Input).focus()

    def action_create_file(self, path):
        if not path:
            return
            
        tree = self.query_one("#sidebar", DirectoryTree)
        if tree.cursor_node and tree.cursor_node.data:
            node_path = Path(tree.cursor_node.data.path)
            if node_path.is_file():
                parent_dir = node_path.parent
            else:
                parent_dir = node_path
        else:
            parent_dir = Path(self.start_dir)
            
        safe_path = path.lstrip("/\\")
        full_path = parent_dir / safe_path
        
        if full_path.exists():
            self.notify(f"'{full_path.name}' already exists", severity="error")
            return

        try:
            if path.endswith("/") or path.endswith("\\"):
                full_path.mkdir(parents=True, exist_ok=True)
                self.notify(f"Created directory {full_path.name}")
            else:
                full_path.parent.mkdir(parents=True, exist_ok=True)
                full_path.touch(exist_ok=True)
                self._open_file(str(full_path))
        except OSError as e:
            self.notify(f"Failed to create: {e}", severity="error")
            return
            
        if hasattr(tree, "reload"):
            tree.reload()

    def action_close_tab(self):
        tabs = self.query_one("#tabs", TabbedContent)
        if not tabs.active:
            return
            
        pane_id = tabs.active
        abs_path = self.pane_to_path.get(pane_id)
        
        tabs.remove_pane(pane_id)
        if abs_path and abs_path in self.open_files:
            del self.open_files[abs_path]
        if pane_id in self.pane_to_path:
            del self.pane_to_path[pane_id]
            
        if not self.open_files:
            self.file_path = None
            self.sub_title = "untitled"
        else:
            # update file_path to the now-active tab
            new_active = tabs.active
            new_path = self.pane_to_path.get(new_active)
            if new_path:
                self.file_path = new_path
                self.sub_title = os.path.basename(new_path)
            
    def action_dismiss(self):
        menu = self.query_one("#completion-menu", CompletionMenu)
        if menu.visible:
            menu.hide()
            return

        dialog = self.query_one("#new-file-dialog", NewFileDialog)
        if dialog.display:
            dialog.display = False
            editor = self._get_active_editor()
            if editor:
                editor.focus()
            return

        search = self.query_one("#search-input", Input)
        if search.display:
            search.display = False
            editor = self._get_active_editor()
            if editor:
                editor.focus()

    def action_toggle_sidebar(self):
        sidebar = self.query_one("#sidebar", DirectoryTree)
        sidebar.display = not sidebar.display
        if sidebar.display:
            sidebar.focus()
        else:
            editor = self._get_active_editor()
            if editor:
                editor.focus()

    def action_focus_sidebar(self):
        sidebar = self.query_one("#sidebar", DirectoryTree)
        if not sidebar.display:
            sidebar.display = True
        sidebar.focus()

    def action_prev_tab(self):
        tabs = self.query_one("#tabs", TabbedContent)
        pane_ids = [p.id for p in tabs.query(TabPane) if p.id]
        if not pane_ids or not tabs.active:
            return
        idx = pane_ids.index(tabs.active) if tabs.active in pane_ids else 0
        tabs.active = pane_ids[(idx - 1) % len(pane_ids)]

    def action_next_tab(self):
        tabs = self.query_one("#tabs", TabbedContent)
        pane_ids = [p.id for p in tabs.query(TabPane) if p.id]
        if not pane_ids or not tabs.active:
            return
        idx = pane_ids.index(tabs.active) if tabs.active in pane_ids else 0
        tabs.active = pane_ids[(idx + 1) % len(pane_ids)]

    def action_goto_definition(self):
        if not self.lsp.running or not self.file_path:
            self.notify("No language server running", severity="warning")
            return
        editor = self._get_active_editor()
        if not editor:
            return
        self.lsp.did_change(editor.text)
        row, col = editor.cursor_location
        self.run_worker(self._do_goto_definition(row, col), exclusive=True, group="goto-def")

    async def _do_goto_definition(self, row, col):
        result = await self.lsp.goto_definition(row, col)
        if not result:
            self.notify("No definition found", severity="warning")
            return

        target_path = uri_to_path(result["uri"])
        target_line = result["line"]
        target_col = result["col"]

        # open the file (or switch to its tab)
        self._open_file(target_path)

        # jump to the definition location after the tab loads
        def _jump():
            editor = self._get_active_editor()
            if editor:
                editor.cursor_location = (target_line, target_col)
                editor.focus()
        self.call_after_refresh(_jump)

    async def action_quit(self):
        if self.lsp.running:
            await self.lsp.stop()
        self.exit()

    def watch_theme(self, old_theme, new_theme):
        for editor in self.query(TextArea):
            self._sync_editor_theme_for(editor)

    def _schedule_completion(self):
        if self._completion_timer:
            self._completion_timer.stop()
        self._completion_timer = self.set_timer(0.1, self._trigger_completion)

    def _trigger_completion(self):
        if not self.lsp.running:
            return
        editor = self._get_active_editor()
        if not editor:
            return
            
        lang = detect_language(self.file_path)
        if lang != self._current_lang:
            return
            
        self.lsp.did_change(editor.text)
        
        row, col = editor.cursor_location
        lines = editor.text.split("\n")
        if row < len(lines) and col > 0:
            ch = lines[row][col - 1]
            if ch.isalnum() or ch == "_" or ch == ".":
                self.run_worker(self._fetch_completions(row, col), exclusive=True, group="completion")
                return

        menu = self.query_one("#completion-menu", CompletionMenu)
        if menu.visible:
            menu.hide()

    async def _fetch_completions(self, row, col):
        items = await self.lsp.complete(row, col)
        menu = self.query_one("#completion-menu", CompletionMenu)

        if not items:
            menu.hide()
            return

        editor = self._get_active_editor()
        if not editor:
            return

        cursor_offset = editor.cursor_screen_offset
        area_region = self.query_one("#editor-area").region

        x = cursor_offset.x - area_region.x
        y = cursor_offset.y - area_region.y + 1

        menu.show(items, (x, y))

    def _insert_completion(self, text):
        editor = self._get_active_editor()
        if not editor:
            return
        row, col = editor.cursor_location

        lines = editor.text.split("\n")
        line = lines[row] if row < len(lines) else ""
        word_start = col
        while word_start > 0 and (line[word_start - 1].isalnum() or line[word_start - 1] == "_"):
            word_start -= 1

        clean = text.split("(")[0] if "(" in text else text
        editor.replace(clean, (row, word_start), (row, col))

    def _sync_editor_theme_for(self, editor):
        target = EDITOR_THEMES.get(self.theme, "vscode_dark")
        if editor.theme != target:
            editor.theme = target

    def _open_file(self, path):
        abs_path = os.path.abspath(path)
        tabs = self.query_one("#tabs", TabbedContent)
        
        pane_id = "tab-" + hashlib.md5(abs_path.encode()).hexdigest()
        
        if abs_path in self.open_files:
            tabs.active = pane_id
            return

        content = safe_read(abs_path)
        if content is None:
            self.notify(f"Cannot read {path}", severity="error")
            return

        self.open_files[abs_path] = content
        self.pane_to_path[pane_id] = abs_path
        self.file_path = abs_path
        
        filename = os.path.basename(abs_path)
        editor_id = "editor-" + hashlib.md5(abs_path.encode()).hexdigest()
        editor = AxiomEditor(content, show_line_numbers=True, id=editor_id)
        
        lang = detect_language(abs_path)
        if lang:
            try:
                editor.language = lang
            except Exception:
                pass
                
        pane = TabPane(filename, editor, id=pane_id)
        tabs.add_pane(pane)
        tabs.active = pane_id

        self._sync_editor_theme_for(editor)
        self._start_lsp(lang, content)
        
        self.call_after_refresh(editor.focus)
        self._update_status_bar(abs_path, editor)

    def _update_status_bar(self, path, editor):
        status = self.query_one("#status-bar", StatusBar)
        status.filename = os.path.basename(path)
        self.sub_title = status.filename
        
        lang = detect_language(path)
        status.language = lang or "plain text"
        
        saved_text = self.open_files.get(path, "")
        status.dirty = editor.text != saved_text
        
        cursor = editor.cursor_location
        status.line = cursor[0] + 1
        status.col = cursor[1] + 1

    def _start_lsp(self, lang, text):
        if not lang or lang not in LANG_SERVERS:
            if self.lsp.running:
                self.run_worker(self.lsp.stop(), exclusive=True, group="lsp")
                self._current_lang = None
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

        editor = self._get_active_editor()
        if not editor:
            return
            
        text = editor.text
        idx = text.find(query)

        if idx == -1:
            self.notify(f'No results for "{query}"', severity="warning")
            return

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