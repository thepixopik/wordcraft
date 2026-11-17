#!/usr/bin/env python3
"""Trunk post_build hook: strip debug/name sections, gzip-compress, split into parallel chunks, and inject progress bar."""

import gzip
import json
import os
import pathlib
import re

CHUNK_BYTES = 6 * 1024 * 1024  # 6 MiB parallel chunks (< 25 MiB Cloudflare Pages limit)


def strip_wasm_debug_sections(raw: bytes) -> bytes:
    """Strip non-executable WebAssembly custom debug/name sections ('name', '.debug*', 'reloc.*')."""
    try:
        if len(raw) < 8 or raw[:8] != b"\x00asm\x01\x00\x00\x00":
            return raw
        out = bytearray(raw[:8])
        pos = 8
        n = len(raw)
        stripped = 0
        while pos < n:
            sec_id = raw[pos]
            p = pos + 1
            sec_len = 0
            shift = 0
            while True:
                if p >= n:
                    return raw
                b = raw[p]
                p += 1
                sec_len |= (b & 0x7F) << shift
                if (b & 0x80) == 0:
                    break
                shift += 7
            sec_end = p + sec_len
            if sec_end > n:
                return raw
            drop = False
            if sec_id == 0 and p < sec_end:
                np = p
                name_len = 0
                nshift = 0
                while np < sec_end:
                    nb = raw[np]
                    np += 1
                    name_len |= (nb & 0x7F) << nshift
                    if (nb & 0x80) == 0:
                        break
                    nshift += 7
                if np + name_len <= sec_end:
                    sec_name = raw[np : np + name_len].decode("utf-8", errors="replace")
                    if sec_name == "name" or sec_name.startswith(".debug") or sec_name.startswith("reloc."):
                        drop = True
                        stripped += sec_end - pos
            if not drop:
                out.extend(raw[pos:sec_end])
            pos = sec_end
        if stripped > 0:
            print(f"pack_wasm: stripped {stripped / (1024 * 1024):.2f} MiB of debug/symbol sections")
        return bytes(out)
    except Exception as exc:
        print(f"pack_wasm: strip skipped ({exc})")
        return raw


def main() -> None:
    if os.environ.get("TRUNK_PROFILE") != "release":
        return
    staging = os.environ.get("TRUNK_STAGING_DIR")
    if not staging:
        return

    dist = pathlib.Path(staging)
    wasm_files = sorted(dist.glob("*.wasm"))
    if not wasm_files:
        return

    chunks_map: dict[str, dict[str, object]] = {}
    for wasm_path in wasm_files:
        raw_orig = wasm_path.read_bytes()
        raw = strip_wasm_debug_sections(raw_orig)
        compressed = gzip.compress(raw, compresslevel=9)
        parts: list[str] = []
        for idx, offset in enumerate(range(0, len(compressed), CHUNK_BYTES)):
            part_name = f"{wasm_path.name}.part{idx}"
            (dist / part_name).write_bytes(compressed[offset : offset + CHUNK_BYTES])
            parts.append(part_name)
        orig_mb = len(raw_orig) / (1024 * 1024)
        comp_mb = len(compressed) / (1024 * 1024)
        print(f"pack_wasm: {wasm_path.name} ({orig_mb:.2f} MiB) -> {len(parts)} parallel part(s) ({comp_mb:.2f} MiB gzip)")
        wasm_path.unlink()
        chunks_map[wasm_path.name] = {"parts": parts, "total": len(compressed)}

    index_path = dist / "index.html"
    html = index_path.read_text(encoding="utf-8")
    html = re.sub(r'<link\b[^>]*\bhref="[^"]*\.wasm"[^>]*>', "", html)

    manifest_json = json.dumps(chunks_map)
    shim = (
        "<script>"
        "(() => {"
        f"const chunks = {manifest_json};"
        "const origFetch = window.fetch.bind(window);"
        "window.fetch = async (input, init) => {"
        'const rawUrl = typeof input === "string" ? input : (input instanceof URL ? input.href : input.url);'
        'const cleanUrl = (rawUrl || "").split("?")[0].split("#")[0];'
        'const slash = cleanUrl.lastIndexOf("/");'
        'const fileName = cleanUrl.substring(slash + 1);'
        "const meta = chunks[fileName];"
        "if (meta) {"
        'const dir = slash >= 0 ? cleanUrl.substring(0, slash + 1) : "./";'
        "const parts = meta.parts;"
        "const totalBytes = meta.total || 0;"
        "let loadedBytes = 0;"
        "const updateUi = () => {"
        'const bar = document.getElementById("wc_load_bar");'
        'const pctEl = document.getElementById("wc_load_pct");'
        "if (totalBytes > 0) {"
        "const pct = Math.min(99, Math.round((loadedBytes / totalBytes) * 100));"
        'if (bar) bar.style.width = Math.max(2, pct) + "%";'
        'if (pctEl) pctEl.textContent = pct + "% (" + (loadedBytes / 1048576).toFixed(1) + " / " + (totalBytes / 1048576).toFixed(1) + " MB)";'
        "}"
        "};"
        "const partPromises = parts.map(async (part) => {"
        "const res = await origFetch(dir + part, init);"
        'if (!res.ok) throw new Error("Failed to fetch " + part + " (" + res.status + ")");'
        "if (!res.body) {"
        "const buf = new Uint8Array(await res.arrayBuffer());"
        "loadedBytes += buf.byteLength;"
        "updateUi();"
        "return buf;"
        "}"
        "const reader = res.body.getReader();"
        "const arr = [];"
        "let len = 0;"
        "while (true) {"
        "const { done, value } = await reader.read();"
        "if (done) break;"
        "arr.push(value);"
        "len += value.byteLength;"
        "loadedBytes += value.byteLength;"
        "updateUi();"
        "}"
        "const out = new Uint8Array(len);"
        "let off = 0;"
        "for (const c of arr) { out.set(c, off); off += c.byteLength; }"
        "return out;"
        "});"
        "const stream = new ReadableStream({"
        "async start(controller) {"
        "try {"
        "for (let i = 0; i < partPromises.length; i++) {"
        "controller.enqueue(await partPromises[i]);"
        "}"
        'const bar = document.getElementById("wc_load_bar");'
        'const pctEl = document.getElementById("wc_load_pct");'
        'if (bar) bar.style.width = "100%";'
        'if (pctEl) pctEl.textContent = "100% — Opening document processor…";'
        "controller.close();"
        "} catch (err) { controller.error(err); }"
        "}"
        "});"
        'return new Response(stream.pipeThrough(new DecompressionStream("gzip")), {'
        'status: 200, headers: { "Content-Type": "application/wasm" }'
        "});"
        "}"
        "return origFetch(input, init);"
        "};"
        "})();"
        "</script>"
    )

    html = html.replace("<head>", "<head>\n  " + shim, 1)
    index_path.write_text(html, encoding="utf-8")


if __name__ == "__main__":
    main()
