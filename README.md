# Super Share

Quickly and easily share files with important people in your life.

Uses powerful AI-based data-reduction techniques to share massive datasets in seconds.

Powered by [InferenceFS](https://github.com/philipl/inferencefs)

## Prerequisites

- Python 3.14+
- [uv](https://docs.astral.sh/uv/)
- System packages: `fuse3-devel` and `python3-devel`

On Fedora:

```bash
sudo dnf install -y fuse3-devel python3-devel
```

## Setup

```bash
uv sync
```

## Usage

```bash
uv run python server.py --backend gemini --api-key YOUR_API_KEY
```

Options:

| Flag | Description | Default |
|------|-------------|---------|
| `--backend` | LLM backend: `gemini`, `claude`, or `claude-code` | `gemini` |
| `--api-key` | API key for the chosen backend | |
| `--host` | Public hostname for share links | `http://localhost:8888` |
| `--port` | Server port | `8888` |

Then open http://localhost:8888 in your browser.
