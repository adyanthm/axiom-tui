import asyncio
import json
import os

# command to spawn each language's server (must support stdio)
LANG_SERVERS = {
    "python": ["pylsp"],
    "javascript": ["typescript-language-server", "--stdio"],
    "typescript": ["typescript-language-server", "--stdio"],
    "rust": ["rust-analyzer"],
    "go": ["gopls", "serve"],
    "c": ["clangd"],
    "cpp": ["clangd"],
}


def path_to_uri(path):
    path = os.path.abspath(path).replace("\\", "/")
    if not path.startswith("/"):
        path = "/" + path
    return f"file://{path}"


def uri_to_path(uri):
    """Convert a file:// URI back to an OS path."""
    if not uri.startswith("file://"):
        return uri
    path = uri[len("file://"):]
    # Windows: file:///C:/path → /C:/path, strip leading /
    if len(path) > 2 and path[0] == "/" and path[2] == ":":
        path = path[1:]
    return path.replace("/", os.sep)


class LspClient:

    def __init__(self):
        self._proc = None
        self._id = 0
        self._pending = {}
        self._reader_task = None
        self._version = 0
        self._uri = None
        self._language = None

    @property
    def running(self):
        return self._proc is not None and self._proc.returncode is None

    async def start(self, language, root_path):
        cmd = LANG_SERVERS.get(language)
        if not cmd:
            return False

        try:
            self._proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            return False

        self._reader_task = asyncio.create_task(self._read_loop())
        self._language = language

        await self._request("initialize", {
            "processId": os.getpid(),
            "rootUri": path_to_uri(root_path),
            "capabilities": {
                "textDocument": {
                    "completion": {
                        "completionItem": {"snippetSupport": False},
                    },
                    "definition": {
                        "dynamicRegistration": False,
                    },
                },
            },
        })
        self._notify("initialized", {})
        return True

    async def stop(self):
        if not self.running:
            return

        try:
            await asyncio.wait_for(self._request("shutdown", None), timeout=2)
            self._notify("exit", None)
        except Exception:
            pass

        if self._reader_task:
            self._reader_task.cancel()
        if self._proc:
            try:
                if self._proc.stdin:
                    self._proc.stdin.close()
            except Exception:
                pass
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=1)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
        self._proc = None
        self._pending.clear()

    def did_open(self, path, text):
        self._uri = path_to_uri(path)
        self._version = 1
        self._notify("textDocument/didOpen", {
            "textDocument": {
                "uri": self._uri,
                "languageId": self._language,
                "version": self._version,
                "text": text,
            },
        })

    def did_change(self, text):
        if not self._uri:
            return
        self._version += 1
        self._notify("textDocument/didChange", {
            "textDocument": {"uri": self._uri, "version": self._version},
            "contentChanges": [{"text": text}],
        })

    async def complete(self, line, col):
        if not self._uri or not self.running:
            return []

        try:
            result = await asyncio.wait_for(
                self._request("textDocument/completion", {
                    "textDocument": {"uri": self._uri},
                    "position": {"line": line, "character": col},
                }),
                timeout=5,
            )
        except Exception:
            return []

        if result is None:
            return []

        items = result if isinstance(result, list) else result.get("items", [])

        completions = []
        for item in items:
            label = item.get("label", "")
            insert = item.get("insertText") or label
            completions.append({"label": label, "insert": insert})

        return completions

    async def goto_definition(self, line, col):
        if not self._uri or not self.running:
            return None

        try:
            result = await asyncio.wait_for(
                self._request("textDocument/definition", {
                    "textDocument": {"uri": self._uri},
                    "position": {"line": line, "character": col},
                }),
                timeout=5,
            )
        except Exception:
            return None

        if not result:
            return None

        # result can be Location, Location[], or LocationLink[]
        target = result[0] if isinstance(result, list) else result

        # LocationLink has targetUri, Location has uri
        if "targetUri" in target:
            uri = target["targetUri"]
            pos = target.get("targetSelectionRange",
                             target.get("targetRange", {})).get(
                                 "start", {"line": 0, "character": 0})
        else:
            uri = target.get("uri", "")
            pos = target.get("range", {}).get(
                "start", {"line": 0, "character": 0})

        return {
            "uri": uri,
            "line": pos.get("line", 0),
            "col": pos.get("character", 0),
        }

    # --- json-rpc internals ---

    async def _request(self, method, params):
        self._id += 1
        rid = self._id

        future = asyncio.get_event_loop().create_future()
        self._pending[rid] = future

        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        return await future

    def _notify(self, method, params):
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _send(self, msg):
        if not self.running:
            return
        body = json.dumps(msg).encode()
        header = f"Content-Length: {len(body)}\r\n\r\n".encode()
        self._proc.stdin.write(header + body)

    async def _read_loop(self):
        try:
            while self.running:
                # each lsp message starts with http-style headers
                content_length = 0
                while True:
                    line = await self._proc.stdout.readline()
                    if not line:
                        return
                    line = line.decode().strip()
                    if not line:
                        break
                    if line.lower().startswith("content-length:"):
                        content_length = int(line.split(":")[1].strip())

                if content_length == 0:
                    continue

                body = await self._proc.stdout.readexactly(content_length)
                msg = json.loads(body)

                rid = msg.get("id")
                if rid is not None and rid in self._pending:
                    future = self._pending.pop(rid)
                    if not future.done():
                        if "error" in msg:
                            future.set_exception(
                                Exception(msg["error"].get("message", "lsp error"))
                            )
                        else:
                            future.set_result(msg.get("result"))
        except (asyncio.CancelledError, ConnectionError):
            pass
        except Exception:
            pass
