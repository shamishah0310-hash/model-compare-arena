import os
import json
import socket
import asyncio
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional
import uvicorn

from client import ArenaClient, load_config

config = load_config()
client = ArenaClient()
app = FastAPI(title="Model Compare Arena", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

static_dir = Path(__file__).parent / "static"
static_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

@app.get("/", response_class=HTMLResponse)
async def index():
    idx_path = static_dir / "index.html"
    with open(idx_path, "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())

@app.get("/api/models")
async def list_models():
    return {"models": client.get_available_models()}

class ModelTarget(BaseModel):
    provider: str
    model: str

class CompareRequest(BaseModel):
    prompt: str
    models: List[ModelTarget]
    system_prompt: Optional[str] = "You are a helpful assistant."

@app.post("/api/compare")
async def compare_models(req: CompareRequest):
    async def sse_stream():
        queue = asyncio.Queue()
        active = len(req.models)

        async def worker(idx: int, prov: str, mod: str):
            try:
                async for update in client.generate_stream(prov, mod, req.prompt, req.system_prompt or ""):
                    await queue.put({"model_index": idx, "provider": prov, "model": mod, "data": update})
            finally:
                await queue.put({"_done": idx})

        for idx, m in enumerate(req.models):
            asyncio.create_task(worker(idx, m.provider, m.model))

        finished = 0
        while finished < active:
            msg = await queue.get()
            if "_done" in msg:
                finished += 1
            else:
                yield f"data: {json.dumps(msg)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(sse_stream(), media_type="text/event-stream")

def find_open_port(host: str, preferred: int) -> int:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, preferred))
            return preferred
    except OSError:
        for p in [preferred + 1, 8088, 8888, 5000, 7860]:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    s.bind((host, p))
                    return p
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]

if __name__ == "__main__":
    host = config.get("app", {}).get("host", "127.0.0.1")
    pref_port = config.get("app", {}).get("port", 8081)
    port = find_open_port(host, pref_port)
    print(f"\n⚔️  Model Compare Arena running at: http://{host}:{port}\n")
    uvicorn.run("app:app", host=host, port=port, reload=False)
