import argparse
import json
import mimetypes
import os
import subprocess
import uuid
from datetime import datetime, timezone

import tornado.ioloop
import tornado.web
import tornado.websocket

from inferencefs.backends import (
    ClaudeCodeContentGenerator,
    ClaudeContentGenerator,
    GeminiContentGenerator,
)

FILENAME_SYSTEM_PROMPT = (
    "You generate descriptive filenames based on file contents. "
    "The filename should be specific and descriptive enough that someone could "
    "guess the file's contents from the name alone. Use common file extensions. "
    "Respond with ONLY the filename. No paths, no explanations, no quotes."
)

FILENAME_USER_PROMPT = (
    "What is the most likely filename for a file with the following contents?\n\n{content}"
)


def generate_filename(generator, content_bytes):
    """Use the same LLM backend to generate a descriptive filename from content."""
    content = content_bytes.decode("utf-8", errors="replace")
    # Truncate very large content to avoid token limits
    if len(content) > 8000:
        content = content[:8000] + "\n... (truncated)"

    prompt = FILENAME_USER_PROMPT.format(content=content)

    if isinstance(generator, ClaudeCodeContentGenerator):
        result = subprocess.run(
            [
                "claude",
                "-p", prompt,
                "--output-format", "json",
                "--no-session-persistence",
                "--model", "sonnet",
                "--max-turns", "1",
                "--system-prompt", FILENAME_SYSTEM_PROMPT,
            ],
            capture_output=True,
            text=True,
        )
        data = json.loads(result.stdout)
        return data["result"].strip().strip('"').strip("'")
    elif isinstance(generator, ClaudeContentGenerator):
        message = generator._client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=100,
            system=FILENAME_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        text: str = message.content[0].text  # type: ignore[union-attr]
        return text.strip().strip('"').strip("'")
    elif isinstance(generator, GeminiContentGenerator):
        from google import genai
        response = generator._client.models.generate_content(
            model="gemini-3.1-flash-lite-preview",
            contents=prompt,
            config=genai.types.GenerateContentConfig(
                system_instruction=FILENAME_SYSTEM_PROMPT,
                max_output_tokens=100,
            ),
        )
        assert response.text is not None
        return response.text.strip().strip('"').strip("'")

BACKEND_MAP = {
    "claude": ClaudeContentGenerator,
    "claude-code": ClaudeCodeContentGenerator,
    "gemini": GeminiContentGenerator,
}

# Global state
recent_shares = []  # list of {filename, original_size, created_at}
pending_uploads = {}  # session_id -> [{filename, size, data}]
ws_clients = set()


def broadcast_shares():
    msg = json.dumps({"type": "shares", "shares": recent_shares})
    for client in list(ws_clients):
        try:
            client.write_message(msg)
        except Exception:
            ws_clients.discard(client)


class MainHandler(tornado.web.RequestHandler):
    def get(self):
        self.render("static/index.html")


class SharesAPIHandler(tornado.web.RequestHandler):
    def get(self):
        self.set_header("Content-Type", "application/json")
        self.write(json.dumps({"shares": recent_shares}))


class ShareHandler(tornado.web.RequestHandler):
    async def get(self, name):
        generator = self.application.settings["generator"]
        try:
            content = await tornado.ioloop.IOLoop.current().run_in_executor(
                None, generator.generate_file_contents, name
            )
        except Exception as e:
            self.set_status(500)
            self.write({"error": f"Failed to hydrate share: {e}"})
            return

        content_type, _ = mimetypes.guess_type(name)
        if content_type:
            self.set_header("Content-Type", content_type)
        else:
            self.set_header("Content-Type", "application/octet-stream")
        self.set_header("Content-Disposition", f'attachment; filename="{name}"')
        self.write(content)


class UploadHandler(tornado.web.RequestHandler):
    def post(self):
        session_id = self.get_argument("session_id", None)
        if not session_id:
            session_id = str(uuid.uuid4())

        if session_id not in pending_uploads:
            pending_uploads[session_id] = []

        for field_name, files in self.request.files.items():
            for upload in files:
                pending_uploads[session_id].append(
                    {
                        "filename": upload["filename"],
                        "size": len(upload["body"]),
                        "body": upload["body"],
                    }
                )

        self.set_header("Content-Type", "application/json")
        self.write(
            json.dumps(
                {
                    "session_id": session_id,
                    "files": [
                        {"filename": f["filename"], "size": f["size"]}
                        for f in pending_uploads[session_id]
                    ],
                }
            )
        )


class CreateShareHandler(tornado.web.RequestHandler):
    async def post(self):
        data = json.loads(self.request.body)
        session_id = data.get("session_id")

        if not session_id or session_id not in pending_uploads:
            self.set_status(400)
            self.write({"error": "No pending uploads for this session"})
            return

        files = pending_uploads.pop(session_id)
        host = self.application.settings["share_host"]
        generator = self.application.settings["generator"]
        created = []

        for f in files:
            # Generate a descriptive filename from the file contents
            share_name = await tornado.ioloop.IOLoop.current().run_in_executor(
                None, generate_filename, generator, f["body"]
            )
            share = {
                "filename": share_name,
                "original_size": f["size"],
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            recent_shares.insert(0, share)
            created.append(
                {
                    **share,
                    "share_url": f"{host}/share/{share_name}",
                }
            )

        # Keep only 10 most recent
        while len(recent_shares) > 10:
            recent_shares.pop()

        broadcast_shares()

        self.set_header("Content-Type", "application/json")
        self.write(json.dumps({"shares": created}))


class ShareWebSocket(tornado.websocket.WebSocketHandler):
    def open(self, *args, **kwargs):
        ws_clients.add(self)
        self.write_message(json.dumps({"type": "shares", "shares": recent_shares}))

    def on_close(self):
        ws_clients.discard(self)

    def check_origin(self, origin):
        return True


def make_app(generator, share_host):
    return tornado.web.Application(
        [
            (r"/", MainHandler),
            (r"/api/shares", SharesAPIHandler),
            (r"/share/(.+)", ShareHandler),
            (r"/upload", UploadHandler),
            (r"/create-share", CreateShareHandler),
            (r"/ws", ShareWebSocket),
        ],
        generator=generator,
        share_host=share_host.rstrip("/"),
        template_path=os.path.dirname(__file__),
        static_path=os.path.join(os.path.dirname(__file__), "static"),
    )


def main():
    parser = argparse.ArgumentParser(description="Super Share - AI-powered file sharing")
    parser.add_argument(
        "--backend",
        choices=["claude", "claude-code", "gemini"],
        default="gemini",
        help="LLM backend for content generation",
    )
    parser.add_argument("--api-key", help="API key for the chosen backend")
    parser.add_argument(
        "--host",
        default="http://localhost:8888",
        help="Public hostname for share links",
    )
    parser.add_argument("--port", type=int, default=8888, help="Server port")
    args = parser.parse_args()

    generator_cls = BACKEND_MAP[args.backend]
    if generator_cls.requires_api_key and not args.api_key:
        parser.error(f"--api-key is required for the {args.backend} backend")

    kwargs = {}
    if args.api_key:
        kwargs["api_key"] = args.api_key
    generator = generator_cls(**kwargs)

    app = make_app(generator, args.host)
    app.listen(args.port)
    print(f"Super Share running at http://localhost:{args.port}")
    print(f"Share links will use: {args.host}")
    tornado.ioloop.IOLoop.current().start()


if __name__ == "__main__":
    main()
